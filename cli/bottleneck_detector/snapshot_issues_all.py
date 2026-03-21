import pandas as pd
import json
import re
from datetime import timedelta
from pathlib import Path


def normalize_ts(ts):
    """
    Parse to pandas Timestamp in UTC, then drop tz info so all comparisons are naive.
    Avoids: Cannot compare tz-naive and tz-aware timestamps.
    """
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return pd.NaT
    t = pd.to_datetime(ts, utc=True)
    if isinstance(t, pd.Timestamp) and t.tz is not None:
        return t.tz_convert("UTC").tz_localize(None)
    return t


# --- DIRECTORY CONFIGURATION ---
# Assumes script lives in: <root>/cli/bottleneck_detector/snapshot_issues_all.py
AUTOBOT_DEV_ROOT = Path(__file__).resolve().parents[2]
INPUT_DIR = AUTOBOT_DEV_ROOT.parent / "adhoc" / "Autobot csvs"
INPUT_ISSUES_CSV = INPUT_DIR / "cleaned_issues.csv"
INPUT_PRS_CSV = INPUT_DIR / "raw_prs.csv"

OUTPUT_DIR = AUTOBOT_DEV_ROOT / "cli" / "bottleneck_detector" / "outputs"
OUTPUT_CSV = OUTPUT_DIR / "snapshot_issues_all.csv"

# Harvesting Schedule: P75=6, P90=22, P95=44
# Using a 7-day frequency to multiply samples for the 'long tail' issues.
SNAPSHOT_SCHEDULE = [7, 15, 22, 29, 36, 44]

def extract_linked_prs(issue_body, comments, timeline, snapshot_date):
    """Re-derives linked PRs at snapshot time via regex and timeline events."""
    snap = normalize_ts(snapshot_date)
    pr_pattern = re.compile(
        r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*#(\d+)", re.IGNORECASE
    )
    linked_prs = set()

    if isinstance(issue_body, str):
        linked_prs.update(int(m) for m in pr_pattern.findall(issue_body))

    for comment in comments:
        if normalize_ts(comment["created_at"]) <= snap:
            linked_prs.update(int(m) for m in pr_pattern.findall(comment.get("body", "")))

    for event in timeline:
        if normalize_ts(event.get("created_at", "2099-01-01")) <= snap:
            etype = event.get("event", "")
            source = event.get("source", {})
            issue_ref = source.get("issue", {})
            if etype == "cross-referenced" and issue_ref.get("pull_request"):
                num = issue_ref.get("number")
                if num is not None:
                    linked_prs.add(int(num))
            if etype == "connected":
                num = issue_ref.get("number")
                if num is not None:
                    linked_prs.add(int(num))

    return linked_prs  # set of int

def clean_json_for_snapshot(raw_json_str, snap_date):
    """Filters activity to cutoff and strips all closure/future metadata."""
    snap = normalize_ts(snap_date)
    try:
        data = json.loads(raw_json_str)
        # Filter comments and timeline
        data["comments"] = [
            c
            for c in data.get("comments", [])
            if normalize_ts(c["created_at"]) <= snap
        ]
        data["timeline"] = [
            e
            for e in data.get("timeline", [])
            if normalize_ts(e.get("created_at", "2099-01-01")) <= snap
        ]

        # Explicitly mask all closure fields on the issue metadata
        if "issue" in data and isinstance(data["issue"], dict):
            data["issue"].update({
                "state": "open",
                "closed_at": None,
                "state_reason": None,
                "closed_by": None,
                "updated_at": None
            })

        # Explicitly mask all closure/merge fields on the linked PR metadata if present
        if "pr" in data and isinstance(data["pr"], dict):
            data["pr"].update({
                "state": "open", 
                "closed_at": None, 
                "merged_at": None
            })

        return data, json.dumps(data)
    except Exception as exc:
        print(f"Error cleaning JSON: {exc}")
        return None, None

