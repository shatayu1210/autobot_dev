"""Planner labeler — Model 3 (Qwen2.5-Coder-7B + LoRA #1).

Label strategy: 100% programmatic, derived from PR metadata.
No GPT-4o needed.

Output label:
{
  "target_files": ["path/to/file.py", ...],       # files touched in the resolving PR
  "target_dirs": ["path/to/dir/", ...],            # unique parent dirs
  "commit_sequence": ["<verb>: <summary>", ...],   # commit messages, imperative form
  "approach_summary": str,                          # PR title + first 2 commit messages joined
  "file_count": int,
  "change_types": ["modified", "added", "deleted"]
}

If no linked PR exists the example is skipped (returns None).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .base import BaseLabeler

logger = logging.getLogger(__name__)


class PlannerLabeler(BaseLabeler):
    model_name = "planner"

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        linked_prs = snapshot.get("linked_prs", [])
        if not linked_prs:
            logger.debug("Issue %s has no linked PRs — skipping planner label", snapshot.get("issue_number"))
            return None

        # Use the first merged/closed PR as ground truth
        pr = self._pick_resolving_pr(linked_prs)
        if pr is None:
            return None

        files = pr.get("files", [])
        commits = pr.get("commits", [])

        target_files = [f["filename"] for f in files if f.get("filename")]
        target_dirs = sorted({str(Path(fp).parent) for fp in target_files})
        change_types = sorted({f.get("status", "modified") for f in files})

        commit_sequence = self._extract_commit_sequence(commits)
        approach_summary = self._build_approach_summary(pr, commit_sequence)

        return {
            "target_files": target_files,
            "target_dirs": target_dirs,
            "commit_sequence": commit_sequence,
            "approach_summary": approach_summary,
            "file_count": len(target_files),
            "change_types": change_types,
            "pr_number": pr.get("number"),
            "pr_title": pr.get("title", ""),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_resolving_pr(prs: list[dict]) -> dict | None:
        """Prefer merged PRs; fall back to any closed PR."""
        merged = [p for p in prs if p.get("merged_at") or p.get("state") == "closed"]
        return merged[0] if merged else (prs[0] if prs else None)

    @staticmethod
    def _extract_commit_sequence(commits: list[dict]) -> list[str]:
        """Return commit messages in chronological order, stripped to first line."""
        messages = []
        for c in commits:
            msg = (c.get("commit", {}).get("message") or c.get("message") or "")
            first_line = msg.split("\n")[0].strip()
            if first_line:
                messages.append(first_line)
        return messages

    @staticmethod
    def _build_approach_summary(pr: dict, commit_sequence: list[str]) -> str:
        parts = [pr.get("title", "")]
        parts.extend(commit_sequence[:2])
        return " → ".join(p for p in parts if p)
