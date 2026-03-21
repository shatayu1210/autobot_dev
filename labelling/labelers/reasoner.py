"""Reasoner labeler — Model 2 (Qwen2.5-7B-Instruct + LoRA).

Label strategy: 100% GPT-4o narrative generation (Section 5.5 / 5A.5 of AutoBot_Ref_v5).

Input: All Model 1 fields PLUS risk_score, pr_review_comments, full PR review
bodies, and timeline cross-references.

Output label: {"narrative": str}
  2-3 sentence paragraph explaining WHY this issue is a bottleneck,
  citing specific signals from the snapshot (no generic statements).
  Written for a non-technical scrum master audience.

Truncation (2048 token budget, applied in priority order):
  1. Issue body after 600 tokens
  2. Timeline — keep only assigned, labeled, cross-referenced, review_requested
  3. Comments — keep first 2 and last 2, drop middle
  Never truncated: title, labels, assignees, risk_score, days_open, project percentiles
"""

from __future__ import annotations

import logging
import re
import datetime
from pathlib import Path
from typing import Any

from .base import BaseLabeler, call_gpt4o, async_call_gpt4o

logger = logging.getLogger(__name__)

# Project percentiles (Section 5A.1)
P50_DAYS = 1
P75_DAYS = 6
P90_DAYS = 23
P95_DAYS = 47

# Timeline events to keep for reasoner (Section 4.2 truncation)
KEEP_TIMELINE_EVENTS = {"assigned", "labeled", "cross-referenced", "review_requested"}

# ---------------------------------------------------------------------------
# GPT-4o Teacher Prompt (Section 5A.5 — Model 2)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEACHER = """\
You are a senior engineering project analyst writing a bottleneck risk briefing \
for a non-technical scrum master.

Project stats: P50={p50} day, P75={p75} days, P90={p90} days, P95={p95} days. \
Risk score: {score} ({band}).

Write exactly 2-3 sentences as a single paragraph.

Rules:
1. No bullet points or lists. Plain prose only.
2. Must reference at least two specific observable signals from the data below \
(e.g., specific comment author, days since last review, CI failure, or number \
of reassignments).
3. Must mention the numeric risk score and connect it to the evidence.
4. No jargon without a plain-English explanation (e.g., describe a "PR" as a \
"proposed code update").
5. Must mention how many days the issue has been open and compare it to project \
baselines using HUMAN-READABLE phrasing. NEVER say "P75" or "P90". Instead, \
use phrases like "taking longer to resolve than 75% of historical issues" or \
"slower than 90% of the project's issues".
6. Do not write generic phrases like "this issue has been open a long time" — \
be specific with numbers.
7. Tone calibration: If the snapshot is EARLY (T+7), be investigative/cautious. \
If the snapshot is LATE (T+14), match the growing urgency of a stale issue.

