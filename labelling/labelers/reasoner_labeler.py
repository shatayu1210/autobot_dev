"""Reasoner labeler — Model 2 (Qwen2.5-7B-Instruct + LoRA).

v3 changes vs v2 (spot-check fixes):
  - Sentence counter fixed: now splits only on sentence boundaries
    (period + space + capital), not on decimals (0.68), version strings
    (3.0.2), or dotted identifiers (sqlalchemy.exc.Error) — was causing
    false sentence-count failures on #59349 and #56497
  - Validator evidence patterns broadened: catches "single comment",
    "lack of an assigned developer", "without any comments",
    "remains unassigned", spelled-out numbers ("two reviewers"), etc.
  - Forbidden phrases expanded: "immediate action is needed",
    "action is needed to", "needs to be addressed/prioritized", etc.
  - Rule 7 updated: explicitly discourages "With a [band] risk score"
    as a default opener (was appearing in 8/10 spot-check outputs)
  - Rule 8 extended: explicit guidance for small comment counts (2, 3)
    must appear as exact digits, never paraphrased away
  - 4th few-shot example added: "Twenty-nine days without..." opener

v2 changes vs v1:
  - temperature raised to 0.3 (was 0.0)
  - system prompt rewritten: opener diversity, numeric preservation,
    evidence-field requirements, forbidden generic closings
  - few-shot examples rewritten to show opener variety
  - post-generation validation + retry loop
  - evidence-type tagging for balanced training splits
  - greedy decoding spot-check mode

Data flow:
  1. Read scorer_labels.jsonl → filter to medium + high band
  2. For each record, enrich with additional fields from raw CSVs
  3. Call GPT-4o to generate a 2-3 sentence narrative
  4. Validate narrative passes all quality checks (retry up to 2x)
  5. Tag record with evidence_types for balanced splitting
  6. Write to reasoner_labels.jsonl

Per Section 4.2 / 5.5 / 5A.5 of AutoBot_Ref_v6.
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

# Project percentiles (Section 5A.1)
P50_DAYS = 1
P75_DAYS = 5
P90_DAYS = 22
P95_DAYS = 44

# Timeline events to keep for reasoner (Section 4.2 truncation)
KEEP_TIMELINE_EVENTS = {"assigned", "labeled", "cross-referenced", "review_requested"}

# ---------------------------------------------------------------------------
# GPT-4o Teacher Prompt — v2
# Key changes:
#   - Rule 6: explicit forbidden closing phrases
#   - Rule 7: mandatory opener variety with concrete examples
#   - Rule 8: numeric preservation (exact counts must appear)
#   - Rule 9: evidence-field requirements (assignee, PR, CI, comments)
#   - Rule 10: issue-specific final sentence requirement
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEACHER = """\
You are a senior engineering project analyst writing a bottleneck risk briefing \
for a non-technical scrum master.

Project stats: P50={p50} day, P75={p75} days, P90={p90} days, P95={p95} days.
Risk score: {score} ({band}).

Write exactly 2-3 sentences as a single paragraph.

Rules:
1. No bullet points or lists. Plain prose only.
2. Must reference at least TWO specific observable evidence signals from the \
data below. Evidence signals include: exact comment count, days since last \
review, CI failure/pass, number of assignee changes, silent reviewer count, \
specific contributor name, PR state (closed without merge, approved, open), \
comment gap duration.
3. Must mention the numeric risk score ({score}) and connect it directly to \
the specific evidence — not just state it.
4. No jargon without plain-English explanation. Describe "PR" as \
"proposed code update", "CI" as "automated tests", "assignee" as \
"assigned developer".
5. Must mention how many days the issue has been open and compare it to \
project baselines. NEVER write "P75" or "P90" — use phrases like \
"taking longer than 75% of historical issues" or "in the slowest 10% \
of the project".
6. FORBIDDEN CLOSING PHRASES — never end with any of these generic sentences:
   - "...if not addressed soon."
   - "...risks slipping into the slowest X% of issues."
   - "...risks drifting into the slowest X% of issues."
   - "Immediate attention is needed to prevent..."
   - "...to prevent it from becoming one of the slowest..."
   Your final sentence must be specific to this issue — name the blocker, \
