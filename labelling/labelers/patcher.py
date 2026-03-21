"""Patcher labeler — Model 4 (Qwen2.5-Coder-7B + LoRA #2).

Label strategy: 100% programmatic — extract unified diffs directly from PR files.
No GPT-4o needed.

Output label:
{
  "patches": [
    {
      "filename": "path/to/file.py",
      "status": "modified",      # added | modified | deleted | renamed
      "patch": "<unified diff>", # the raw patch text from the GitHub API
      "additions": int,
      "deletions": int,
    },
    ...
  ],
  "total_additions": int,
  "total_deletions": int,
  "pr_number": int,
}

Files with no patch text (e.g. binary files) are excluded.
Examples with no linked PR are skipped (returns None).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .base import BaseLabeler

logger = logging.getLogger(__name__)

# Diffs larger than this are truncated to keep training examples manageable.
MAX_PATCH_CHARS = 8192


class PatcherLabeler(BaseLabeler):
    model_name = "patcher"

    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        linked_prs = snapshot.get("linked_prs", [])
        if not linked_prs:
            logger.debug("Issue %s has no linked PRs — skipping patcher label", snapshot.get("issue_number"))
            return None

        pr = self._pick_resolving_pr(linked_prs)
        if pr is None:
            return None

        files = pr.get("files", [])
        patches = []
        total_add = 0
        total_del = 0

        for f in files:
            raw_patch = f.get("patch")
            if not raw_patch:
                continue  # binary file or no diff available

            patch_text = raw_patch if len(raw_patch) <= MAX_PATCH_CHARS else raw_patch[:MAX_PATCH_CHARS] + "\n# [truncated]"
            additions = f.get("additions", 0)
            deletions = f.get("deletions", 0)

            patches.append({
                "filename": f.get("filename", ""),
                "status": f.get("status", "modified"),
                "patch": patch_text,
                "additions": additions,
                "deletions": deletions,
            })
            total_add += additions
            total_del += deletions

        if not patches:
            logger.debug("Issue %s PR #%s has no text patches — skipping", snapshot.get("issue_number"), pr.get("number"))
            return None

        return {
            "patches": patches,
            "total_additions": total_add,
            "total_deletions": total_del,
            "pr_number": pr.get("number"),
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _pick_resolving_pr(prs: list[dict]) -> dict | None:
        merged = [p for p in prs if p.get("merged_at") or p.get("state") == "closed"]
        return merged[0] if merged else (prs[0] if prs else None)
