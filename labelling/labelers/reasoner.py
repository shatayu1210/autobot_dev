"""Reasoner labeler — Model 2 (Qwen2.5-7B-Instruct + LoRA).

Label strategy: 100% GPT-4o narrative generation (Section 5.5 of AutoBot_Ref_v2).

Output label: {"narrative": str}
  2-3 sentence paragraph explaining WHY this issue is a bottleneck,
  citing specific signals from the snapshot (no generic statements).
  Written for a non-technical scrum master audience.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import re
import datetime

from .base import BaseLabeler, call_gpt4o

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System Prompt (Section 5.5 + 5A.5 spec)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEACHER = """\
You are a senior engineering project analyst writing a bottleneck risk briefing for a non-technical scrum master.

Project stats: P50=1 day, P75=5 days, P90=22 days, P95=44 days. Risk score: {score} ({band}).

Write exactly 2–3 sentences as a single paragraph. 

Rules:
1. No bullet points or lists. Plain prose only.
2. Must reference at least two specific observable signals from the data below (e.g., specific comment author, days since last review, CI failure, or number of reassignments).
3. Must mention the numeric risk score and connect it to the evidence.
4. No jargon without a plain-English explanation (e.g., describe a "PR" as a "proposed code update").
5. Must mention how many days the issue has been open and compare it to project baselines using HUMAN-READABLE phrasing. NEVER say "P75" or "P90". Instead, use phrases like "taking longer to resolve than 75% of historical issues" or "slower than 90% of the project's issues".
6. Do not write generic phrases like "this issue has been open a long time" — be specific with numbers.
7. Tone calibration: If the snapshot is EARLY (T+7), be investigative/cautious. If the snapshot is LATE (T+14), match the growing urgency of a stale issue.

Respond with ONLY the narrative paragraph."""

# ---------------------------------------------------------------------------
# Training/Inference System Prompt (Section 5A.5 V4)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_QWEN = """\
You are a bottleneck analyst for GitHub issues. Given an issue snapshot and its risk score, write a 2–3 sentence explanation for a non-technical scrum master. Reference specific signals. No bullet points.
RISK_SCORE: {score} | PROJECT: apache/airflow | P50=1d P75=5d P90=22d P95=44d"""

# ---------------------------------------------------------------------------
# Few-Shot Examples (Reflecting User's "Human-Readable" Request)
# ---------------------------------------------------------------------------
FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": """ISSUE DATA:
Issue #48291 — Snapshot at T+14 days [LATE SNAPSHOT]
Title: Scheduler silently drops tasks when DAG has more than 50 slots
Risk Score: 0.74 (high)
Labels: kind:bug, area:scheduler, priority:critical
Assignees: 0 (unassigned)
Days open at snapshot: 14
Max Comment Gap: 11.0 days
Linked PRs: None
Comments: [dev_alice]: Confirmed on 2.8.1... [dev_bob]: off-by-one on the slot counter...

Write a 2-3 sentence diagnostic narrative for this issue."""
    },
    {
        "role": "assistant",
        "content": """At 14 days open, this critical scheduler bug is already taking longer to resolve than 75% of all historical issues in the project. With no assignee and no linked code update, its risk score has climbed to 0.74 as the massive 11-day gap between comments suggests the fix has stalled despite a developer identifying the exact technical root cause. We should escalate this before it drifts further into the slowest 10% of issues."""
    },
    {
        "role": "user",
        "content": """ISSUE DATA:
Issue #51033 — Snapshot at T+7 days [EARLY SNAPSHOT]
Title: Add --dry-run flag to airflow db upgrade command
Risk Score: 0.18 (low)
Labels: kind:feature, area:cli, good first issue
Assignees: 1 (priya_k)
Days open at snapshot: 7
Linked PRs: PR #51089 | State: open | Reviews: 0 | Days since last review: 1
Comments: [priya_k]: Claimed! Working on a branch...

