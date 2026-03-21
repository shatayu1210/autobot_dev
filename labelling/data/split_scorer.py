"""Split scorer labels into train/val/test with issue-level stratification.

Splits on issue_number (not rows) to prevent data leakage across anchors.
Stratifies by dominant band so each split has proportional representation.

Run from labelling/: python -m data.split_scorer
"""

import json
import random
from pathlib import Path
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────────────
INPUT_FILE  = Path(__file__).resolve().parent / "labeled" / "scorer" / "scorer_labels.jsonl"
OUTPUT_DIR  = Path(__file__).resolve().parent / "labeled" / "scorer"
TRAIN_RATIO = 0.80
VAL_RATIO   = 0.10
TEST_RATIO  = 0.10
SEED        = 42

# ── Load all records ─────────────────────────────────────────────────────
records = []
with open(INPUT_FILE) as f:
    for line in f:
        if line.strip():
            records.append(json.loads(line))

print(f"Total records loaded: {len(records)}")

# ── Group by issue_number ────────────────────────────────────────────────
issue_groups: dict[int, list[dict]] = defaultdict(list)
for record in records:
    issue_num = record["issue_number"]
    issue_groups[issue_num].append(record)

print(f"Unique issues: {len(issue_groups)}")

# ── Determine dominant band per issue ────────────────────────────────────
# Use the highest anchor's band as the issue's representative band
BAND_PRIORITY = {"low": 0, "medium": 1, "high": 2}


def get_issue_band(rows: list[dict]) -> str:
    bands = [r["label"]["band_name"] for r in rows]
    return max(bands, key=lambda b: BAND_PRIORITY.get(b, 0))


# Group issue numbers by their dominant band
band_issues: dict[str, list[int]] = defaultdict(list)
for issue_num, rows in issue_groups.items():
    dominant_band = get_issue_band(rows)
    band_issues[dominant_band].append(issue_num)

print("\nIssue distribution by dominant band:")
for band in ["low", "medium", "high"]:
    print(f"  {band:10s}: {len(band_issues.get(band, []))} unique issues")

# ── Stratified split on issue numbers ───────────────────────────────────
random.seed(SEED)
train_issues, val_issues, test_issues = set(), set(), set()

for band, issues in band_issues.items():
    shuffled = issues.copy()
    random.shuffle(shuffled)
    n_total = len(shuffled)
    n_test  = max(1, round(n_total * TEST_RATIO))
    n_val   = max(1, round(n_total * VAL_RATIO))
    n_train = n_total - n_test - n_val

    train_issues.update(shuffled[:n_train])
    val_issues.update(shuffled[n_train:n_train + n_val])
    test_issues.update(shuffled[n_train + n_val:])

print(f"\nIssue split:")
print(f"  Train: {len(train_issues)} issues")
print(f"  Val:   {len(val_issues)} issues")
print(f"  Test:  {len(test_issues)} issues")

# ── Assign rows to splits ────────────────────────────────────────────────
train_records, val_records, test_records = [], [], []
for issue_num, rows in issue_groups.items():
    if issue_num in train_issues:
        train_records.extend(rows)
    elif issue_num in val_issues:
        val_records.extend(rows)
    elif issue_num in test_issues:
        test_records.extend(rows)

# ── Verify no leakage ────────────────────────────────────────────────────
train_nums = {r["issue_number"] for r in train_records}
val_nums   = {r["issue_number"] for r in val_records}
test_nums  = {r["issue_number"] for r in test_records}

assert len(train_nums & val_nums)  == 0, "LEAKAGE: train/val overlap"
assert len(train_nums & test_nums) == 0, "LEAKAGE: train/test overlap"
assert len(val_nums   & test_nums) == 0, "LEAKAGE: val/test overlap"
print("\n✓ No leakage detected")

# ── Write splits ─────────────────────────────────────────────────────────
def write_jsonl(recs: list[dict], path: Path):
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"  Written: {path.name} ({len(recs)} rows)")


write_jsonl(train_records, OUTPUT_DIR / "train.jsonl")
write_jsonl(val_records,   OUTPUT_DIR / "val.jsonl")
write_jsonl(test_records,  OUTPUT_DIR / "test.jsonl")

# ── Final stats ──────────────────────────────────────────────────────────
print("\nFinal row distribution:")
for split_name, split_records in [("Train", train_records), ("Val", val_records), ("Test", test_records)]:
    band_counts = defaultdict(int)
    for r in split_records:
        band_counts[r["label"]["band_name"]] += 1
    total = len(split_records)
    print(f"\n  {split_name} ({total} rows):")
    for band in ["low", "medium", "high"]:
        count = band_counts.get(band, 0)
        pct = count / total * 100 if total else 0
        print(f"    {band:10s}: {count:4d} ({pct:.1f}%)")
