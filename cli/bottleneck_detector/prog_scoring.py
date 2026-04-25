import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
import csv
import random

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(levelname)-8s %(message)s')
ROOT_DIR = Path(__file__).resolve().parents[2]
ISSUES_JSONL = ROOT_DIR / "etl" / "training_data" / "issues_clean.jsonl"
PRS_JSONL = ROOT_DIR / "etl" / "training_data" / "prs_clean.jsonl"
OUTPUT_DIR = ROOT_DIR / "cli" / "bottleneck_detector" / "outputs"
OUTPUT_CSV = OUTPUT_DIR / "scored_snapshots.csv"
BALANCED_CSV = OUTPUT_DIR / "balanced_4000_sample.csv"

# Snapshot anchors
SNAPSHOT_DAYS = [1, 7, 14, 28, 45, 60]
P75_DAYS = 82  # Our computed P75

def parse_ts(ts_str):
    if not ts_str or ts_str == 'None' or ts_str == 'null':
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except:
        return None

def compute_programmatic_score(days_open, issue, snapshot_date, pr_map):
    """Computes the 60% programmatic bottleneck score based on temporally cut-off signals."""
    score = 0.0
    
    # 1. Base Age Score (max 0.4 at P75)
    base = min(days_open / P75_DAYS, 1.0) * 0.4
    score += base
    
    # 2. Extract temporally valid comments
    comments = []
    for c in (issue.get('comments') or []):
        ts = parse_ts(c.get('created_at'))
        if ts and ts <= snapshot_date:
            comments.append(ts)
            
    # Max comment gap
    if len(comments) >= 2:
        comments.sort()
        gaps = [(comments[i] - comments[i-1]).total_seconds() / 86400 for i in range(1, len(comments))]
        if max(gaps) > 14:
            score += 0.15
    elif len(comments) == 1:
        gap = (snapshot_date - comments[0]).total_seconds() / 86400
        if gap > 14:
            score += 0.15
            
    # 3. Timeline Events temporally valid
    timeline = []
    for e in (issue.get('timeline') or []):
        ts = parse_ts(e.get('created_at'))
        if ts and ts <= snapshot_date:
            timeline.append(e)
            
    assignee_changes = sum(1 for e in timeline if e.get('event') == 'assigned')
    if assignee_changes > 1:
        score += 0.10
        
    has_sub_issues = any(e.get('event') == 'added_to_project' for e in timeline)
    if has_sub_issues:
        score += 0.05
        
    # 4. Check linked PRs
    linked_prs = issue.get('linked_pr_numbers') or []
    for pr_num in linked_prs:
        if str(pr_num) in pr_map:
            pr_data = pr_map[str(pr_num)]
            pr_created = parse_ts(pr_data.get('pr', {}).get('created_at'))
            
            # Did this PR exist at the time of snapshot?
            if not pr_created or pr_created > snapshot_date:
                continue
                
            # Review cycles
            review_count = 0
            first_review_ts = None
            for rev in pr_data.get('reviews', []):
                rev_ts = parse_ts(rev.get('submitted_at'))
                if rev_ts and rev_ts <= snapshot_date:
                    review_count += 1
                    if first_review_ts is None or rev_ts < first_review_ts:
                        first_review_ts = rev_ts
                        
            if first_review_ts:
                days_to_review = (first_review_ts - pr_created).total_seconds() / 86400
                if days_to_review > 7:
                    score += 0.15
                    
            if review_count > 3:
                score += 0.10
                
            # Closed without merge BEFORE snapshot?
            pr_closed = parse_ts(pr_data.get('pr', {}).get('closed_at'))
            pr_merged = pr_data.get('pr', {}).get('merged_at')
            if pr_closed and pr_closed <= snapshot_date:
                if not pr_merged or pr_merged == 'None' or pr_merged == 'null':
                    score += 0.15
                    
            # Silent Reviewers
            req_rev = pr_data.get('pr', {}).get('requested_reviewers', [])
            if req_rev and review_count == 0:
                score += 0.10
                
            # CI Failed (pure failure prior to snapshot)
            ci_failed_count = sum(1 for cr in pr_data.get('check_runs', []) if cr.get('conclusion') == 'failure' and parse_ts(cr.get('completed_at', cr.get('started_at'))) and parse_ts(cr.get('completed_at', cr.get('started_at'))) <= snapshot_date)
            has_success = any(cr.get('conclusion') == 'success' for cr in pr_data.get('check_runs', []) if parse_ts(cr.get('completed_at', cr.get('started_at'))) and parse_ts(cr.get('completed_at', cr.get('started_at'))) <= snapshot_date)
            
            if ci_failed_count > 0 and not has_success:
                score += 0.15
                
    return min(score, 1.0)