Write a 2-3 sentence diagnostic narrative for this issue."""
    },
    {
        "role": "assistant",
        "content": """This feature request is currently showing healthy early momentum and carries a low 0.18 risk score only seven days into its lifecycle. While it has recently crossed the one-week mark — making it slightly slower than the median project issue — developer priya_k is actively moving it forward with a newly submitted code update that is awaiting maintainer feedback."""
    }
]

class ReasonerLabeler(BaseLabeler):
    model_name = "reasoner"

    project_stats: dict = {
        "p50": 1,
        "p75": 5,
        "p90": 22,
        "p95": 44,
    }

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        # Reasoner Model 2 always uses GPT-4o for its training data (narrative)
        narrative = self._generate_narrative(snapshot)
        return {"narrative": narrative}

    def _generate_narrative(self, snapshot: dict[str, Any]) -> str:
        issue = snapshot.get("issue", {})
        
        # Risk score from Scorer
        score_val = snapshot.get("scorer_score", 0.0)
        score_band = "high" if score_val >= 0.65 else "medium" if score_val >= 0.35 else "low"
        
        # Metrics
        days_open = snapshot.get('days_open_at_snapshot', 0) or 0
        stage_label = snapshot.get('snapshot_type', "EARLY" if days_open <= 7 else "LATE").upper()
        
        label_names = ", ".join(lb.get("name", "") for lb in issue.get("labels", []))
        assignees = ", ".join(a.get("login", "") for a in issue.get("assignees", [])) or "unassigned"
        
        timeline = snapshot.get("timeline", [])
        reassignments = sum(1 for e in timeline if e.get("event") == "unassigned")
        cross_refs = ", ".join(set(str(e.get("source", {}).get("issue", {}).get("number", "?")) for e in timeline if e.get("event") == "cross-referenced"))

        # Body - STRIP CLOSURE SIGNALS (V4 5.1/7.3)
        clean_body = self._strip_closure_signals(issue.get("body", ""))

        # Comments - STRIP CLOSURE SIGNALS
        comments = snapshot.get("comments", [])
        max_gap = 0.0
        if len(comments) >= 2:
            sorted_c = sorted(comments, key=lambda c: c.get("created_at", ""))
            try:
                gaps = []
                for i in range(1, len(sorted_c)):
                    t1 = datetime.datetime.fromisoformat(sorted_c[i - 1].get("created_at", "").replace("Z", "+00:00"))
                    t2 = datetime.datetime.fromisoformat(sorted_c[i].get("created_at", "").replace("Z", "+00:00"))
                    gaps.append(abs((t2 - t1).total_seconds()) / 86400)
                max_gap = max(gaps)
            except: pass
        
        comment_summary = ""
        for c in (comments[:2] + comments[-2:] if len(comments) > 4 else comments):
            clean_comment = self._strip_closure_signals(str(c.get("body") or ""))
            body_trunc = clean_comment[:150].replace("\n", " ")
            comment_summary += f"[{c.get('user',{}).get('login','?')}]: {body_trunc}... "

        # PR Data - STRIP CLOSURE SIGNALS FROM REVIEWS
        prs = snapshot.get("linked_snowflake_prs", [])
        pr_details = []
        for pr in prs:
            pr_data = pr.get("raw_json", {}) or {}
            state = pr.get("STATE", "unknown")
            rev_count = pr.get("REVIEW_COUNT", 0)
            
            # Review bodies and inline comments (V4 requirement)
            reviews = pr_data.get("reviews", [])
            review_bodies = " ".join(self._strip_closure_signals(str(r.get("body") or ""))[:200] for r in reviews if r.get("body"))
            
            review_comments = pr_data.get("comments", []) # Inline comments
            inline_bodies = " ".join(self._strip_closure_signals(str(c.get("body") or ""))[:100] for c in review_comments if c.get("body"))

            pr_str = f"PR #{pr_data.get('number','?')}: {state}, {rev_count} reviews. Review feedback: {review_bodies[:300]} Inline comments: {inline_bodies[:300]}"
            pr_details.append(pr_str)

        # Build Issue Data Block
        issue_data = (
            f"Issue #{snapshot.get('issue_number')} — Snapshot at T+{days_open} days [{stage_label} SNAPSHOT]\n"
            f"Title: {issue.get('title', '')}\n"
            f"Risk Score: {score_val:.2f} ({score_band})\n"
            f"Labels: {label_names}\n"
            f"Assignees: {assignees}\n"
            f"Days open: {days_open}\n"
            f"Reassignments: {reassignments}\n"
            f"Cross-referenced issues: {cross_refs}\n"
            f"Max Comment Gap: {max_gap:.1f} days\n"
            f"Recent Comments: {comment_summary}\n"
            f"Issue Body: {clean_body[:1000]}\n"
            f"Linked PRs: {' | '.join(pr_details) if pr_details else 'None'}"
        )

        sys_msg = SYSTEM_PROMPT_TEACHER.format(score=round(score_val, 2), band=score_band)
        
        return call_gpt4o(
            messages=[
                {"role": "system", "content": sys_msg},
                *FEW_SHOT_EXAMPLES,
                {"role": "user", "content": f"ISSUE DATA:\n{issue_data}\n\nWrite a 2-3 sentence diagnostic narrative for this issue."}
            ],
            temperature=0
        )
