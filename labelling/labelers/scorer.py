"""Scorer labeler — Model 1 (v2 — CSV-based, 3-band approach).

Label strategy:
  60% weight -> Programmatic bottleneck score (v5 formula).
  40% weight -> GPT-4o semantic adjustment offset [-0.3, +0.3].

Output: {"score": float 0.0–1.0, "band": 0/1/2, "band_name": low/medium/high}
  3 bands: Low (0.0–0.35), Medium (0.35–0.65), High (0.65–1.0)
"""

import logging
import re
import datetime
import hashlib
from pathlib import Path
from typing import Any
from .base import BaseLabeler, call_gpt4o, async_call_gpt4o

import pandas as pd

logger = logging.getLogger(__name__)

# Project percentiles (corrected)
P50_DAYS = 1.0
P75_DAYS = 5.0
P90_DAYS = 22.0
P95_DAYS = 44.0

# 3-band thresholds
BAND_THRESHOLDS = [0.35, 0.65]
BAND_NAMES = ["low", "medium", "high"]


class ScorerLabeler(BaseLabeler):
    model_name = "scorer"

    def __init__(self, output_dir: Path | str, dry_run: bool = False,
                 gpt4o_fraction: float = 1.0, gpt_model: str = "gpt-4o"):
        super().__init__(Path(output_dir), dry_run)
        self.gpt4o_fraction = gpt4o_fraction
        self.gpt_model = gpt_model

    # ------------------------------------------------------------------
    # Build input prompt (Qwen training format, Section 5A.5 V5)
    # ------------------------------------------------------------------

    def _build_input(self, snapshot: dict) -> str:
        issue = snapshot.get("issue", {})
        title = issue.get("title", "")
        body = self._strip_closure_signals(issue.get("body", ""))
        if body and len(body) > 1600:
            body = body[:1600] + "... [TRUNCATED]"

        labels = [lb.get("name") for lb in issue.get("labels", [])]
        assignees = [a.get("login") for a in issue.get("assignees", [])]
        days_open = snapshot.get("days_open_at_snapshot") or snapshot.get("DAYS_OPEN") or 0
        snapshot_tier = snapshot.get("SNAPSHOT_TIER") or snapshot.get("snapshot_tier") or "unknown"

        # Comments (first 3, last 3)
        comments = snapshot.get("comments", [])
        if comments:
            comments = sorted(comments, key=lambda c: c.get("created_at", ""))

        comments_str = ""
        if len(comments) <= 6:
            for c in comments:
                clean_body = self._strip_closure_signals(str(c.get("body", "")))
                comments_str += f"- [{c.get('created_at')}] {c.get('user', {}).get('login')}: {clean_body[:100]}...\n"
        else:
            for c in comments[:3]:
                clean_body = self._strip_closure_signals(str(c.get("body", "")))
                comments_str += f"- [{c.get('created_at')}] {c.get('user', {}).get('login')}: {clean_body[:100]}...\n"
            comments_str += "... [MIDDLE COMMENTS SKIPPED] ...\n"
            for c in comments[-3:]:
                clean_body = self._strip_closure_signals(str(c.get("body", "")))
                comments_str += f"- [{c.get('created_at')}] {c.get('user', {}).get('login')}: {clean_body[:100]}...\n"

        # PR summaries
        prs = snapshot.get("linked_prs", [])
        pr_states = []
        pr_str = ""
        silent_reviewer_count = 0
        ci_failed = False
        for pr in prs:
            parsed = pr.get("_parsed", {})
            pr_state = pr.get("STATE", str(parsed.get("pr", {}).get("state", "unknown")))
            pr_states.append(pr_state)
            review_count = pr.get("REVIEW_COUNT", 0)

            # Silent reviewers
            silent_reviewer_count += len(parsed.get("silent_reviewers", []))

            # CI
            check_runs = parsed.get("check_runs", [])
            conclusions = [cr.get("conclusion") for cr in check_runs if cr.get("conclusion")]
            if any(c in ("failure", "timed_out", "cancelled") for c in conclusions):
                ci_failed = True
            ci_label = "failure" if ci_failed else ("success" if conclusions else "none")

            pr_str += f"PR #{pr.get('PR_NUMBER', '?')} | State: {pr_state} | Reviews: {review_count} | CI: {ci_label}\n"

        gap = self._signal_max_comment_gap(snapshot)
        assign_changes = self._signal_assignee_change(snapshot)

        prompt = (
            f"PROJECT: apache/airflow | P50={P50_DAYS:.0f}d P75={P75_DAYS:.0f}d P90={P90_DAYS:.0f}d P95={P95_DAYS:.0f}d\n"
            f"ISSUE: {title} | LABELS: {', '.join(labels)} | ASSIGNEES: {len(assignees)} "
            f"| DAYS_OPEN: {days_open} | SNAPSHOT_TIER: {snapshot_tier} "
            f"| COMMENT_COUNT: {len(comments)} "
            f"| MAX_COMMENT_GAP_DAYS: {gap} | LINKED_PR_COUNT: {len(prs)} "
            f"| PR_STATES: {', '.join(pr_states) if pr_states else 'none'} "
            f"| SILENT_REVIEWERS: {silent_reviewer_count} "
            f"| CI: {'failure' if ci_failed else 'pass' if prs else 'none'}\n"
            f"BODY: {body}\n"
            f"COMMENTS: {comments_str.strip()}"
        )
        return prompt

    # ------------------------------------------------------------------
    # Label methods
    # ------------------------------------------------------------------

    def _score_to_band(self, score: float) -> tuple[int, str]:
        """Map float score to 3-band: 0=low, 1=medium, 2=high."""
        if score < BAND_THRESHOLDS[0]:
            return 0, BAND_NAMES[0]
        elif score < BAND_THRESHOLDS[1]:
            return 1, BAND_NAMES[1]
        else:
            return 2, BAND_NAMES[2]

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        prog_score = self._programmatic_score(snapshot)
        use_gpt = self._should_use_gpt(snapshot)

        if use_gpt:
            final_score, reasoning, adjustment = self._semantic_score(snapshot, prog_score)
        else:
            final_score = prog_score
            adjustment = 0.0
            reasoning = None

        band, band_name = self._score_to_band(final_score)

        return {
            "score": round(final_score, 4),
            "band": band,
            "band_name": band_name,
            "programmatic_score": round(prog_score, 4),
            "gpt4o_adjustment": round(adjustment, 4),
            "label_source": "gpt4o_hybrid" if use_gpt else "programmatic",
            "reasoning": reasoning,
        }

    async def async_label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        prog_score = self._programmatic_score(snapshot)
        use_gpt = self._should_use_gpt(snapshot)

        if use_gpt:
            final_score, reasoning, adjustment = await self._async_semantic_score(snapshot, prog_score)
        else:
            final_score = prog_score
            adjustment = 0.0
            reasoning = None

        band, band_name = self._score_to_band(final_score)

        return {
            "score": round(final_score, 4),
            "band": band,
            "band_name": band_name,
            "programmatic_score": round(prog_score, 4),
            "gpt4o_adjustment": round(adjustment, 4),
            "label_source": "gpt4o_hybrid" if use_gpt else "programmatic",
            "reasoning": reasoning,
        }

    # ------------------------------------------------------------------
    # Programmatic score (60% weight) — v5 formula
    # ------------------------------------------------------------------

    def _programmatic_score(self, snapshot: dict[str, Any]) -> float:
        score = 0.0
        score += self._signal_resolution_days(snapshot)
        score += self._signal_days_to_first_review(snapshot)
        score += self._signal_review_cycles(snapshot)
        score += self._signal_closed_without_merge(snapshot)
        score += self._signal_max_comment_gap(snapshot)
        score += self._signal_assignee_change(snapshot)
        score += self._signal_requested_no_review(snapshot)
        score += self._signal_has_sub_issues(snapshot)
        score += self._signal_ci_failed(snapshot)
        return min(max(score, 0.0), 1.0)

    @staticmethod
    def _parse_ts(ts_val) -> datetime.datetime | None:
        if not ts_val:
            return None
        if isinstance(ts_val, datetime.datetime):
            return ts_val.replace(tzinfo=None)
        if isinstance(ts_val, str):
            try:
                dt = datetime.datetime.fromisoformat(ts_val.replace('Z', '+00:00'))
                return dt.replace(tzinfo=None)
            except Exception:
                pass
        return None

    @classmethod
    def _signal_resolution_days(cls, snapshot: dict) -> float:
        """v5 formula: baseline = min(days_open / P90, 1.0) * 0.4"""
        days = snapshot.get("days_open_at_snapshot") or snapshot.get("DAYS_OPEN") or snapshot.get("days_open")
        if days is None:
            return 0.0
        days = float(days)
        return round(min(days / P90_DAYS, 1.0) * 0.4, 4)

    @classmethod
    def _signal_days_to_first_review(cls, snapshot: dict) -> float:
        prs = snapshot.get("linked_prs", [])
        for pr in prs:
            parsed = pr.get("_parsed", {})
            pr_created = cls._parse_ts(pr.get("CREATED_AT"))
            if not pr_created:
                continue
            reviews = parsed.get("reviews", [])
            first_review_dt = None
            for r in reviews:
                sub_at = cls._parse_ts(r.get("submitted_at"))
                if sub_at and (not first_review_dt or sub_at < first_review_dt):
                    first_review_dt = sub_at
            if first_review_dt and (first_review_dt - pr_created).days > 7:
                return 0.15
        return 0.0

    @staticmethod
    def _signal_review_cycles(snapshot: dict) -> float:
        prs = snapshot.get("linked_prs", [])
        if any(pr.get("REVIEW_COUNT", 0) > 3 for pr in prs):
            return 0.10
        return 0.0

    @staticmethod
    def _signal_closed_without_merge(snapshot: dict) -> float:
        prs = snapshot.get("linked_prs", [])
        for pr in prs:
            if str(pr.get("STATE", "")).lower() == "closed" and not pr.get("IS_MERGED"):
                return 0.15
        return 0.0

    @classmethod
    def _signal_max_comment_gap(cls, snapshot: dict) -> float:
        comments = snapshot.get("comments", [])
        timestamps = [cls._parse_ts(snapshot.get("issue", {}).get("created_at"))]
        for c in comments:
            timestamps.append(cls._parse_ts(c.get("created_at")))
        # Use SNAPSHOT_DATE as endpoint
        timestamps.append(cls._parse_ts(
            snapshot.get("SNAPSHOT_DATE") or snapshot.get("snapshot_date")
        ))

        parsed = sorted([t for t in timestamps if t])
        max_gap = 0
        for i in range(1, len(parsed)):
            gap = (parsed[i] - parsed[i - 1]).days
            if gap > max_gap:
                max_gap = gap

        return 0.15 if max_gap > 14 else 0.0

    @staticmethod
    def _signal_assignee_change(snapshot: dict) -> float:
        timeline = snapshot.get("timeline", [])
        assign_events = [e for e in timeline if e.get("event") in ("assigned", "unassigned")]
        return 0.10 if len(assign_events) > 1 else 0.0

    @staticmethod
    def _signal_requested_no_review(snapshot: dict) -> float:
        prs = snapshot.get("linked_prs", [])
        for pr in prs:
            parsed = pr.get("_parsed", {})
            silent = parsed.get("silent_reviewers", [])
            if silent:
                return 0.10
        return 0.0

    @staticmethod
    def _signal_has_sub_issues(snapshot: dict) -> float:
        body = snapshot.get("issue", {}).get("body", "")
        if not body:
            return 0.0
        if "- [ ]" in body or "- [x]" in body:
            return 0.05
        return 0.0
    @staticmethod
    def _signal_ci_failed(snapshot: dict) -> float:
        """CI failure on any linked PR (not mixed — only explicit failure/timed_out/cancelled)."""
        prs = snapshot.get("linked_prs", [])
        for pr in prs:
            parsed = pr.get("_parsed", {})
            check_runs = parsed.get("check_runs", [])
            conclusions = [cr.get("conclusion") for cr in check_runs if cr.get("conclusion")]
            if not conclusions:
                continue
            has_failure = any(c in ("failure", "timed_out", "cancelled") for c in conclusions)
            has_success = any(c in ("success", "neutral", "skipped") for c in conclusions)
            # Only fire on pure failure — not mixed results
            if has_failure and not has_success:
                return 0.15
        return 0.0


    # ------------------------------------------------------------------
    # GPT-4o semantic refinement (40% weight)
    # ------------------------------------------------------------------

    def _should_use_gpt(self, snapshot: dict[str, Any]) -> bool:
        issue_num = str(snapshot.get("ISSUE_NUMBER") or snapshot.get("issue_number", 0))
        digest = int(hashlib.md5(issue_num.encode()).hexdigest(), 16)
        return (digest % 100) < int(self.gpt4o_fraction * 100)

    def _build_gpt_prompt(self, snapshot: dict, prog_score: float) -> tuple[str, str]:
        """Build system + user messages for GPT semantic adjustment."""
        issue = snapshot.get("issue", {})
        prs = snapshot.get("linked_prs", [])

        pr_summaries = []
        for pr in prs:
            pr_summaries.append(f"PR #{pr.get('PR_NUMBER', '?')}: {pr.get('PR_TITLE', '')} ({pr.get('STATE', '')})")

        days_open = snapshot.get("days_open_at_snapshot") or snapshot.get("DAYS_OPEN") or 14
        snapshot_tier = snapshot.get("SNAPSHOT_TIER") or snapshot.get("snapshot_tier") or "unknown"

        user_msg = f"""[ISSUE SNAPSHOT]
Issue #{snapshot.get('ISSUE_NUMBER', '?')}
Title: {issue.get('title', '')}
Linked PRs: {', '.join(pr_summaries) if pr_summaries else 'None'}

Issue body:
{self._truncate(issue.get('body', ''), 1500)}"""

        tier_guidance = (
            "An early snapshot — weight lack of triage, missing assignee, and silence more heavily."
            if days_open <= 15
            else "A later snapshot — weight prolonged inactivity, review delays, and unresolved discussions more heavily."
        )

        sys_msg = f"""You are a senior engineering project analyst scoring GitHub issue bottleneck risk for Apache Airflow.

Project resolution stats: P50={P50_DAYS} day, P75={P75_DAYS} days, P90={P90_DAYS} days, P95={P95_DAYS} days.
Programmatic bottleneck score (60% weight): {prog_score:.2f}

This is a {snapshot_tier} snapshot — the issue has been open for {days_open} days.
{tier_guidance}

Based ONLY on the language and semantics of the issue text below (not the programmatic score), output a single float between -0.3 and +0.3 representing how much the text signals MORE or LESS risk than the programmatic score suggests. Then output one sentence explaining why. Be specific — reference actual words or phrases from the issue.

Format exactly: ADJUSTMENT: <float> REASON: <one sentence>

ISSUE SNAPSHOT (T+{days_open} days):"""

        return sys_msg, user_msg

    def _parse_gpt_response(self, raw: str, prog_score: float) -> tuple[float, str, float]:
        adj_val = 0.0
        reasoning = "fallback"

        adj_match = re.search(r"ADJUSTMENT:\s*([+-]?\d*\.?\d+)", raw)
        reason_match = re.search(r"REASON:\s*(.*)", raw)

        if adj_match:
            try:
                adj_val = float(adj_match.group(1))
                adj_val = max(min(adj_val, 0.3), -0.3)
            except ValueError:
                pass
        if reason_match:
            reasoning = reason_match.group(1).strip()

        final_score = 0.6 * prog_score + 0.4 * (prog_score + adj_val)
        return min(max(final_score, 0.0), 1.0), reasoning, adj_val

    def _semantic_score(self, snapshot: dict, prog_score: float) -> tuple[float, str, float]:
        sys_msg, user_msg = self._build_gpt_prompt(snapshot, prog_score)
        raw = call_gpt4o(
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            model=self.gpt_model, temperature=0.0,
        )
        return self._parse_gpt_response(raw, prog_score)

    async def _async_semantic_score(self, snapshot: dict, prog_score: float) -> tuple[float, str, float]:
        sys_msg, user_msg = self._build_gpt_prompt(snapshot, prog_score)
        raw = await async_call_gpt4o(
            messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
            model=self.gpt_model, temperature=0.0,
        )
        return self._parse_gpt_response(raw, prog_score)


# ---------------------------------------------------------------------------
# CLI: python -m labelers.scorer [--limit N] [--concurrency N] [--dry-run]
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

    parser = argparse.ArgumentParser(description="Run scorer labelling on CSV data.")
    parser.add_argument("--limit", type=int, default=None, help="Max rows (for testing)")
    parser.add_argument("--concurrency", type=int, default=5, help="Async concurrency (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output files")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory")
    parser.add_argument("--model", type=str, default="gpt-4o", help="GPT model (default: gpt-4o)")
    args = parser.parse_args()

    _base = Path(__file__).resolve().parent.parent
    output_dir = Path(args.output_dir) if args.output_dir else _base / "data" / "labeled"

    scorer = ScorerLabeler(
        output_dir=output_dir,
        dry_run=args.dry_run,
        gpt_model=args.model,
    )

    start = time.time()
    counts = asyncio.run(scorer.async_label_all_csv(
        limit=args.limit,
        concurrency=args.concurrency,
    ))
    elapsed = time.time() - start
    logger.info(
        "DONE — labeled: %d, skipped: %d, errors: %d — %.1fs (%.1f min)",
        counts["labeled"], counts["skipped"], counts["errors"],
        elapsed, elapsed / 60,
    )
