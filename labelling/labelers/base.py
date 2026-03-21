"""Base labeler interface and shared utilities (v2 — CSV-based approach)."""

from __future__ import annotations

import json
import logging
import os
import asyncio
import time
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI, AsyncOpenAI

# Load .env from project root
_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env", override=True)

logger = logging.getLogger(__name__)

# Signals that indicate the issue's resolution — stripped from snapshots
CLOSURE_SIGNALS = [
    r"(?i)lgtm",
    r"(?i)fixed\s+in",
    r"(?i)resolved\s+by",
    r"(?i)closing\s+#\d+",
    r"(?i)merged",
    r"(?i)merging\s+this",
    r"(?i)resolution_days",
    r"(?i)closed_at",
]

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ISSUES_CSV = _DATA_DIR / "snapshot_issues_all.csv"
PRS_CSV = _DATA_DIR / "raw_prs.csv"


def load_pr_lookup() -> dict[int, list[dict]]:
    """Load raw_prs.csv and build a lookup: issue_number -> list of PR dicts.

    Each PR dict contains top-level columns + parsed RAW_JSON fields.
    """
    if not PRS_CSV.is_file():
        logger.warning("PR file not found: %s — PR signals will be empty", PRS_CSV)
        return {}

    df = pd.read_csv(PRS_CSV)
    pr_map: dict[int, list[dict]] = {}

    for _, row in df.iterrows():
        issue_num = int(row["LINKED_ISSUE_NUMBER"])
        pr_dict = row.to_dict()

        # Parse RAW_JSON into the dict
        raw_json_str = pr_dict.pop("RAW_JSON", "{}")
        if pd.isna(raw_json_str):
            raw_json_str = "{}"
        try:
            parsed = json.loads(raw_json_str)
        except Exception:
            parsed = {}
        pr_dict["_parsed"] = parsed

        pr_map.setdefault(issue_num, []).append(pr_dict)

    logger.info("Loaded %d PRs for %d issues from %s", len(df), len(pr_map), PRS_CSV.name)
    return pr_map


def load_issues_csv(limit: int | None = None) -> pd.DataFrame:
    """Load snapshot_issues_all.csv."""
    df = pd.read_csv(ISSUES_CSV)
    if limit:
        df = df.head(limit)
    logger.info("Loaded %d snapshot rows from %s", len(df), ISSUES_CSV.name)
    return df


# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------

_client: OpenAI | None = None


def get_openai_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def call_gpt4o(
    messages: list[dict],
    model: str = "gpt-4o",
    temperature: float = 0.2,
    max_retries: int = 5,
    initial_backoff: float = 2.0,
) -> str:
    """Call GPT-4o with exponential backoff."""
    client = get_openai_client()
    backoff = initial_backoff
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            logger.warning("GPT call failed (attempt %d): %s — retrying in %.1fs", attempt + 1, exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Async OpenAI helpers with rate limiting
# ---------------------------------------------------------------------------

_async_client: AsyncOpenAI | None = None


def get_async_openai_client() -> AsyncOpenAI:
    global _async_client
    if _async_client is None:
        _async_client = AsyncOpenAI()
    return _async_client


class TokenRateLimiter:
    """Simple sliding-window rate limiter for TPM."""

    def __init__(self, tpm_limit: int = 20000):
        self._tpm_limit = tpm_limit
        self._lock = asyncio.Lock()
        self._timestamps: list[tuple[float, int]] = []

    async def acquire(self, estimated_tokens: int = 800):
        while True:
            async with self._lock:
                now = time.time()
                cutoff = now - 60.0
                self._timestamps = [(t, tok) for t, tok in self._timestamps if t > cutoff]
                used = sum(tok for _, tok in self._timestamps)
                if used + estimated_tokens <= self._tpm_limit:
                    self._timestamps.append((now, estimated_tokens))
                    return
            await asyncio.sleep(2.0)


_rate_limiter: TokenRateLimiter | None = None


def get_rate_limiter(tpm_limit: int = 20000) -> TokenRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = TokenRateLimiter(tpm_limit)
    return _rate_limiter


async def async_call_gpt4o(
    messages: list[dict],
    model: str = "gpt-4o",
    temperature: float = 0.2,
    max_retries: int = 8,
    initial_backoff: float = 3.0,
) -> str:
    """Async GPT call with rate limiting and exponential backoff."""
    client = get_async_openai_client()
    limiter = get_rate_limiter()
    await limiter.acquire(estimated_tokens=700)

    backoff = initial_backoff
    for attempt in range(max_retries):
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            err_str = str(exc)
            wait = max(backoff, 5.0) if ("429" in err_str or "rate_limit" in err_str) else backoff
            logger.warning("Async GPT call failed (attempt %d): %s — retrying in %.1fs", attempt + 1, exc, wait)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, 60.0)
    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Base class (CSV-based)
# ---------------------------------------------------------------------------


