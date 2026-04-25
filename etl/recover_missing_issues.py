#!/usr/bin/env python3
"""
recover_missing_issues.py
=========================
Identifies issue numbers that are in the checkpoint but NOT in any JSONL file
(caused by DNS failures during extraction where the checkpoint marked items as
done but no data was actually written).

This script:
  1. Scans all GITHUB_ISSUES_*.jsonl for actual issue numbers on disk
  2. Reads the checkpoint (issues_processed.json)
  3. Computes the difference = missing issues
  4. Saves missing numbers to missing_issues.json (audit trail)
  5. Rewrites checkpoint with ONLY the issues that have JSONL data

After running this, retrigger extract_issues in Airflow.
It will re-discover ~12,352 issues, subtract the ~8,551 in checkpoint,
and fetch only the ~3,801 missing ones (~30-60 min with stable network).

Usage:
    python3 recover_missing_issues.py           # dry run (shows what would change)
    python3 recover_missing_issues.py --apply   # actually modifies checkpoint
"""

import json
import glob
import shutil
import argparse
from pathlib import Path
from datetime import datetime

EXTRACTED_DIR  = Path(__file__).parent / "extracted_data"
CHECKPOINT_DIR = Path(__file__).parent / "checkpoints"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually modify the checkpoint. Without this flag, dry-run only.")
    args = parser.parse_args()

    # ── 1. Scan JSONL for actual issue numbers on disk ──────────────────────
    jsonl_nums = set()
    files = sorted(EXTRACTED_DIR.glob("GITHUB_ISSUES_*.jsonl"))
    for f in files:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    n = rec.get("issue_number")
                    if n is not None:
                        jsonl_nums.add(int(n))
                except Exception:
                    pass

    print(f"JSONL files scanned: {len(files)}")
    print(f"Unique issues in JSONL: {len(jsonl_nums)}")

    # ── 2. Read checkpoint (handle corrupted str entries) ───────────────────
    ckpt_path = CHECKPOINT_DIR / "issues_processed.json"
    if not ckpt_path.exists():
        print(f"ERROR: Checkpoint not found at {ckpt_path}")
        return

    raw_ckpt = json.load(open(ckpt_path))
    ckpt_nums = set()
    for entry in raw_ckpt:
        if isinstance(entry, int):
            ckpt_nums.add(entry)
        elif isinstance(entry, str):
            # Handle corrupted entry: JSON-encoded list as a string
            try:
                parsed = json.loads(entry)
                if isinstance(parsed, list):
                    ckpt_nums.update(int(x) for x in parsed)
                else:
                    ckpt_nums.add(int(entry))
            except (json.JSONDecodeError, ValueError):
                pass
        elif isinstance(entry, float):
            ckpt_nums.add(int(entry))

    print(f"Checkpoint entries (raw): {len(raw_ckpt)}")
    print(f"Checkpoint unique ints:   {len(ckpt_nums)}")

    # ── 3. Compute missing ─────────────────────────────────────────────────
    missing = sorted(ckpt_nums - jsonl_nums)
    verified = sorted(ckpt_nums & jsonl_nums)

    print(f"\n{'='*50}")
    print(f"Issues WITH JSONL data (keep in checkpoint): {len(verified)}")
    print(f"Issues WITHOUT JSONL data (MISSING):         {len(missing)}")
    print(f"Missing range: {missing[0]}..{missing[-1]}" if missing else "No missing issues!")
    print(f"{'='*50}")

    if not missing:
        print("\nNothing to recover. All checkpoint entries have JSONL data.")
        return

    # ── 4. Save missing list (audit trail) ─────────────────────────────────
    missing_path = CHECKPOINT_DIR / "missing_issues.json"
    json.dump(missing, open(missing_path, "w"), indent=2)
    print(f"\nSaved {len(missing)} missing issue numbers → {missing_path}")

    if not args.apply:
        print(f"\n⚠️  DRY RUN — no changes made. Run with --apply to fix checkpoint.")
        print(f"   This will keep {len(verified)} issues in checkpoint")
        print(f"   and remove {len(missing)} so they get re-fetched.")
        return

    # ── 5. Backup and rewrite checkpoint ───────────────────────────────────
    backup_name = f"issues_processed.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path = CHECKPOINT_DIR / backup_name
    shutil.copy2(ckpt_path, backup_path)
    print(f"Backup saved → {backup_path}")

    json.dump(verified, open(ckpt_path, "w"))
    print(f"Checkpoint rewritten: {len(verified)} verified issue numbers")
    print(f"\n✅ Done. Now retrigger extract_issues in Airflow.")
    print(f"   It will discover ~12,352 issues, subtract {len(verified)} done, fetch {len(missing)} remaining.")


if __name__ == "__main__":
    main()
