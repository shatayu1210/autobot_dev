"""Reasoner labeler — Model 2 (Qwen2.5-7B-Instruct + LoRA).

Data flow:
  1. Read scorer_labels.jsonl → filter to medium + high band (~2,579 records)
  2. For each record, enrich with additional fields from the raw CSVs:
     - pr_review_comments (inline code-level comments)
     - PR review bodies (especially CHANGES_REQUESTED)
     - timeline cross-references
  3. Call GPT-4o to generate a 2–3 sentence narrative
  4. Write to reasoner_labels.jsonl

Per Section 4.2 / 5.5 / 5A.5 of AutoBot_Ref_v5.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd

from .base import (
    BaseLabeler,
    async_call_gpt4o,
    call_gpt4o,
    load_issues_csv,
    load_pr_lookup,
)

logger = logging.getLogger(__name__)

# Project percentiles (Section 5A.1) — must match scorer.py
P50_DAYS = 1
P75_DAYS = 5
P90_DAYS = 22
P95_DAYS = 44

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

    # ------------------------------------------------------------------
    # Load scorer records
    # ------------------------------------------------------------------

    def _load_scorer_records(self) -> list[dict]:
        """Load scorer_labels.jsonl, filter to medium + high band."""
        scorer_file = self.output_dir.parent / "scorer" / "scorer_labels.jsonl"
        if not scorer_file.is_file():
            raise FileNotFoundError(
                f"Scorer labels not found at {scorer_file}. "
                "Run the scorer labeler first."
            )

        records = []
        skipped_low = 0
        with open(scorer_file, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    band = rec.get("label", {}).get("band", 0)
                    if band >= 1:  # medium + high only
                        records.append(rec)
                    else:
                        skipped_low += 1
                except Exception:
                    continue

        logger.info(
            "Loaded %d medium+high scorer records (skipped %d low) from %s",
            len(records), skipped_low, scorer_file.name,
        )
        return records

    # ------------------------------------------------------------------
    # Enrich scorer input with additional fields from raw CSVs
    # (Section 4.2: pr_review_comments, review bodies, timeline xrefs)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_pr_review_bodies(prs: list[dict]) -> str:
        """Extract full review bodies, especially CHANGES_REQUESTED."""
        lines = []
        for pr in prs:
            parsed = pr.get("_parsed", {})
            pr_num = pr.get("PR_NUMBER") or parsed.get("pr_number", "?")
            for review in parsed.get("reviews", []):
                body = (review.get("body") or "").strip()
                state = review.get("state", "")
                user = review.get("user", {}).get("login", "?")
                if body:
                    clean = BaseLabeler._strip_closure_signals(body)
                    lines.append(f"[PR #{pr_num} {state} by {user}]: {clean[:300]}")
        return "\n".join(lines)

    @staticmethod
    def _extract_pr_review_comments(prs: list[dict]) -> str:
        """Extract inline code-level review comments from /pulls/{number}/comments."""
        lines = []
        for pr in prs:
            parsed = pr.get("_parsed", {})
            pr_num = pr.get("PR_NUMBER") or parsed.get("pr_number", "?")
            for c in parsed.get("review_comments", []):
                body = (c.get("body") or "").strip()
                path = c.get("path", "")
                line_num = c.get("line") or c.get("original_line", "")
                user = c.get("user", {}).get("login", "?")
                if body:
                    clean = BaseLabeler._strip_closure_signals(body)
                    lines.append(f"[{user} on {path}:{line_num}]: {clean[:200]}")
        return "\n".join(lines)

    @staticmethod
    def _extract_timeline_cross_refs(timeline: list[dict]) -> str:
        """Extract cross-referenced issues/PRs from timeline."""
        refs = []
        for e in timeline:
            if e.get("event") != "cross-referenced":
                continue
            src = e.get("source", {}).get("issue", {})
            num = src.get("number")
            if not num:
                continue
            title = src.get("title", "")
            is_pr = bool(src.get("pull_request"))
            prefix = "PR" if is_pr else "Issue"
            refs.append(f"{prefix} #{num}: {title[:80]}")
        # dedupe preserving order
        seen = set()
        unique = []
        for r in refs:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        return "\n".join(unique)

    def _enrich_input(
        self,
        scorer_input: str,
        score: float,
        band_name: str,
        issue_num: int,
        snapshot_tier: str,
        pr_lookup: dict[int, list[dict]],
        snapshot_lookup: dict[str, dict],
    ) -> str:
        """Build the full reasoner input: scorer input + additional fields.

        Adds risk_score, PR review bodies, PR inline comments, and
        timeline cross-references on top of the scorer's structured text.
        """
        parts = [f"Risk Score: {score:.2f} ({band_name})\n"]
        parts.append(scorer_input)

        # Get linked PRs for this issue from raw_prs.csv
        linked_prs = pr_lookup.get(issue_num, [])

        # Filter PRs by snapshot date to prevent leakage
        snap_data = snapshot_lookup.get(f"{issue_num}_{snapshot_tier}")
        snapshot_date = None
        if snap_data:
            snapshot_date = pd.to_datetime(
                snap_data.get("SNAPSHOT_DATE"), utc=True
            )
            if snapshot_date and snapshot_date.tzinfo:
                snapshot_date = snapshot_date.tz_convert(None)

        filtered_prs = []
        for pr in linked_prs:
            pr_created = pd.to_datetime(pr.get("CREATED_AT"), utc=True)
            if pr_created and pr_created.tzinfo:
                pr_created = pr_created.tz_convert(None)
            if snapshot_date is not None and pr_created is not None and pr_created <= snapshot_date:
                filtered_prs.append(pr)

        # Additional input 1: PR review bodies (CHANGES_REQUESTED etc.)
        review_bodies = self._extract_pr_review_bodies(filtered_prs)
        if review_bodies:
            parts.append(f"\nPR Review Feedback:\n{review_bodies[:600]}")

        # Additional input 2: PR inline review comments
        review_comments = self._extract_pr_review_comments(filtered_prs)
        if review_comments:
            parts.append(f"\nInline Code Review Comments:\n{review_comments[:400]}")

        # Additional input 3: Timeline cross-references
        if snap_data:
            raw_json_str = snap_data.get("RAW_JSON_SNAPSHOT", "{}")
            if pd.isna(raw_json_str):
                raw_json_str = "{}"
            try:
                raw_snap = json.loads(raw_json_str)
            except Exception:
                raw_snap = {}
            timeline = raw_snap.get("timeline", [])
            xrefs = self._extract_timeline_cross_refs(timeline)
            if xrefs:
                parts.append(f"\nCross-Referenced Issues/PRs:\n{xrefs[:400]}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # GPT-4o message builder
    # ------------------------------------------------------------------

    def _build_gpt_messages(self, enriched_input: str, score: float, band_name: str) -> list[dict]:
        sys_msg = SYSTEM_PROMPT_TEACHER.format(
            p50=P50_DAYS, p75=P75_DAYS, p90=P90_DAYS, p95=P95_DAYS,
            score=round(score, 2), band=band_name,
        )
        user_msg = (
            f"ISSUE DATA:\n{enriched_input}\n\n"
            f"Write a 2-3 sentence diagnostic narrative for this issue."
        )
        return [
            {"role": "system", "content": sys_msg},
            *FEW_SHOT_EXAMPLES,
            {"role": "user", "content": user_msg},
        ]

    # ------------------------------------------------------------------
    # Main labelling loop
    # ------------------------------------------------------------------

    async def async_label_all_from_scorer(
        self,
        limit: int | None = None,
        concurrency: int = 10,
    ) -> dict[str, int]:
        """Label medium+high scorer records with GPT-4o narratives.

        Reads scorer_labels.jsonl + raw CSVs for enrichment.
        """
        from tqdm import tqdm

        counts = {"labeled": 0, "skipped": 0, "errors": 0}
        output_file = self.output_dir / f"{self.model_name}_labels.jsonl"

        # Dedup / resume
        processed_keys: set[str] = set()
        if output_file.exists():
            with open(output_file, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            rec = json.loads(line)
                            key = f"{rec['issue_number']}_{rec['snapshot_tier']}"
                            processed_keys.add(key)
                        except Exception:
                            pass

        # Load scorer records (medium + high)
        all_records = self._load_scorer_records()
        if limit:
            all_records = all_records[:limit]

        # Load raw CSVs for enrichment
        logger.info("Loading raw CSVs for PR review + timeline enrichment...")
        pr_lookup = load_pr_lookup()

        issues_df = load_issues_csv()
        snapshot_lookup: dict[str, dict] = {}
        for _, row in issues_df.iterrows():
            key = f"{int(row['ISSUE_NUMBER'])}_{row['SNAPSHOT_TIER']}"
            snapshot_lookup[key] = row.to_dict()

        # Filter out already-processed
        todo = []
        for rec in all_records:
            key = f"{rec['issue_number']}_{rec['snapshot_tier']}"
            if key in processed_keys:
                counts["skipped"] += 1
            else:
                todo.append(rec)

        total = len(todo) + counts["skipped"]
        pbar = tqdm(
            total=total,
            desc="Labelling reasoner",
            unit="issue",
            initial=counts["skipped"],
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        )

        sem = asyncio.Semaphore(concurrency)

        async def _label_one(scorer_rec: dict):
            async with sem:
                issue_num = scorer_rec["issue_number"]
                tier = scorer_rec["snapshot_tier"]
                label_data = scorer_rec.get("label", {})
                score = label_data.get("score", 0.0)
                band_name = label_data.get("band_name", "medium")

                try:
                    # Enrich scorer input with PR reviews + timeline xrefs
                    enriched = self._enrich_input(
                        scorer_input=scorer_rec.get("input", ""),
                        score=score,
                        band_name=band_name,
                        issue_num=issue_num,
                        snapshot_tier=tier,
                        pr_lookup=pr_lookup,
                        snapshot_lookup=snapshot_lookup,
                    )

                    messages = self._build_gpt_messages(enriched, score, band_name)
                    narrative = await async_call_gpt4o(
                        messages=messages,
                        model=self.gpt_model,
                        temperature=0.0,
                    )
                    pbar.update(1)
                    return (issue_num, tier, scorer_rec, enriched, narrative)
                except Exception as exc:
                    logger.error("Error labeling %s (%s): %s", issue_num, tier, exc)
                    pbar.update(1)
                    return (issue_num, tier, scorer_rec, None, None)

        tasks = [_label_one(rec) for rec in todo]
        results = await asyncio.gather(*tasks)

        # Write results
        with open(output_file, "a") as f:
            for issue_num, tier, scorer_rec, enriched, narrative in results:
                if narrative is None:
                    counts["errors"] += 1
                    continue

                label_data = scorer_rec.get("label", {})
                record = {
                    "issue_number": issue_num,
                    "snapshot_tier": tier,
                    "model": self.model_name,
                    "input": enriched,
                    "scorer_score": label_data.get("score", 0.0),
                    "scorer_band": label_data.get("band_name", ""),
                    "label": {"narrative": narrative},
                }
                if not self.dry_run:
                    f.write(json.dumps(record, default=str) + "\n")
                counts["labeled"] += 1

        pbar.close()
        logger.info(
            "Complete — labeled: %d, skipped: %d, errors: %d",
            counts["labeled"], counts["skipped"], counts["errors"],
        )
        return counts

    # ------------------------------------------------------------------
    # Interface stubs (reasoner uses async_label_all_from_scorer instead)
    # ------------------------------------------------------------------

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "ReasonerLabeler uses async_label_all_from_scorer(). "
            "It reads scorer_labels.jsonl, not raw snapshots."
        )

    async def async_label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "ReasonerLabeler uses async_label_all_from_scorer(). "
            "It reads scorer_labels.jsonl, not raw snapshots."
        )


# ---------------------------------------------------------------------------
# CLI: python -m labelers.reasoner [--limit N] [--concurrency N] [--dry-run]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="Run reasoner labelling from scorer output.")
    parser.add_argument("--limit", type=int, default=None, help="Max records (for testing)")
    parser.add_argument("--concurrency", type=int, default=5, help="Async concurrency")
    parser.add_argument("--dry-run", action="store_true", help="Don't write output files")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory")
    parser.add_argument("--model", type=str, default="gpt-4o", help="GPT model")
    args = parser.parse_args()

    _base = Path(__file__).resolve().parent.parent
    output_dir = Path(args.output_dir) if args.output_dir else _base / "data" / "labeled"

    reasoner = ReasonerLabeler(
        output_dir=output_dir,
        dry_run=args.dry_run,
        gpt_model=args.model,
    )

    start = time.time()
    counts = asyncio.run(reasoner.async_label_all_from_scorer(
        limit=args.limit,
        concurrency=args.concurrency,
    ))
    elapsed = time.time() - start
    logger.info(
        "DONE — labeled: %d, skipped: %d, errors: %d — %.1fs (%.1f min)",
        counts["labeled"], counts["skipped"], counts["errors"],
        elapsed, elapsed / 60,
    )
