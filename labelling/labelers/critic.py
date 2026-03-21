"""Critic labeler — Model 5 (Qwen2.5-Coder-7B + LoRA #3).

Label strategy:
  Primary: Extract ACCEPT / REVISE / REJECT verdicts from real PR review events.
           A PR with only APPROVED reviews → ACCEPT.
           A PR with any CHANGES_REQUESTED review → REVISE (or REJECT if it was never merged).
  Synthetic (~1.2K examples): GPT-4o generates labels for PRs with no formal reviews
           (e.g. merged without review, or review body is empty).

Output label:
{
  "verdict": "ACCEPT" | "REVISE" | "REJECT",
  "reasoning": str,       # 1-2 sentences
  "label_source": "real_review" | "gpt4o_synthetic",
  "review_summary": [     # condensed real reviews (if any)
    {"reviewer": str, "state": str, "body_excerpt": str},
    ...
  ]
}
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .base import BaseLabeler, call_gpt4o

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior software engineer reviewing a patch submitted for a GitHub issue.
Given the issue context and the patch diff, decide whether the patch should be:
  ACCEPT  — the patch is correct and ready to merge
  REVISE  — the patch has issues but is on the right track (needs changes)
  REJECT  — the patch is fundamentally wrong, targets the wrong files, or makes things worse

Respond with ONLY a JSON object:
{"verdict": "ACCEPT"|"REVISE"|"REJECT", "reasoning": "<1-2 sentences citing specific evidence from the diff>"}
No other text.
"""

# Fraction of examples to send to GPT-4o when no real review exists
GPT4O_SYNTHETIC_FRACTION = 0.30


class CriticLabeler(BaseLabeler):
    model_name = "critic"

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        linked_prs = snapshot.get("linked_prs", [])
        if not linked_prs:
            return None

        pr = self._pick_resolving_pr(linked_prs)
        if pr is None:
            return None

        reviews = pr.get("reviews", [])
        real_label = self._extract_real_verdict(pr, reviews)

        if real_label:
            return real_label

        # No usable real review — optionally generate synthetic label
        if self._should_synthesize(snapshot):
            return self._synthetic_verdict(snapshot, pr)

        return None

    # ------------------------------------------------------------------
    # Real review extraction
    # ------------------------------------------------------------------

    def _extract_real_verdict(self, pr: dict, reviews: list[dict]) -> dict | None:
        """Map APPROVED / CHANGES_REQUESTED reviews to ACCEPT/REVISE/REJECT."""
        if not reviews:
            return None

        approved = [r for r in reviews if r.get("state") == "APPROVED"]
        changes_requested = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
        dismissed = [r for r in reviews if r.get("state") == "DISMISSED"]

        review_summary = [
            {
                "reviewer": r.get("user", {}).get("login", "?"),
                "state": r.get("state", ""),
                "body_excerpt": (r.get("body") or "")[:300],
            }
            for r in reviews[:5]
        ]

        # All reviews approved and PR was merged → ACCEPT
        if approved and not changes_requested:
            verdict = "ACCEPT"
            reasoning = f"All {len(approved)} reviewer(s) approved with no requested changes."
        elif changes_requested and (pr.get("merged_at") or pr.get("state") == "closed"):
            # Changes were requested but PR was merged anyway → REVISE
            verdict = "REVISE"
            bodies = "; ".join((r.get("body") or "")[:100] for r in changes_requested[:2] if r.get("body"))
            reasoning = f"Reviewer(s) requested changes: {bodies or 'no body provided'}."
        elif changes_requested and not pr.get("merged_at"):
            # Changes requested and PR not merged → REJECT
            verdict = "REJECT"
            bodies = "; ".join((r.get("body") or "")[:100] for r in changes_requested[:2] if r.get("body"))
            reasoning = f"PR closed without merge after change requests: {bodies or 'no body provided'}."
        elif dismissed:
            # Only dismissed reviews — treat as REVISE
            verdict = "REVISE"
            reasoning = "All reviews were dismissed, indicating iterative feedback was needed."
        else:
            return None  # no actionable review state

        return {
            "verdict": verdict,
            "reasoning": reasoning,
            "label_source": "real_review",
            "review_summary": review_summary,
        }

    # ------------------------------------------------------------------
    # Synthetic label via GPT-4o
    # ------------------------------------------------------------------

    def _should_synthesize(self, snapshot: dict[str, Any]) -> bool:
        issue_num = str(snapshot.get("issue_number", 0))
        digest = int(hashlib.md5(issue_num.encode()).hexdigest(), 16)
        return (digest % 100) < int(GPT4O_SYNTHETIC_FRACTION * 100)

    def _synthetic_verdict(self, snapshot: dict[str, Any], pr: dict) -> dict[str, Any] | None:
        issue = snapshot.get("issue", {})
        files = pr.get("files", [])

        # Build a compact diff summary (first 3 files, truncated)
        diff_parts = []
        for f in files[:3]:
            patch = (f.get("patch") or "")[:600]
            if patch:
                diff_parts.append(f"### {f.get('filename', '')}\n{patch}")
        diff_text = "\n\n".join(diff_parts) or "No diff available."

        user_msg = f"""Issue #{snapshot.get('issue_number')}
Title: {issue.get('title', '')}
Labels: {', '.join(lb.get('name', '') for lb in issue.get('labels', []))}

Issue body:
{self._truncate(issue.get('body', ''), 800)}

PR #{pr.get('number')}: {pr.get('title', '')}
Files changed: {len(files)} | Additions: {pr.get('additions', '?')} | Deletions: {pr.get('deletions', '?')}

Diff excerpt:
{self._truncate(diff_text, 1500)}

Assess whether this patch should be ACCEPT, REVISE, or REJECT."""

        raw = call_gpt4o(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )

        try:
            parsed = json.loads(raw)
            verdict = parsed["verdict"].upper()
            if verdict not in {"ACCEPT", "REVISE", "REJECT"}:
                raise ValueError(f"Invalid verdict: {verdict}")
            return {
                "verdict": verdict,
                "reasoning": parsed.get("reasoning", ""),
                "label_source": "gpt4o_synthetic",
                "review_summary": [],
            }
        except Exception as exc:
            logger.warning("Could not parse GPT-4o critic response: %s. Raw: %s", exc, raw[:200])
            return None

    # ------------------------------------------------------------------

    @staticmethod
    def _pick_resolving_pr(prs: list[dict]) -> dict | None:
        merged = [p for p in prs if p.get("merged_at") or p.get("state") == "closed"]
        return merged[0] if merged else (prs[0] if prs else None)