Respond with ONLY the narrative paragraph."""

# ---------------------------------------------------------------------------
# Qwen Training / Inference System Prompt (Section 5A.5)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_QWEN = """\
You are a bottleneck analyst for GitHub issues. Given an issue snapshot and its \
risk score, write a 2-3 sentence explanation for a non-technical scrum master. \
Reference specific signals. No bullet points.
RISK_SCORE: {score} | PROJECT: apache/airflow | P50={p50}d P75={p75}d P90={p90}d P95={p95}d"""

# ---------------------------------------------------------------------------
# Few-Shot Examples
# ---------------------------------------------------------------------------
FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": (
            "ISSUE DATA:\n"
            "Issue #48291 — Snapshot at T+14 days [LATE SNAPSHOT]\n"
            "Title: Scheduler silently drops tasks when DAG has more than 50 slots\n"
            "Risk Score: 0.74 (high)\n"
            "Labels: kind:bug, area:scheduler, priority:critical\n"
            "Assignees: 0 (unassigned)\n"
            "Days open at snapshot: 14\n"
            "Max Comment Gap: 11.0 days\n"
            "Linked PRs: None\n"
            "Comments: [dev_alice]: Confirmed on 2.8.1... "
            "[dev_bob]: off-by-one on the slot counter...\n\n"
            "Write a 2-3 sentence diagnostic narrative for this issue."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "At 14 days open, this critical scheduler bug is already taking longer "
            "to resolve than 75% of all historical issues in the project. With no "
            "assignee and no linked code update, its risk score has climbed to 0.74 "
            "as the massive 11-day gap between comments suggests the fix has stalled "
            "despite a developer identifying the exact technical root cause. We should "
            "escalate this before it drifts further into the slowest 10% of issues."
        ),
    },
    {
        "role": "user",
        "content": (
            "ISSUE DATA:\n"
            "Issue #51033 — Snapshot at T+7 days [EARLY SNAPSHOT]\n"
            "Title: Add --dry-run flag to airflow db upgrade command\n"
            "Risk Score: 0.18 (low)\n"
            "Labels: kind:feature, area:cli, good first issue\n"
            "Assignees: 1 (priya_k)\n"
            "Days open at snapshot: 7\n"
            "Linked PRs: PR #51089 | State: open | Reviews: 0 | "
            "Days since last review: 1\n"
            "Comments: [priya_k]: Claimed! Working on a branch...\n\n"
            "Write a 2-3 sentence diagnostic narrative for this issue."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "This feature request is currently showing healthy early momentum and "
            "carries a low 0.18 risk score only seven days into its lifecycle. While "
            "it has recently crossed the one-week mark — making it slightly slower "
            "than the median project issue — developer priya_k is actively moving it "
            "forward with a newly submitted code update that is awaiting maintainer "
            "feedback."
        ),
    },
]


class ReasonerLabeler(BaseLabeler):
    model_name = "reasoner"

    def __init__(
        self,
        output_dir: Path | str,
        dry_run: bool = False,
        gpt_model: str = "gpt-4o",
    ):
        super().__init__(Path(output_dir), dry_run)
        self.gpt_model = gpt_model
        self._scorer_lookup = self._load_scorer_scores()

    def _load_scorer_scores(self) -> dict[str, float]:
        """Load scorer labels from scorer_labels.jsonl.

        Returns a dict keyed by '{issue_number}_{snapshot_tier}' -> score float.
        The scorer must be run first so these labels exist.
        """
        scorer_file = self.output_dir.parent / "scorer" / "scorer_labels.jsonl"
        lookup: dict[str, float] = {}
        if not scorer_file.is_file():
            logger.warning(
                "Scorer labels not found at %s — reasoner will use score=0.0 as fallback. "
                "Run the scorer labeler first for proper risk_score injection.",
                scorer_file,
            )
            return lookup

        import json as _json
        with open(scorer_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = _json.loads(line)
                    key = f"{rec['issue_number']}_{rec['snapshot_tier']}"
                    lookup[key] = float(rec.get("label", {}).get("score", 0.0))
                except Exception:
                    continue

        logger.info("Loaded %d scorer scores from %s", len(lookup), scorer_file.name)
        return lookup

    def _inject_scorer_score(self, snapshot: dict[str, Any]) -> None:
        """Inject the scorer's risk_score into the snapshot dict."""
        issue_num = snapshot.get("ISSUE_NUMBER") or snapshot.get("issue_number", 0)
        tier = snapshot.get("SNAPSHOT_TIER") or snapshot.get("snapshot_tier", "")
        key = f"{issue_num}_{tier}"
        snapshot["scorer_score"] = self._scorer_lookup.get(key, 0.0)

    # ------------------------------------------------------------------
    # Truncation helpers (Section 4.2 / 5A.3 — 2048 token budget)
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_body(body: str, max_tokens: int = 600) -> str:
        """Truncate issue body to ~600 tokens (approx 4 chars/token)."""
        if not body:
            return ""
        max_chars = max_tokens * 4
        if len(body) > max_chars:
            return body[:max_chars] + "... [TRUNCATED]"
        return body

    @staticmethod
    def _filter_timeline(timeline: list[dict]) -> list[dict]:
        """Keep only assigned, labeled, cross-referenced, review_requested."""
        return [
            e for e in timeline
            if e.get("event") in KEEP_TIMELINE_EVENTS
        ]

    @staticmethod
    def _truncate_comments(comments: list[dict]) -> list[dict]:
        """Keep first 2 and last 2 comments, drop middle."""
        if not comments:
            return []
        sorted_comments = sorted(comments, key=lambda c: c.get("created_at", ""))
        if len(sorted_comments) <= 4:
            return sorted_comments
        return sorted_comments[:2] + sorted_comments[-2:]

    # ------------------------------------------------------------------
    # Signal extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ts(ts_val) -> datetime.datetime | None:
        if not ts_val:
            return None
        if isinstance(ts_val, datetime.datetime):
            return ts_val.replace(tzinfo=None)
        if isinstance(ts_val, str):
            try:
                dt = datetime.datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                return dt.replace(tzinfo=None)
            except Exception:
                pass
        return None

    @classmethod
    def _compute_max_comment_gap(cls, snapshot: dict) -> float:
        """Compute max gap in days between consecutive comments."""
        comments = snapshot.get("comments", [])
        timestamps = [cls._parse_ts(snapshot.get("issue", {}).get("created_at"))]
        for c in comments:
            timestamps.append(cls._parse_ts(c.get("created_at")))
        timestamps.append(cls._parse_ts(
            snapshot.get("SNAPSHOT_DATE") or snapshot.get("snapshot_date")
        ))
        parsed = sorted([t for t in timestamps if t])
        max_gap = 0.0
        for i in range(1, len(parsed)):
            gap = (parsed[i] - parsed[i - 1]).total_seconds() / 86400
            if gap > max_gap:
                max_gap = gap
        return round(max_gap, 1)

    @classmethod
    def _extract_cross_references(cls, timeline: list[dict]) -> list[str]:
        """Extract cross-referenced issue/PR numbers from timeline."""
        refs = []
        for e in timeline:
            if e.get("event") == "cross-referenced":
                src = e.get("source", {}).get("issue", {})
                num = src.get("number")
                if num:
                    is_pr = bool(src.get("pull_request"))
                    prefix = "PR" if is_pr else "Issue"
                    refs.append(f"{prefix} #{num}")
        return list(dict.fromkeys(refs))  # dedupe preserving order

    @classmethod
    def _extract_pr_review_details(cls, prs: list[dict]) -> tuple[str, str]:
        """Extract PR review bodies and inline review comments from linked PRs.

        Returns (review_bodies_str, inline_comments_str).
        """
        review_bodies = []
        inline_comments = []

        for pr in prs:
            parsed = pr.get("_parsed", {})
            pr_num = pr.get("PR_NUMBER") or parsed.get("pr_number", "?")

            # Full review bodies (especially CHANGES_REQUESTED)
            for review in parsed.get("reviews", []):
                body = review.get("body", "")
                state = review.get("state", "")
                user = review.get("user", {}).get("login", "?")
                if body and body.strip():
                    clean = BaseLabeler._strip_closure_signals(body.strip())
                    review_bodies.append(
                        f"[PR #{pr_num} — {state} by {user}]: {clean[:300]}"
                    )

            # Inline code-level comments from /pulls/{number}/comments
            for comment in parsed.get("review_comments", []):
                body = comment.get("body", "")
                path = comment.get("path", "")
                line = comment.get("line") or comment.get("original_line", "")
                user = comment.get("user", {}).get("login", "?")
                if body and body.strip():
                    clean = BaseLabeler._strip_closure_signals(body.strip())
                    inline_comments.append(
                        f"[{user} on {path}:{line}]: {clean[:200]}"
                    )

        return "\n".join(review_bodies), "\n".join(inline_comments)

    # ------------------------------------------------------------------
    # Build input prompt (Qwen training format — Section 5A.5)
    # ------------------------------------------------------------------

    def _build_input(self, snapshot: dict) -> str:
        """Build the structured issue text for both GPT-4o labelling and Qwen training."""
        issue = snapshot.get("issue", {})
        title = issue.get("title", "")
        body = self._strip_closure_signals(issue.get("body", ""))
        body = self._truncate_body(body, max_tokens=600)

        labels = [lb.get("name") for lb in issue.get("labels", [])]
        assignees = [a.get("login") for a in issue.get("assignees", [])]
        days_open = snapshot.get("days_open_at_snapshot") or snapshot.get("DAYS_OPEN") or 0
        score_val = snapshot.get("scorer_score", 0.0)
        score_band = "high" if score_val >= 0.65 else "medium" if score_val >= 0.35 else "low"

        # Timeline — filtered to keep only relevant events
        timeline = snapshot.get("timeline", [])
        filtered_timeline = self._filter_timeline(timeline)

        # Reassignment count
        reassignments = sum(1 for e in timeline if e.get("event") == "unassigned")

        # Cross-references from timeline
        cross_refs = self._extract_cross_references(filtered_timeline)

        # Comments — truncated: first 2 + last 2
        comments = snapshot.get("comments", [])
        max_gap = self._compute_max_comment_gap(snapshot)
        kept_comments = self._truncate_comments(comments)

        comment_str = ""
        for c in kept_comments:
            clean = self._strip_closure_signals(str(c.get("body") or ""))
            user = c.get("user", {}).get("login", "?")
            ts = c.get("created_at", "")
            comment_str += f"[{ts}] {user}: {clean[:150]}...\n"

        if len(comments) > 4:
            comment_str = (
                comment_str.split("\n", 2)[0] + "\n"
                + comment_str.split("\n", 2)[1] + "\n"
                + "... [MIDDLE COMMENTS SKIPPED] ...\n"
                + "\n".join(comment_str.strip().split("\n")[-2:]) + "\n"
            ) if len(kept_comments) == 4 else comment_str

        # PR details including review bodies and inline comments
        prs = snapshot.get("linked_prs", [])
        review_bodies_str, inline_comments_str = self._extract_pr_review_details(prs)

        pr_summary_lines = []
        for pr in prs:
            parsed = pr.get("_parsed", {})
            pr_num = pr.get("PR_NUMBER") or parsed.get("pr_number", "?")
            pr_state = pr.get("STATE", str(parsed.get("pr", {}).get("state", "unknown")))
            review_count = pr.get("REVIEW_COUNT", 0)

            # CI status
            check_runs = parsed.get("check_runs", [])
            conclusions = [cr.get("conclusion") for cr in check_runs if cr.get("conclusion")]
            ci_failed = any(c in ("failure", "timed_out", "cancelled") for c in conclusions)
            ci_label = "failure" if ci_failed else ("success" if conclusions else "none")

            # Silent reviewers
            silent = parsed.get("silent_reviewers", [])

            pr_summary_lines.append(
                f"PR #{pr_num} | State: {pr_state} | Reviews: {review_count} "
                f"| CI: {ci_label} | Silent reviewers: {len(silent)}"
            )

        # Stage label
        stage_label = "EARLY" if days_open <= 7 else "LATE"

        # Build the full structured text
        prompt = (
            f"Issue #{snapshot.get('ISSUE_NUMBER') or snapshot.get('issue_number', '?')} "
            f"— Snapshot at T+{days_open} days [{stage_label} SNAPSHOT]\n"
            f"Title: {title}\n"
            f"Risk Score: {score_val:.2f} ({score_band})\n"
            f"Labels: {', '.join(labels) if labels else 'none'}\n"
            f"Assignees: {', '.join(assignees) if assignees else 'unassigned'} ({len(assignees)})\n"
            f"Days open: {days_open}\n"
            f"PROJECT: apache/airflow | P50={P50_DAYS}d P75={P75_DAYS}d P90={P90_DAYS}d P95={P95_DAYS}d\n"
            f"Reassignments: {reassignments}\n"
            f"Max Comment Gap: {max_gap} days\n"
            f"Cross-referenced: {', '.join(cross_refs) if cross_refs else 'none'}\n"
            f"Linked PRs: {chr(10).join(pr_summary_lines) if pr_summary_lines else 'None'}\n"
        )

        if review_bodies_str:
            prompt += f"PR Review Feedback:\n{review_bodies_str[:600]}\n"

        if inline_comments_str:
            prompt += f"Inline Code Comments:\n{inline_comments_str[:400]}\n"

        prompt += (
            f"BODY: {body}\n"
            f"COMMENTS:\n{comment_str.strip()}"
        )

        return prompt

    # ------------------------------------------------------------------
    # Label methods
    # ------------------------------------------------------------------

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._inject_scorer_score(snapshot)
        narrative = self._generate_narrative(snapshot)
        return {"narrative": narrative}

    async def async_label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._inject_scorer_score(snapshot)
        narrative = await self._async_generate_narrative(snapshot)
        return {"narrative": narrative}

    def _generate_narrative(self, snapshot: dict[str, Any]) -> str:
        sys_msg, user_msg = self._build_gpt_messages(snapshot)
        return call_gpt4o(
            messages=[
                {"role": "system", "content": sys_msg},
                *FEW_SHOT_EXAMPLES,
                {"role": "user", "content": user_msg},
            ],
            model=self.gpt_model,
            temperature=0.0,
        )

    async def _async_generate_narrative(self, snapshot: dict[str, Any]) -> str:
        sys_msg, user_msg = self._build_gpt_messages(snapshot)
        return await async_call_gpt4o(
            messages=[
                {"role": "system", "content": sys_msg},
                *FEW_SHOT_EXAMPLES,
                {"role": "user", "content": user_msg},
            ],
            model=self.gpt_model,
            temperature=0.0,
        )

    def _build_gpt_messages(self, snapshot: dict) -> tuple[str, str]:
        """Build (system_prompt, user_message) for GPT-4o teacher labelling."""
        score_val = snapshot.get("scorer_score", 0.0)
        score_band = "high" if score_val >= 0.65 else "medium" if score_val >= 0.35 else "low"

        sys_msg = SYSTEM_PROMPT_TEACHER.format(
            p50=P50_DAYS, p75=P75_DAYS, p90=P90_DAYS, p95=P95_DAYS,
            score=round(score_val, 2), band=score_band,
        )

        issue_data = self._build_input(snapshot)
        user_msg = f"ISSUE DATA:\n{issue_data}\n\nWrite a 2-3 sentence diagnostic narrative for this issue."

        return sys_msg, user_msg


# ---------------------------------------------------------------------------
# CLI: python -m labelers.reasoner [--limit N] [--concurrency N] [--dry-run]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import asyncio
    import time

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="Run reasoner labelling on CSV data.")
    parser.add_argument("--limit", type=int, default=None, help="Max rows (for testing)")
    parser.add_argument("--concurrency", type=int, default=5, help="Async concurrency (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output files")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory")
    parser.add_argument("--model", type=str, default="gpt-4o", help="GPT model (default: gpt-4o)")
    args = parser.parse_args()

    _base = Path(__file__).resolve().parent.parent
    output_dir = Path(args.output_dir) if args.output_dir else _base / "data" / "labeled"

    reasoner = ReasonerLabeler(
        output_dir=output_dir,
        dry_run=args.dry_run,
        gpt_model=args.model,
    )

    start = time.time()
    counts = asyncio.run(reasoner.async_label_all_csv(
        limit=args.limit,
        concurrency=args.concurrency,
    ))
    elapsed = time.time() - start
    logger.info(
        "DONE — labeled: %d, skipped: %d, errors: %d — %.1fs (%.1f min)",
        counts["labeled"], counts["skipped"], counts["errors"],
        elapsed, elapsed / 60,
    )