class BaseLabeler(ABC):
    """Abstract base for CSV-based labelers.

    Reads from snapshot_issues_all.csv + raw_prs.csv, labels each row,
    writes results to a JSONL file with dedup on (issue_number, snapshot_tier).
    """

    model_name: str  # e.g. "scorer"

    def __init__(self, output_dir: Path, dry_run: bool = False):
        self.output_dir = output_dir / self.model_name
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # CSV-based labelling (async)
    # ------------------------------------------------------------------

    async def async_label_all_csv(
        self,
        limit: int | None = None,
        concurrency: int = 10,
    ) -> dict[str, int]:
        """Label all rows from the CSV with async concurrency + progress bar."""
        from tqdm import tqdm

        counts = {"labeled": 0, "skipped": 0, "errors": 0}
        output_file = self.output_dir / f"{self.model_name}_labels.jsonl"
        processed_keys: set[str] = set()

        # Load already-processed keys
        if output_file.exists():
            with open(output_file, "r") as f:
                for line in f:
                    if line.strip():
                        try:
                            record = json.loads(line)
                            key = f"{record.get('issue_number')}_{record.get('snapshot_tier', '')}"
                            processed_keys.add(key)
                        except Exception:
                            pass

        # Load data
        df = load_issues_csv(limit=limit)
        pr_lookup = load_pr_lookup()

        total = len(df)
        pbar = tqdm(total=total, desc=f"Labelling {self.model_name}", unit="issue",
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

        sem = asyncio.Semaphore(concurrency)

        # Build snapshot dicts
        tasks = []
        for _, row in df.iterrows():
            issue_num = int(row["ISSUE_NUMBER"])
            snapshot_tier = str(row["SNAPSHOT_TIER"])
            dedup_key = f"{issue_num}_{snapshot_tier}"

            if dedup_key in processed_keys:
                counts["skipped"] += 1
                pbar.update(1)
                continue

            # Parse RAW_JSON_SNAPSHOT
            raw_json_str = row.get("RAW_JSON_SNAPSHOT", "{}")
            if pd.isna(raw_json_str):
                raw_json_str = "{}"
            try:
                snapshot = json.loads(raw_json_str)
            except Exception:
                snapshot = {}

            # Merge CSV columns into snapshot
            for col in row.index:
                if col == "RAW_JSON_SNAPSHOT":
                    continue
                val = row[col]
                if pd.isna(val):
                    snapshot[col] = None
                else:
                    snapshot[col] = val

            # Normalize days_open
            if "days_open_at_snapshot" not in snapshot:
                snapshot["days_open_at_snapshot"] = (
                    snapshot.get("DAYS_OPEN") or snapshot.get("days_open") or 0
                )

            # Attach linked PRs (filtered by snapshot date)
            snapshot_date = pd.to_datetime(row.get("SNAPSHOT_DATE"), utc=True)
            if snapshot_date and snapshot_date.tzinfo:
                snapshot_date = snapshot_date.tz_localize(None) if snapshot_date.tzinfo is None else snapshot_date.tz_convert(None)

            linked_prs = []
            issue_prs = pr_lookup.get(issue_num, [])
            for pr in issue_prs:
                pr_created = pd.to_datetime(pr.get("CREATED_AT"), utc=True)
                if pr_created and pr_created.tzinfo:
                    pr_created = pr_created.tz_convert(None)
                # Only include PRs created before snapshot date (prevent leakage)
                if snapshot_date is not None and pr_created is not None and pr_created <= snapshot_date:
                    linked_prs.append(pr)

            snapshot["linked_prs"] = linked_prs

            async def _label_task(snap, inum, tier):
                async with sem:
                    try:
                        label = await self.async_label_one(snap)
                        input_data = self._build_input(snap)
                        pbar.update(1)
                        return (inum, tier, label, input_data)
                    except Exception as exc:
                        logger.error("Error labeling %s (%s): %s", inum, tier, exc)
                        pbar.update(1)
                        return (inum, tier, None, None)

            tasks.append(_label_task(snapshot, issue_num, snapshot_tier))

        # Run all tasks
        results = await asyncio.gather(*tasks)

        # Write results
        with open(output_file, "a") as f:
            for issue_num, tier, label, input_data in results:
                if label is None:
                    counts["errors"] += 1
                    continue
                record = {
                    "issue_number": issue_num,
                    "snapshot_tier": tier,
                    "model": self.model_name,
                    "input": input_data,
                    "label": label,
                }
                if not self.dry_run:
                    f.write(json.dumps(record, default=str) + "\n")
                counts["labeled"] += 1

        pbar.close()
        logger.info("Complete — labeled: %d, skipped: %d, errors: %d",
                     counts["labeled"], counts["skipped"], counts["errors"])
        return counts

    # ------------------------------------------------------------------
    # Must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Produce a label dict for a single snapshot."""

    async def async_label_one(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Async version. Override for async GPT calls."""
        return self.label_one(snapshot)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_input(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return the portion of the snapshot used as model input."""
        issue = snapshot.get("issue", {})
        return {
            "issue_number": snapshot.get("ISSUE_NUMBER") or snapshot.get("issue_number"),
            "title": issue.get("title", ""),
            "body": (issue.get("body") or "")[:2000],
            "labels": [lb.get("name") for lb in issue.get("labels", [])],
            "days_open_at_snapshot": snapshot.get("days_open_at_snapshot", 0),
            "snapshot_tier": snapshot.get("SNAPSHOT_TIER", ""),
        }

    @staticmethod
    def _truncate(text: str | None, max_chars: int = 3000) -> str:
        if not text:
            return ""
        return text[:max_chars] if len(text) > max_chars else text

    @classmethod
    def _strip_closure_signals(cls, text: str | None) -> str:
        """Strip signals that reveal the future state of the issue."""
        if not text:
            return ""
        clean_text = text
        for pattern in CLOSURE_SIGNALS:
            clean_text = re.sub(pattern, "[STRIPPED]", clean_text)
        return clean_text