def main():
    if not ISSUES_JSONL.exists() or not PRS_JSONL.exists():
        logging.error("Clean JSONL files not found. Run clean_and_consolidate.py first.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    logging.info("Loading PR data map...")
    pr_map = {}
    with open(PRS_JSONL) as f:
        for line in f:
            rec = json.loads(line)
            pr_map[str(rec['pr_number'])] = rec
            
    logging.info("Processing issues into snapshots based on T+1,7,14,28,45,60...")
    snapshots = []
    
    with open(ISSUES_JSONL) as f:
        for line in f:
            rec = json.loads(line)
            issue = rec.get('issue', {})
            created_at = parse_ts(issue.get('created_at'))
            closed_at = parse_ts(issue.get('closed_at'))
            
            if not created_at: continue
                
            # Filter Extreme Outliers > 528 Days (P95)
            final_days = None
            if closed_at:
                final_days = (closed_at - created_at).total_seconds() / 86400
                if final_days > 528: 
                    continue # Skip 1.5+ year tracker epics
            
            for days in SNAPSHOT_DAYS:
                target_date = created_at + timedelta(days=days)
                
                # Check if issue was physically open on target_date
                if closed_at and closed_at < target_date:
                    continue
                    
                score = compute_programmatic_score(days, rec, target_date, pr_map)
                snapshots.append({
                    "issue_number": rec.get("issue_number"),
                    "title": issue.get("title"),
                    "snapshot_tier": f"T+{days}",
                    "snapshot_date": target_date.isoformat(),
                    "days_open_at_snapshot": days,
                    "final_resolution_days": final_days,
                    "prog_score": round(score, 3)
                })
                
    logging.info(f"Generated {len(snapshots)} total snapshots.")
    
    scores = [s['prog_score'] for s in snapshots]
    scores.sort()
    n = len(scores)
    
    # Dynamic Quantile-based Bucketing
    p33 = scores[int(n*0.33)]
    p66 = scores[int(n*0.66)]
    
    logging.info(f"Dynamic Band Thresholds computed: Low <= {p33:.3f} < Medium <= {p66:.3f} < High")
    
    for s in snapshots:
        if s['prog_score'] <= p33:
            s['band'] = "Low"
        elif s['prog_score'] <= p66:
            s['band'] = "Medium"
        else:
            s['band'] = "High"
            
    with open(OUTPUT_CSV, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=snapshots[0].keys())
        writer.writeheader()
        writer.writerows(snapshots)
    
    logging.info("\n--- Overall Score Distribution ---")
    scores = [s['prog_score'] for s in snapshots]
    if scores:
        scores.sort()
        n = len(scores)
        print(f"Count: {n}")
        print(f"Mean:  {sum(scores)/n:.3f}")
        print(f"Min:   {scores[0]:.3f}")
        print(f"25%:   {scores[int(n*0.25)]:.3f}")
        print(f"50%:   {scores[int(n*0.50)]:.3f}")
        print(f"75%:   {scores[int(n*0.75)]:.3f}")
        print(f"Max:   {scores[-1]:.3f}")
    
    logging.info("\n--- Band Distribution ---")
    band_counts = {"Low": 0, "Medium": 0, "High": 0}
    for s in snapshots:
        band_counts[s['band']] += 1
    for b, c in band_counts.items():
        print(f"{b}: {c}")
    
    # Stratified balanced sampling (4000 total: ~1333 per band)
    target_per_band = 1333
    final_sample = []
    
    for band in ["Low", "Medium", "High"]:
        band_items = [s for s in snapshots if s['band'] == band]
        if len(band_items) >= target_per_band:
            final_sample.extend(random.sample(band_items, target_per_band))
        else:
            final_sample.extend(band_items)
            
    with open(BALANCED_CSV, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=snapshots[0].keys())
        writer.writeheader()
        writer.writerows(final_sample)
        
    logging.info(f"\nSaved balanced sample ({len(final_sample)} rows) to {BALANCED_CSV.name}")
    logging.info("Send this CSV to GPT-4o for final AI semantic adjustment + narrative labelling.")

if __name__ == "__main__":
    main()