def process_snapshots():
    # 1. Verify Inputs
    if not INPUT_ISSUES_CSV.is_file():
        raise FileNotFoundError(f"Missing: {INPUT_ISSUES_CSV}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Load PR metadata for creation-date filtering (optional file)
    pr_creation_map = {}
    if INPUT_PRS_CSV.is_file():
        print(f"Loading PR dates from {INPUT_PRS_CSV}...")
        df_prs = pd.read_csv(INPUT_PRS_CSV)
        df_prs["CREATED_AT"] = df_prs["CREATED_AT"].map(normalize_ts)
        pr_creation_map = dict(
            zip(df_prs["PR_NUMBER"].astype(int), df_prs["CREATED_AT"])
        )
    else:
        print(
            f"Warning: {INPUT_PRS_CSV} not found — "
            "linked_pr_numbers from JSON will not be filtered by PR created_at."
        )

    # 3. Load Main Issues
    print(f"Reading {INPUT_ISSUES_CSV}...")
    df_issues = pd.read_csv(INPUT_ISSUES_CSV)
    df_issues["CREATED_AT"] = df_issues["CREATED_AT"].map(normalize_ts)
    df_issues["CLOSED_AT"] = df_issues["CLOSED_AT"].map(normalize_ts)
    
    final_dataset = []

    # 4. Snapshot Loop
    for days in SNAPSHOT_SCHEDULE:
        print(f"Harvesting T+{days} snapshots...")
        # Open-Only Gate: Was the issue open at this specific timestamp?
        mask_open = (df_issues['CLOSED_AT'].isna()) | (df_issues['CLOSED_AT'] > (df_issues['CREATED_AT'] + timedelta(days=days)))
        tier_df = df_issues[mask_open].copy()
        
        for _, row in tier_df.iterrows():
            try:
                snap_date = normalize_ts(row["CREATED_AT"]) + timedelta(days=days)

                # Clean JSON and Mask Data
                json_obj, masked_json_str = clean_json_for_snapshot(
                    row["RAW_JSON"], snap_date
                )
                if not json_obj:
                    continue

                # Re-derive PRs from Regex/Timeline
                issue_body = (json_obj.get("issue") or {}).get("body") or ""
                regex_prs = extract_linked_prs(
                    f"{row['TITLE']} {issue_body}",
                    json_obj["comments"],
                    json_obj["timeline"],
                    snap_date,
                )

                # Filter Official PR list found in JSON using creation-date map
                official_prs = json_obj.get("linked_pr_numbers") or []
                valid_official_prs = set()
                for pr_num in official_prs:
                    try:
                        n = int(pr_num)
                    except (TypeError, ValueError):
                        continue
                    if n in pr_creation_map and normalize_ts(pr_creation_map[n]) <= snap_date:
                        valid_official_prs.add(n)

                # Final Temporal-Consistent PR Set
                total_valid_prs = list(regex_prs | valid_official_prs)

                final_dataset.append(
                    {
                        "ISSUE_NUMBER": row["ISSUE_NUMBER"],
                        "REPO": row["REPO"],
                        "TITLE": row["TITLE"],
                        "STATE": "open",
                        "LABELS": row["LABELS"],
                        "ASSIGNEE_COUNT": row["ASSIGNEE_COUNT"],
                        "MILESTONE": row["MILESTONE"],
                        "COMMENT_COUNT_AT_SNAPSHOT": len(json_obj["comments"]),
                        "LINKED_PR_COUNT_AT_SNAPSHOT": len(total_valid_prs),
                        "CREATED_AT": row["CREATED_AT"],
                        "SNAPSHOT_DATE": snap_date,
                        "DAYS_OPEN": days,
                        "SNAPSHOT_TIER": f"T+{days}",
                        "RAW_JSON_SNAPSHOT": masked_json_str,
                    }
                )
            except Exception as exc:
                print(f"Error processing issue {row.get('ISSUE_NUMBER', '?')}: {exc}")

    # 5. Output Results
    output_df = pd.DataFrame(final_dataset)
    output_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSuccess! Dataset saved to {OUTPUT_CSV.resolve()}")
    print(f"Total Rows: {len(output_df)}")
    print(f"Tier Breakdown:\n{output_df['SNAPSHOT_TIER'].value_counts().sort_index()}")

if __name__ == "__main__":
    process_snapshots()