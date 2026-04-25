"""Train / val / test split for reasoner_v2_labels.jsonl.

Split strategy (per Section 5B.3 of AutoBot_Ref_v6):
  - 80 / 10 / 10 split at the ISSUE level — all tiers of the same issue
    go to the same split to prevent data leakage
  - Stratified by band (medium / high) to preserve distribution
  - Rare evidence types (approved_pr_stalled, closed_without_merge)
    forced into train to ensure they are seen during training
  - validation_passed=False records go to train only
  - Tier balance check after split (T+7 vs T+14+ within each band)

Outputs:
  reasoner_train.jsonl  (~80% of rows)
  reasoner_val.jsonl    (~10% of rows)
  reasoner_test.jsonl   (~10% of rows)
  split_report.txt      (distribution summary + tier balance check)
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEED = 42
TRAIN_RATIO = 0.80
VAL_RATIO = 0.10
TEST_RATIO = 0.10

# Evidence types so rare they must be forced into train
FORCE_TRAIN_EVIDENCE = {"approved_pr_stalled", "closed_without_merge"}

INPUT_FILE = Path("data/labeled/reasoner_v2/reasoner_v2_labels.jsonl")
OUTPUT_DIR = Path("data/splits/reasoner")

# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_records(path: Path) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    print(f"Loaded {len(records)} records from {path.name}")
    return records


# ---------------------------------------------------------------------------
# Core split logic
# ---------------------------------------------------------------------------

def split_records(records: list[dict]) -> tuple[list, list, list]:
    """Split records into train/val/test at the issue level.

    Steps:
      1. Separate out records that must go to train:
         - validation_passed=False
         - contains a rare evidence type (approved_pr_stalled, closed_without_merge)
      2. Group remaining records by issue_number
      3. Assign each unique issue to train/val/test (stratified by band)
      4. Expand issue assignments back to individual rows
    """
    random.seed(SEED)

    force_train_records = []
    eligible_records = []

    for rec in records:
        evidence = set(rec.get("evidence_types", []))
        val_passed = rec.get("validation_passed", True)

        if not val_passed or evidence & FORCE_TRAIN_EVIDENCE:
            force_train_records.append(rec)
        else:
            eligible_records.append(rec)

    print(f"Force-train records: {len(force_train_records)} "
          f"(validation failures + rare evidence types)")
    print(f"Eligible for splitting: {len(eligible_records)}")

    # Group eligible records by issue_number, track dominant band per issue
    issue_to_records: dict[int, list[dict]] = defaultdict(list)
    for rec in eligible_records:
        issue_to_records[rec["issue_number"]].append(rec)

    # Determine dominant band per issue (highest band wins)
    BAND_ORDER = {"medium": 0, "high": 1}
    issue_to_band: dict[int, str] = {}
    for issue_num, recs in issue_to_records.items():
        bands = [r.get("scorer_band", "medium") for r in recs]
        dominant = max(bands, key=lambda b: BAND_ORDER.get(b, 0))
        issue_to_band[issue_num] = dominant

    # Separate issues by band for stratified sampling
    band_issues: dict[str, list[int]] = defaultdict(list)
    for issue_num, band in issue_to_band.items():
        band_issues[band].append(issue_num)

    # Shuffle within each band
    for band in band_issues:
        random.shuffle(band_issues[band])

    # Assign issues to splits within each band
    train_issues: set[int] = set()
    val_issues: set[int] = set()
    test_issues: set[int] = set()

    for band, issues in band_issues.items():
        n = len(issues)
        n_val = max(1, round(n * VAL_RATIO))
        n_test = max(1, round(n * TEST_RATIO))
        n_train = n - n_val - n_test

        train_issues.update(issues[:n_train])
        val_issues.update(issues[n_train:n_train + n_val])
        test_issues.update(issues[n_train + n_val:])

        print(f"  Band '{band}': {n} issues → "
              f"train={n_train}, val={n_val}, test={n_test}")

    # Verify no overlap
    assert not (train_issues & val_issues), "Train/val overlap!"
    assert not (train_issues & test_issues), "Train/test overlap!"
    assert not (val_issues & test_issues), "Val/test overlap!"

    # Expand back to rows — force-train records excluded from val/test
    force_train_issue_nums = {r["issue_number"] for r in force_train_records}

    train_rows = force_train_records.copy()
    val_rows = []
    test_rows = []

    for rec in eligible_records:
        iss = rec["issue_number"]
        if iss in force_train_issue_nums:
            # Issue was force-trained via another tier — keep in train
            train_rows.append(rec)
        elif iss in train_issues:
            train_rows.append(rec)
        elif iss in val_issues:
            val_rows.append(rec)
        elif iss in test_issues:
            test_rows.append(rec)

    return train_rows, val_rows, test_rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def band_dist(rows: list[dict]) -> dict:
    c = Counter(r.get("scorer_band", "?") for r in rows)
    total = sum(c.values())
    return {k: (v, f"{v/total:.1%}") for k, v in c.most_common()}


def tier_dist(rows: list[dict]) -> dict:
    c = Counter(r.get("snapshot_tier", "?") for r in rows)
    return dict(c.most_common())


def evidence_dist(rows: list[dict]) -> dict:
    c = Counter(t for r in rows for t in r.get("evidence_types", []))
    return dict(c.most_common())


def tier_by_band(rows: list[dict]) -> dict:
    """Check tier distribution within each band — catches T+7/T+14 skew."""
    result = defaultdict(Counter)
    for r in rows:
        band = r.get("scorer_band", "?")
        tier = r.get("snapshot_tier", "?")
        result[band][tier] += 1
    return dict(result)


def check_issue_leakage(train: list[dict], val: list[dict], test: list[dict]):
    train_issues = {r["issue_number"] for r in train}
    val_issues = {r["issue_number"] for r in val}
    test_issues = {r["issue_number"] for r in test}

    tv = train_issues & val_issues
    tt = train_issues & test_issues
    vt = val_issues & test_issues

    if tv or tt or vt:
        print(f"  ⚠ LEAKAGE DETECTED: train∩val={len(tv)}, "
              f"train∩test={len(tt)}, val∩test={len(vt)}")
        return False
    print("  ✓ No issue leakage across splits")
    return True


def generate_report(
    train: list[dict],
    val: list[dict],
    test: list[dict],
) -> str:
    total = len(train) + len(val) + len(test)
    lines = [
        "=" * 60,
        "REASONER TRAIN/VAL/TEST SPLIT REPORT",
        "=" * 60,
        "",
        f"Total records : {total}",
        f"Train         : {len(train)} ({len(train)/total:.1%})",
        f"Val           : {len(val)} ({len(val)/total:.1%})",
        f"Test          : {len(test)} ({len(test)/total:.1%})",
        "",
        "--- Band Distribution ---",
    ]

    for split_name, rows in [("Train", train), ("Val", val), ("Test", test)]:
        lines.append(f"  {split_name}:")
        for band, (count, pct) in band_dist(rows).items():
            lines.append(f"    {band}: {count} ({pct})")

    lines += ["", "--- Tier Distribution ---"]
    for split_name, rows in [("Train", train), ("Val", val), ("Test", test)]:
        lines.append(f"  {split_name}: {tier_dist(rows)}")

    lines += ["", "--- Tier by Band (skew check) ---"]
    for split_name, rows in [("Train", train), ("Val", val), ("Test", test)]:
        lines.append(f"  {split_name}:")
        for band, tiers in tier_by_band(rows).items():
            total_band = sum(tiers.values())
            tier_pcts = {t: f"{c/total_band:.0%}" for t, c in sorted(tiers.items())}
            lines.append(f"    {band}: {tier_pcts}")

    lines += ["", "--- Evidence Type Distribution (train) ---"]
    for ev_type, count in evidence_dist(train).items():
        lines.append(f"  {ev_type}: {count}")

    lines += ["", "--- Rare Evidence Types in Val/Test ---"]
    for split_name, rows in [("Val", val), ("Test", test)]:
        ev = evidence_dist(rows)
        rare = {k: v for k, v in ev.items() if k in FORCE_TRAIN_EVIDENCE}
        lines.append(f"  {split_name}: {rare if rare else 'none (expected)'}")

    lines += ["", "--- Validation Failures ---"]
    for split_name, rows in [("Train", train), ("Val", val), ("Test", test)]:
        failed = sum(1 for r in rows if not r.get("validation_passed", True))
        lines.append(f"  {split_name}: {failed} validation_passed=False records")

    lines += ["", "--- Issue Leakage Check ---"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_jsonl(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"Wrote {len(rows)} records → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    records = load_records(INPUT_FILE)

    print("\nSplitting records...")
    train, val, test = split_records(records)

    print(f"\nFinal split: train={len(train)}, val={len(val)}, test={len(test)}")

    report = generate_report(train, val, test)
    print("\n" + report)

    # Leakage check
    print("")
    check_issue_leakage(train, val, test)

    # Write
    print("")
    write_jsonl(train, OUTPUT_DIR / "reasoner_train.jsonl")
    write_jsonl(val,   OUTPUT_DIR / "reasoner_val.jsonl")
    write_jsonl(test,  OUTPUT_DIR / "reasoner_test.jsonl")

    report_path = OUTPUT_DIR / "split_report.txt"
    report_path.write_text(report)
    print(f"Report written → {report_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()