"""
Rebuild checkpoint files from exported Snowflake CSVs.
Run this ONLY if the old /tmp/autobot_checkpoints/ is gone from the Docker container.

Usage:
    python3 rebuild_checkpoints.py
"""
import csv
import json
from pathlib import Path

ISSUES_CSV = Path("/Users/shatayu/Downloads/issues_back.csv")
PRS_CSV    = Path("/Users/shatayu/Downloads/prs_back.csv")

# This path is volume-mounted to /opt/airflow/code_v2/checkpoints inside Docker
CHECKPOINT_DIR = Path("/Users/shatayu/Desktop/FALL24/SPRING26/298B/WB2/autobot_dev/etl/checkpoints")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

# ── Issues ────────────────────────────────────────────────────────────────────
issues_processed = []
with open(ISSUES_CSV) as f:
    for row in csv.DictReader(f):
        issues_processed.append(int(float(row["issue_number"])))

# ── PRs ───────────────────────────────────────────────────────────────────────
prs_processed   = []
linked_pr_numbers = {}   # { "issue_number_str": [pr1, pr2, ...] }

with open(PRS_CSV) as f:
    for row in csv.DictReader(f):
        pr_num = int(float(row["pr_number"]))
        prs_processed.append(pr_num)

        raw_issue = row.get("linked_issue_number", "").strip()
        if raw_issue and raw_issue not in ("", "None", "nan"):
            key = str(int(float(raw_issue)))
            linked_pr_numbers.setdefault(key, [])
            if pr_num not in linked_pr_numbers[key]:
                linked_pr_numbers[key].append(pr_num)

# ── Write checkpoints ─────────────────────────────────────────────────────────
json.dump(issues_processed,  open(CHECKPOINT_DIR / "issues_processed.json",  "w"))
json.dump(prs_processed,     open(CHECKPOINT_DIR / "prs_processed.json",     "w"))
json.dump(linked_pr_numbers, open(CHECKPOINT_DIR / "linked_pr_numbers.json", "w"))

print(f"✅ issues_processed.json   → {len(issues_processed)} issues")
print(f"✅ prs_processed.json      → {len(prs_processed)} PRs")
print(f"✅ linked_pr_numbers.json  → {len(linked_pr_numbers)} issue→PR mappings")
print(f"\nCheckpoints written to: {CHECKPOINT_DIR}")
print("(Maps to /opt/airflow/code_v2/checkpoints inside Docker)")