the technical domain, the stalled reviewer, or the next concrete action.
7. VARY YOUR OPENING SENTENCE — do NOT always start with "At X days open" \
or "With a [band] risk score of X". Both of these are overused defaults — \
choose based on the most alarming signal in this specific issue instead. \
Valid openers include:
   - Lead with the key stall: "Despite a proposed code update submitted by \
[name], this [issue type]..."
   - Lead with silence: "Eleven days without a new comment on this [type]..."
   - Lead with scope: "This [critical/blocking] [type] has been open X days..."
   - Lead with ownership gap: "Still unassigned after X days, this [type]..."
   - Lead with risk score (use sparingly): "With a {band} risk score of \
{score}, this [issue type]..."
8. PRESERVE EXACT NUMBERS — if the data says 9 comments, write "9 comments", \
not "active discussion" or "the comments". If days_open is 22, write "22 days", \
not "several weeks". If there are 2 silent reviewers, write "two reviewers \
assigned but silent". Small comment counts are especially important: if \
comment_count is 1, write "a single comment from [author]" or "1 comment"; \
if it is 2 or 3, write the exact digit — never drop a count just because it \
is small. Never paraphrase a number away. This applies even when the count \
seems minor: "3 comments" must appear as "3 comments", not as "some comments" \
or omitted entirely when describing reviewer feedback.
9. USE AVAILABLE EVIDENCE FIELDS when present — if the data includes:
   - PR review state: mention whether the proposed code update was approved, \
closed without resolution, or awaiting review.
   - Silent reviewer: explicitly state that a reviewer was assigned but has \
not responded.
   - CI result: mention whether automated tests passed or failed.
   - Assignee absence: explicitly state the issue has no assigned developer.
   - Comment gap: state the longest gap in days between comments if > 5 days.
10. Your final sentence must be issue-specific — reference the actual \
technical area (scheduler, UI, authentication, DAG parsing, etc.), \
the specific bottleneck (stalled review, missing assignee, closed PR), \
or the concrete next step needed. Generic urgency phrases are not acceptable.

Respond with ONLY the narrative paragraph. No preamble, no labels.\
"""

# ---------------------------------------------------------------------------
# Few-Shot Examples — v2
# Each opener uses a different anchor to teach variety.
# Example 1: high-risk, lead with stall signal (no "At X days open" opener)
# Example 2: low-risk, lead with positive momentum
# Example 3: medium-risk, lead with risk score
# ---------------------------------------------------------------------------
FEW_SHOT_EXAMPLES = [
    # --- Example 1: high risk, late snapshot, opener = key stall ---
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
            "Comment Count: 4\n"
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
            "Despite a developer pinpointing the exact off-by-one error in the slot "
            "counter after 4 comments, this critical scheduler bug has sat unassigned "
            "for 14 days with an 11-day silence since the last exchange — already "
            "slower than 75% of all historical issues and carrying a high risk score "
            "of 0.74. "
            "The absence of any proposed code update means the identified root cause "
            "has not translated into action, and without an owner in the scheduler "
            "area the fix is likely to stall further."
        ),
    },

    # --- Example 2: low risk, early snapshot, opener = positive momentum ---
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
            "Comment Count: 2\n"
            "Max Comment Gap: 0 days\n"
            "Linked PRs: PR #51089 | State: open | Reviews: 0 | "
            "Days since last review: 1\n"
            "Comments: [priya_k]: Claimed! Working on a branch...\n\n"
            "Write a 2-3 sentence diagnostic narrative for this issue."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "This CLI feature request carries a low risk score of 0.18 and is showing "
            "healthy early momentum just 7 days in — developer priya_k has claimed "
            "ownership and submitted a proposed code update within the first week, "
            "which is faster than 75% of issues at this stage. "
            "The update is awaiting its first maintainer review after 1 day, which "
            "is well within normal range, and the absence of any comment gaps suggests "
            "active engagement rather than drift."
        ),
    },

    # --- Example 3: medium risk, late snapshot, opener = risk score ---
    {
        "role": "user",
        "content": (
            "ISSUE DATA:\n"
            "Issue #53847 — Snapshot at T+22 days [LATE SNAPSHOT]\n"
            "Title: XCom backend fails silently on large payloads in Redis provider\n"
            "Risk Score: 0.61 (medium)\n"
            "Labels: kind:bug, area:providers, area:xcom\n"
            "Assignees: 1 (wei_chen)\n"
            "Days open at snapshot: 22\n"
            "Comment Count: 6\n"
            "Max Comment Gap: 9.0 days\n"
            "Linked PRs: PR #53901 | State: open | Reviews: 2 | "
            "Silent Reviewers: 1 | Days since last review: 8\n"
            "PR Review Feedback:\n"
            "[reviewer_a CHANGES_REQUESTED]: Missing error handling for "
            "payload size threshold check.\n\n"
            "Write a 2-3 sentence diagnostic narrative for this issue."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "With a medium risk score of 0.61 after 22 days — slower than 90% of "
            "historical issues — this XCom bug has a proposed code update that has "
            "been stalled for 8 days since a reviewer requested changes to the "
            "payload size error handling, and one of two assigned reviewers has "
            "not responded at all. "
            "The 9-day gap between the 6 comments suggests that developer wei_chen "
            "has not yet addressed the requested change, leaving the Redis provider "
            "fix in a review deadlock that is unlikely to resolve without a direct "
            "follow-up on the outstanding feedback."
        ),
    },

    # --- Example 4: medium risk, late snapshot, opener = days open count ---
    # Demonstrates a fourth opener style distinct from "With a", "Despite", stall lead
    {
        "role": "user",
        "content": (
            "ISSUE DATA:\n"
            "Issue #54201 — Snapshot at T+29 days [LATE SNAPSHOT]\n"
            "Title: TaskFlow API context variables missing in dynamic task mapping\n"
            "Risk Score: 0.58 (medium)\n"
            "Labels: kind:bug, area:taskflow, area:core\n"
            "Assignees: 0 (unassigned)\n"
            "Days open at snapshot: 29\n"
            "Comment Count: 3\n"
            "Max Comment Gap: 14.0 days\n"
            "Linked PRs: None\n"
            "Comments: [user_a]: Confirmed in 2.9... [user_b]: Same issue "
            "on 2.10...\n\n"
            "Write a 2-3 sentence diagnostic narrative for this issue."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Twenty-nine days without a proposed code update places this TaskFlow "
            "API bug in the slowest 10% of project issues, and a 14-day gap between "
            "the 3 comments signals that the thread has gone cold — driving its "
            "medium risk score of 0.58. "
            "The issue remains unassigned, meaning no developer has formally taken "
            "ownership of the context variable failure in dynamic task mapping, and "
            "without a maintainer directing next steps the stall is likely to "
            "continue across both the 2.9 and 2.10 release lines."
        ),
    },
]


# ---------------------------------------------------------------------------
# Evidence type tagging — used to balance training splits
# ---------------------------------------------------------------------------
EVIDENCE_TYPES = [
    "no_assignee",
    "silent_reviewer",
    "closed_without_merge",
    "approved_pr_stalled",
    "no_comments",
    "many_comments_no_code",
    "ci_failed",
    "ci_passed",
    "assignee_changed",
    "long_comment_gap",
    "multiple_prs",
]


def tag_evidence_types(scorer_input: str, enriched_input: str) -> list[str]:
    """Tag which evidence types are present in this record.

    Used downstream to ensure balanced training splits across
    evidence types, not just risk bands.
    """
    tags = []
    combined = (scorer_input + enriched_input).lower()

    if "assignees: 0" in combined or "unassigned" in combined:
        tags.append("no_assignee")
    if "silent reviewer" in combined or "requested_reviewers_with_no_review: true" in combined:
        tags.append("silent_reviewer")
    if "closed without" in combined or "was_closed_without_merge: true" in combined:
        tags.append("closed_without_merge")
    if "approved" in combined and ("stall" in combined or "no new commit" in combined):
        tags.append("approved_pr_stalled")
    if "comment_count: 0" in combined or "comments: none" in combined:
        tags.append("no_comments")
    if re.search(r"comment.count:\s*[5-9]|comment.count:\s*[1-9]\d", combined):
        if "linked_pr_count: 0" in combined or "no linked" in combined:
            tags.append("many_comments_no_code")
    if "ci_failed: true" in combined or "ci: failed" in combined:
        tags.append("ci_failed")
    if "ci_failed: false" in combined or "ci: passed" in combined:
        tags.append("ci_passed")
    if "assignee_change_count:" in combined:
        match = re.search(r"assignee_change_count:\s*([1-9]\d*)", combined)
        if match:
            tags.append("assignee_changed")
    if "max_comment_gap" in combined:
        match = re.search(r"max_comment_gap[_\s\w]*:\s*(\d+\.?\d*)", combined)
        if match and float(match.group(1)) >= 7:
            tags.append("long_comment_gap")
    if combined.count("pr #") >= 2 or combined.count("linked_pr_count: ") and \
            re.search(r"linked_pr_count:\s*[2-9]", combined):
        tags.append("multiple_prs")

    return tags if tags else ["no_special_evidence"]


# ---------------------------------------------------------------------------
# Narrative validation
# ---------------------------------------------------------------------------

class NarrativeValidator:
    """Post-generation checks before writing to JSONL.

    Checks:
      - Exact days_open number present
      - Score present
      - Comment count present when >= 3
      - No forbidden generic closing phrases
      - Sentence count 2-3
      - At least 2 evidence signals referenced
      - Correct percentile framing (no raw "P75"/"P90")
    """

    FORBIDDEN_PHRASES = [
        "if not addressed soon",
        "risks slipping into the slowest",
        "risks drifting into the slowest",
        "immediate attention is needed",
        "immediate action is needed",
        "action is needed to",
        "attention is needed to",
        "to prevent it from becoming one of the slowest",
        "to prevent it from slipping into",
        "to prevent it from drifting",
        "this issue has been open a long time",
        "needs to be addressed",
        "needs to be prioritized",
        "should be prioritized",
    ]

    # Signals that indicate the narrative used concrete evidence.
    # Patterns are intentionally broad to avoid false negatives —
    # the goal is to confirm evidence was referenced, not enforce exact wording.
    EVIDENCE_SIGNAL_PATTERNS = [
        r"\d+\s+comment",                        # "9 comments", "3 comments"
        r"single comment",                        # "a single comment from"
        r"one comment",                           # "only one comment"
        r"no comment",                            # "without any comments"
        r"no further (comment|discussion)",       # "no further discussion"
        r"\d+[\s-]day\s+gap",                    # "11-day gap", "9 day gap"
        r"gap of \d+",                            # "gap of 14 days"
        r"silent reviewer",                       # explicit signal
        r"reviewer.{0,30}(silent|not responded|no response)",
        r"no assigned developer",                 # exact phrase
        r"lack of an? assigned",                  # "lack of an assigned developer"
        r"without an? assigned",                  # "without an assigned developer"
        r"remains unassigned",                    # "the issue remains unassigned"
        r"still unassigned",
        r"no (developer|owner|assignee)",
        r"unassigned",                            # standalone fallback
        r"closed without",                        # "closed without merging/resolution"
        r"automated tests? (passed|failed)",      # CI signal
        r"proposed code update",                  # PR reference
        r"\d+\s+(review cycle|revision)",
        r"assignee.{0,20}changed",
        r"changes requested",
        r"approved",
        r"\d+\s+days? since",
        r"[a-z_-]{3,}\s+(submitted|opened|closed|requested)",  # contributor actions
        r"no (comments?|discussion|progress|linked|code)",     # absence signals
        r"without (any )?(comment|discussion|progress|code|linked)",
        r"\d+\s+(developer|contributor|reviewer)",             # "two reviewers"
        r"(two|three|four|five|six|seven|eight|nine|ten)\s+(reviewer|developer|comment)",
    ]

    def validate(
        self,
        narrative: str,
        days_open: int,
        comment_count: int,
        score: float,
    ) -> tuple[bool, list[str]]:
        failures = []

        # 1. Must contain actual days_open
        if str(days_open) not in narrative:
            failures.append(f"missing days_open={days_open}")

        # 2. Must contain score
        score_str = str(round(score, 2))
        alt_score = str(round(score, 1))
        if score_str not in narrative and alt_score not in narrative:
            failures.append(f"missing score={score_str}")

        # 3. Comment count when meaningful
        if comment_count >= 3 and str(comment_count) not in narrative:
            failures.append(f"missing comment_count={comment_count}")

        # 4. No raw percentile labels
        if re.search(r'\bP75\b|\bP90\b|\bP50\b|\bP95\b', narrative):
            failures.append("contains raw percentile label (P75/P90/etc)")

        # 5. No forbidden generic closings
        for phrase in self.FORBIDDEN_PHRASES:
            if phrase.lower() in narrative.lower():
                failures.append(f"contains forbidden phrase: '{phrase}'")

        # 6. Sentence count 2-3
        # Split only on sentence-ending periods (period followed by whitespace +
        # capital letter). This avoids false splits on decimal numbers (0.68),
        # version strings (3.0.2), and dotted identifiers (sqlalchemy.exc.Error).
        sentences = [
            s.strip() for s in re.split(r'\.(?=\s+[A-Z])|[!?]', narrative)
            if len(s.strip()) > 15
        ]
        if not (2 <= len(sentences) <= 4):  # 4 allows for edge cases
            failures.append(f"sentence count={len(sentences)}, expected 2-3")

        # 7. At least 2 concrete evidence signals
        signals_found = sum(
            1 for pattern in self.EVIDENCE_SIGNAL_PATTERNS
            if re.search(pattern, narrative.lower())
        )
        if signals_found < 2:
            failures.append(
                f"only {signals_found} evidence signal(s) found, need at least 2"
            )

        return len(failures) == 0, failures

    def build_retry_message(
        self,
        failures: list[str],
        days_open: int,
        comment_count: int,
        score: float,
    ) -> str:
        """Build a specific correction message for the retry."""
        lines = [
            "That narrative has the following issues that must be fixed:",
        ]
        for f in failures:
            lines.append(f"  - {f}")

        lines.append("")
        lines.append("Please rewrite the narrative. Specifically:")

        if any("days_open" in f for f in failures):
            lines.append(f"  - You must include the number {days_open} (days open).")
        if any("score" in f for f in failures):
            lines.append(f"  - You must include the risk score {round(score, 2)}.")
        if any("comment_count" in f for f in failures):
            lines.append(f"  - You must include the exact comment count ({comment_count}).")
        if any("forbidden phrase" in f for f in failures):
            lines.append(
                "  - Do not end with generic urgency phrases. End with something "
                "specific to this issue's technical area or the concrete blocker."
            )
        if any("evidence signal" in f for f in failures):
            lines.append(
                "  - Reference at least 2 concrete signals: comment count, gap duration, "
                "PR review state, CI result, assignee presence/absence, or silent reviewer."
            )
        if any("opener" in f for f in failures) or any("At" in f for f in failures):
            lines.append(
                f"  - Do not open with 'At {days_open} days open'. "
                "Choose a different anchor based on the most alarming signal."
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main labeler class
# ---------------------------------------------------------------------------

class ReasonerLabeler(BaseLabeler):
    model_name = "reasoner_v2"

    def __init__(
        self,
        output_dir: Path | str,
        dry_run: bool = False,
        gpt_model: str = "gpt-4o",
        temperature: float = 0.3,
        max_retries: int = 2,
    ):
        super().__init__(Path(output_dir), dry_run)
        self.gpt_model = gpt_model
        self.temperature = temperature
        self.max_retries = max_retries
        self.validator = NarrativeValidator()

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
                    if band >= 1:  # medium (1) + high (2) only
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
    # Enrichment helpers (unchanged from v1)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_pr_review_bodies(prs: list[dict]) -> str:
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
        """Build full reasoner input: scorer input + additional fields."""
        parts = [f"Risk Score: {score:.2f} ({band_name})\n"]
        parts.append(scorer_input)

        linked_prs = pr_lookup.get(issue_num, [])

        snap_data = snapshot_lookup.get(f"{issue_num}_{snapshot_tier}")
        snapshot_date = None
        if snap_data:
            snapshot_date = pd.to_datetime(snap_data.get("SNAPSHOT_DATE"), utc=True)
            if snapshot_date and snapshot_date.tzinfo:
                snapshot_date = snapshot_date.tz_convert(None)

        filtered_prs = []
        for pr in linked_prs:
            pr_created = pd.to_datetime(pr.get("CREATED_AT"), utc=True)
            if pr_created and pr_created.tzinfo:
                pr_created = pr_created.tz_convert(None)
            if (snapshot_date is None or pr_created is None
                    or pr_created <= snapshot_date):
                filtered_prs.append(pr)

        review_bodies = self._extract_pr_review_bodies(filtered_prs)
        if review_bodies:
            parts.append(f"\nPR Review Feedback:\n{review_bodies[:600]}")

        review_comments = self._extract_pr_review_comments(filtered_prs)
        if review_comments:
            parts.append(f"\nInline Code Review Comments:\n{review_comments[:400]}")

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
    # GPT message builder
    # ------------------------------------------------------------------

    def _build_gpt_messages(
        self,
        enriched_input: str,
        score: float,
        band_name: str,
        snapshot_tier: str,
    ) -> list[dict]:
        # Determine early vs late label for tone calibration
        tier_num = 7
        match = re.search(r'T\+(\d+)', snapshot_tier)
        if match:
            tier_num = int(match.group(1))
        snapshot_label = "EARLY SNAPSHOT" if tier_num <= 14 else "LATE SNAPSHOT"

        sys_msg = SYSTEM_PROMPT_TEACHER.format(
            p50=P50_DAYS, p75=P75_DAYS, p90=P90_DAYS, p95=P95_DAYS,
            score=round(score, 2), band=band_name,
        )
        user_msg = (
            f"ISSUE DATA: [{snapshot_label}]\n"
            f"{enriched_input}\n\n"
            f"Write a 2-3 sentence diagnostic narrative for this issue."
        )
        return [
            {"role": "system", "content": sys_msg},
            *FEW_SHOT_EXAMPLES,
            {"role": "user", "content": user_msg},
        ]

    # ------------------------------------------------------------------
    # Extract key fields for validation from scorer input
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_validation_fields(scorer_input: str) -> dict:
        """Pull days_open and comment_count from structured scorer input."""
        fields = {"days_open": 0, "comment_count": 0}

        m = re.search(r'DAYS_OPEN:\s*(\d+)', scorer_input, re.IGNORECASE)
        if m:
            fields["days_open"] = int(m.group(1))

        m = re.search(r'COMMENT_COUNT:\s*(\d+)', scorer_input, re.IGNORECASE)
        if m:
            fields["comment_count"] = int(m.group(1))

        return fields

    # ------------------------------------------------------------------
    # Single label with retry loop
    # ------------------------------------------------------------------

    async def _label_one_with_retry(
        self,
        scorer_rec: dict,
        pr_lookup: dict,
        snapshot_lookup: dict,
    ) -> tuple[int, str, dict, str | None, str | None, list[str]]:
        """Label one record, retrying up to self.max_retries on validation failure.

        Returns: (issue_num, tier, scorer_rec, enriched_input, narrative, evidence_tags)
        """
        issue_num = scorer_rec["issue_number"]
        tier = scorer_rec["snapshot_tier"]
        label_data = scorer_rec.get("label", {})
        score = label_data.get("score", 0.0)
        band_name = label_data.get("band_name", "medium")

        try:
            enriched = self._enrich_input(
                scorer_input=scorer_rec.get("input", ""),
                score=score,
                band_name=band_name,
                issue_num=issue_num,
                snapshot_tier=tier,
                pr_lookup=pr_lookup,
                snapshot_lookup=snapshot_lookup,
            )
        except Exception as exc:
            logger.error("Enrichment failed for %s (%s): %s", issue_num, tier, exc)
            return issue_num, tier, scorer_rec, None, None, []

        val_fields = self._extract_validation_fields(scorer_rec.get("input", ""))
        days_open = val_fields["days_open"]
        comment_count = val_fields["comment_count"]

        messages = self._build_gpt_messages(enriched, score, band_name, tier)
        narrative = None
        last_failures = []

        for attempt in range(self.max_retries + 1):
            # Slightly raise temperature on each retry to escape the stuck pattern
            attempt_temp = min(self.temperature + (attempt * 0.1), 0.7)

            try:
                candidate = await async_call_gpt4o(
                    messages=messages,
                    model=self.gpt_model,
                    temperature=attempt_temp,
                )
            except Exception as exc:
                logger.error(
                    "GPT call failed for %s (%s) attempt %d: %s",
                    issue_num, tier, attempt, exc,
                )
                break

            valid, failures = self.validator.validate(
                candidate, days_open, comment_count, score
            )

            if valid:
                narrative = candidate
                last_failures = []
                break

            last_failures = failures
            if attempt < self.max_retries:
                logger.warning(
                    "Validation failed for #%s (%s) attempt %d: %s — retrying",
                    issue_num, tier, attempt, failures,
                )
                # Add the failed attempt + specific correction to messages
                retry_msg = self.validator.build_retry_message(
                    failures, days_open, comment_count, score
                )
                messages = messages + [
                    {"role": "assistant", "content": candidate},
                    {"role": "user", "content": retry_msg},
                ]
            else:
                logger.error(
                    "Still invalid after %d retries for #%s (%s): %s — using best candidate",
                    self.max_retries, issue_num, tier, failures,
                )
                # Use the last candidate anyway but log that it failed validation
                narrative = candidate

        # Tag evidence types for balanced splitting
        evidence_tags = tag_evidence_types(scorer_rec.get("input", ""), enriched)

        return issue_num, tier, scorer_rec, enriched, narrative, evidence_tags

    # ------------------------------------------------------------------
    # Main labelling loop
    # ------------------------------------------------------------------

    async def async_label_all_from_scorer(
        self,
        limit: int | None = None,
        concurrency: int = 10,
    ) -> dict[str, int]:
        """Label medium+high scorer records with GPT-4o narratives."""
        from tqdm import tqdm

        counts = {
            "labeled": 0,
            "skipped": 0,
            "errors": 0,
            "validation_failures": 0,
        }
        output_file = self.output_dir / f"{self.model_name}_labels.jsonl"

        # Resume: skip already-processed keys
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

        all_records = self._load_scorer_records()
        if limit:
            all_records = all_records[:limit]

        logger.info("Loading raw CSVs for PR review + timeline enrichment...")
        pr_lookup = load_pr_lookup()

        issues_df = load_issues_csv()
        snapshot_lookup: dict[str, dict] = {}
        for _, row in issues_df.iterrows():
            key = f"{int(row['ISSUE_NUMBER'])}_{row['SNAPSHOT_TIER']}"
            snapshot_lookup[key] = row.to_dict()

        todo = []
        for rec in all_records:
            key = f"{rec['issue_number']}_{rec['snapshot_tier']}"
            if key in processed_keys:
                counts["skipped"] += 1
            else:
                todo.append(rec)

        logger.info(
            "To label: %d, already done: %d", len(todo), counts["skipped"]
        )

        total = len(todo) + counts["skipped"]
        pbar = tqdm(
            total=total,
            desc="Labelling reasoner",
            unit="issue",
            initial=counts["skipped"],
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        )

        sem = asyncio.Semaphore(concurrency)

        async def _wrapped(rec):
            async with sem:
                result = await self._label_one_with_retry(rec, pr_lookup, snapshot_lookup)
                pbar.update(1)
                return result

        tasks = [_wrapped(rec) for rec in todo]
        results = await asyncio.gather(*tasks)

        with open(output_file, "a") as f:
            for issue_num, tier, scorer_rec, enriched, narrative, evidence_tags in results:
                if narrative is None:
                    counts["errors"] += 1
                    continue

                label_data = scorer_rec.get("label", {})

                # Flag records where validation ultimately failed (used best candidate)
                val_fields = self._extract_validation_fields(scorer_rec.get("input", ""))
                final_valid, final_failures = self.validator.validate(
                    narrative,
                    val_fields["days_open"],
                    val_fields["comment_count"],
                    label_data.get("score", 0.0),
                )
                if not final_valid:
                    counts["validation_failures"] += 1

                record = {
                    "issue_number": issue_num,
                    "snapshot_tier": tier,
                    "model": self.model_name,
                    "input": enriched,
                    "scorer_score": label_data.get("score", 0.0),
                    "scorer_band": label_data.get("band_name", ""),
                    # Evidence tags for balanced training splits
                    "evidence_types": evidence_tags,
                    # Validation metadata — useful for post-hoc filtering
                    "validation_passed": final_valid,
                    "validation_failures": final_failures if not final_valid else [],
                    "label": {"narrative": narrative},
                }
                if not self.dry_run:
                    f.write(json.dumps(record, default=str) + "\n")
                counts["labeled"] += 1

        pbar.close()
        logger.info(
            "Complete — labeled: %d, skipped: %d, errors: %d, "
            "validation_failures (best-candidate used): %d",
            counts["labeled"], counts["skipped"],
            counts["errors"], counts["validation_failures"],
        )
        return counts

    # ------------------------------------------------------------------
    # Spot-check utility: run greedy (temp=0) for deterministic review
    # ------------------------------------------------------------------

    async def spot_check_greedy(
        self,
        n: int = 10,
    ) -> list[dict]:
        """Run n samples with temperature=0 for deterministic quality review.

        Use this BEFORE a full labeling run to verify label quality.
        Returns list of {issue_num, tier, narrative, validation_passed, failures}.
        """
        all_records = self._load_scorer_records()
        pr_lookup = load_pr_lookup()
        issues_df = load_issues_csv()
        snapshot_lookup = {
            f"{int(row['ISSUE_NUMBER'])}_{row['SNAPSHOT_TIER']}": row.to_dict()
            for _, row in issues_df.iterrows()
        }

        # Sample: mix of bands
        import random
        random.seed(42)
        sampled = random.sample(all_records, min(n, len(all_records)))

        results = []
        for rec in sampled:
            issue_num = rec["issue_number"]
            tier = rec["snapshot_tier"]
            label_data = rec.get("label", {})
            score = label_data.get("score", 0.0)
            band_name = label_data.get("band_name", "medium")

            enriched = self._enrich_input(
                scorer_input=rec.get("input", ""),
                score=score,
                band_name=band_name,
                issue_num=issue_num,
                snapshot_tier=tier,
                pr_lookup=pr_lookup,
                snapshot_lookup=snapshot_lookup,
            )
            messages = self._build_gpt_messages(enriched, score, band_name, tier)

            # Greedy pass
            narrative = await async_call_gpt4o(
                messages=messages,
                model=self.gpt_model,
                temperature=0.0,
            )
            val_fields = self._extract_validation_fields(rec.get("input", ""))
            valid, failures = self.validator.validate(
                narrative,
                val_fields["days_open"],
                val_fields["comment_count"],
                score,
            )
            results.append({
                "issue_num": issue_num,
                "tier": tier,
                "band": band_name,
                "score": score,
                "narrative": narrative,
                "validation_passed": valid,
                "failures": failures,
                "evidence_types": tag_evidence_types(rec.get("input", ""), enriched),
            })
            print(f"\n{'='*70}")
            print(f"Issue #{issue_num} ({tier}) | {band_name} | score={score}")
            print(f"Evidence types: {tag_evidence_types(rec.get('input', ''), enriched)}")
            print(f"Validation: {'PASS' if valid else 'FAIL — ' + str(failures)}")
            print(f"Narrative:\n{narrative}")

        pass_rate = sum(1 for r in results if r["validation_passed"]) / len(results)
        print(f"\n{'='*70}")
        print(f"Spot-check pass rate: {pass_rate:.0%} ({sum(1 for r in results if r['validation_passed'])}/{len(results)})")
        return results

    # ------------------------------------------------------------------
    # Interface stubs
    # ------------------------------------------------------------------

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "ReasonerLabeler uses async_label_all_from_scorer()."
        )

    async def async_label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "ReasonerLabeler uses async_label_all_from_scorer()."
        )


# ---------------------------------------------------------------------------
# CLI
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

    parser = argparse.ArgumentParser(
        description="Run reasoner labelling from scorer output."
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--model", type=str, default="gpt-4o")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-retries", type=int, default=2)
    # Spot-check mode: run 10 greedy samples and print, don't write JSONL
    parser.add_argument(
        "--spot-check", type=int, default=0,
        help="Run N greedy spot-check samples and exit (no JSONL written)"
    )
    args = parser.parse_args()

    _base = Path(__file__).resolve().parent.parent
    output_dir = (
        Path(args.output_dir) if args.output_dir
        else _base / "data" / "labeled"
    )

    reasoner = ReasonerLabeler(
        output_dir=output_dir,
        dry_run=args.dry_run,
        gpt_model=args.model,
        temperature=args.temperature,
        max_retries=args.max_retries,
    )

    start = time.time()

    if args.spot_check > 0:
        # Spot-check mode: deterministic pass, prints to stdout, no file writes
        asyncio.run(reasoner.spot_check_greedy(n=args.spot_check))
    else:
        counts = asyncio.run(reasoner.async_label_all_from_scorer(
            limit=args.limit,
            concurrency=args.concurrency,
        ))
        elapsed = time.time() - start
        logger.info(
            "DONE — labeled: %d, skipped: %d, errors: %d, "
            "validation_failures: %d — %.1fs (%.1f min)",
            counts["labeled"], counts["skipped"],
            counts["errors"], counts["validation_failures"],
            elapsed, elapsed / 60,
        )