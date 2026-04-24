#!/usr/bin/env python3
"""
clean_and_consolidate.py
========================
Reads all extracted JSONL batches, deduplicates, removes bot-authored items,
establishes issue→PR mappings, and outputs two clean single-file JSONLs
ready for upload to Google Colab for model training.

Input:
    etl/extracted_data/GITHUB_ISSUES_*.jsonl
    etl/extracted_data/GITHUB_PRS_*.jsonl

Output:
    etl/training_data/issues_clean.jsonl      — one line per unique human issue
    etl/training_data/prs_clean.jsonl         — one line per unique human PR
    etl/training_data/cleaning_report.json    — statistics on what was filtered

Usage:
    python3 clean_and_consolidate.py

No arguments needed. Reads from extracted_data/ relative to this script.
"""

import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
)

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
EXTRACTED_DIR  = SCRIPT_DIR / "extracted_data"
OUTPUT_DIR     = SCRIPT_DIR / "training_data"

# ── Bot detection ─────────────────────────────────────────────────────────────
# Based on actual data scan: only 2 bots exist in your dataset:
#   github-actions[bot]: 1,215 PRs (automated cherry-picks / backports)
#   dependabot[bot]:     1,140 PRs (dependency bumps)
#   Total: ~2,355 / ~41k PRs (6.7%). Zero bot issues.
#
# These are useless for all 5 training models:
#   - Scorer/Reasoner: Bot issues don't exist; bot PRs merge in hours, skewing timing
#   - Planner: Bot PRs aren't decomposed from issues
#   - Patcher: Dependency bumps teach nothing about code generation
#   - Critic:  Bot PRs have no meaningful human review

BOT_SUFFIXES = ["[bot]"]
BOT_TYPES    = {"Bot"}

def is_bot(user_dict: dict) -> bool:
    """Check if a GitHub user object represents a bot."""
    if not isinstance(user_dict, dict):
        return False
    login = user_dict.get("login", "")
    utype = user_dict.get("type", "")
    if utype in BOT_TYPES:
        return True
    for suffix in BOT_SUFFIXES:
        if login.endswith(suffix):
            return True
    return False


# ── JSONL reading + dedup ─────────────────────────────────────────────────────

def load_all_jsonl(glob_pattern: str, key_field: str) -> dict:
    """
    Read all matching JSONL files, deduplicate by key_field.
    Returns dict: {key_value: record} keeping the latest extracted_at per key.
    """
    files = sorted(EXTRACTED_DIR.glob(glob_pattern))
    if not files:
        logging.warning(f"No files matching '{glob_pattern}' in {EXTRACTED_DIR}")
        return {}

    logging.info(f"Reading {len(files)} files matching '{glob_pattern}'")
    best = {}
    total_lines = 0
    malformed = 0

    for f in files:
        with open(f) as fh:
            for line in fh:
                total_lines += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                key = rec.get(key_field)
                if key is None:
                    malformed += 1
                    continue
                ts = rec.get("extracted_at", "")
                if key not in best or ts > best[key].get("extracted_at", ""):
                    best[key] = rec

    logging.info(f"  Read {total_lines} lines → {len(best)} unique by '{key_field}' ({malformed} malformed)")
    return best


# ── Issue→PR mapping ──────────────────────────────────────────────────────────

def build_issue_pr_map(issues: dict, prs: dict) -> dict:
    """
    Build a bidirectional issue↔PR mapping from five sources:
      1. PR's linked_issue_number field (from ETL keyword scan)
      2. Issue's linked_pr_numbers list (from ETL keyword scan)
      3. PR body text scanning for "fixes #NNN" keyword patterns
      4. PR body scanning for GitHub issue URLs
      5. PR title scanning for "fixes #NNN" keyword patterns

    Returns: {issue_number: [pr_number, ...]}
    """
    fix_pattern = re.compile(
        r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)[:\s]*#(\d+)", re.IGNORECASE
    )
    url_pattern = re.compile(
        r"github\.com/apache/airflow/issues/(\d+)", re.IGNORECASE
    )

    issue_to_prs = {}
    source_counts = Counter()  # track which source found each link

    # Source 1: PR → Issue (linked_issue_number from ETL)
    for pr_num, pr_rec in prs.items():
        linked = pr_rec.get("linked_issue_number")
        if linked and str(linked) not in ("None", "null", ""):
            issue_num = int(linked) if not isinstance(linked, int) else linked
            if issue_num in issues:
                issue_to_prs.setdefault(issue_num, set()).add(pr_num)
                source_counts["etl_linked_issue_number"] += 1

    # Source 2: Issue → PRs (linked_pr_numbers from ETL)
    for issue_num, issue_rec in issues.items():
        for pr_num in issue_rec.get("linked_pr_numbers", []):
            if pr_num in prs:
                issue_to_prs.setdefault(issue_num, set()).add(pr_num)
                source_counts["etl_linked_pr_numbers"] += 1

    # Source 3: PR body text scan for "fixes #NNN"
    for pr_num, pr_rec in prs.items():
        body = pr_rec.get("pr", {}).get("body") or ""
        for m in fix_pattern.finditer(body):
            issue_num = int(m.group(1))
            if issue_num in issues:
                issue_to_prs.setdefault(issue_num, set()).add(pr_num)
                source_counts["body_keyword"] += 1

    # Source 4: PR body scan for GitHub issue URLs
    for pr_num, pr_rec in prs.items():
        body = pr_rec.get("pr", {}).get("body") or ""
        for m in url_pattern.finditer(body):
            issue_num = int(m.group(1))
            if issue_num in issues:
                issue_to_prs.setdefault(issue_num, set()).add(pr_num)
                source_counts["body_url"] += 1

    # Source 5: PR title scan for "fixes #NNN"
    for pr_num, pr_rec in prs.items():
        title = pr_rec.get("pr", {}).get("title") or ""
        for m in fix_pattern.finditer(title):
            issue_num = int(m.group(1))
            if issue_num in issues:
                issue_to_prs.setdefault(issue_num, set()).add(pr_num)
                source_counts["title_keyword"] += 1

    logging.info(f"  Link sources: {dict(source_counts)}")

    # Convert sets to sorted lists
    return {k: sorted(v) for k, v in issue_to_prs.items()}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {"timestamp": datetime.utcnow().isoformat(), "filters_applied": []}

    # ── Step 1: Load and dedup ────────────────────────────────────────────────
    logging.info("=" * 60)
    logging.info("STEP 1: Load and deduplicate")
    logging.info("=" * 60)

    raw_issues = load_all_jsonl("GITHUB_ISSUES_*.jsonl", "issue_number")
    raw_prs    = load_all_jsonl("GITHUB_PRS_*.jsonl", "pr_number")

    report["raw_issues"] = len(raw_issues)
    report["raw_prs"]    = len(raw_prs)

    # ── Step 2: Remove bots ───────────────────────────────────────────────────
    logging.info("\n" + "=" * 60)
    logging.info("STEP 2: Remove bot-authored items")
    logging.info("=" * 60)

    bot_issue_logins = Counter()
    bot_pr_logins    = Counter()

    clean_issues = {}
    for key, rec in raw_issues.items():
        user = rec.get("issue", {}).get("user", {})
        if is_bot(user):
            bot_issue_logins[user.get("login", "unknown")] += 1
        else:
            clean_issues[key] = rec

    clean_prs = {}
    for key, rec in raw_prs.items():
        user = rec.get("pr", {}).get("user", {})
        if is_bot(user):
            bot_pr_logins[user.get("login", "unknown")] += 1
        else:
            clean_prs[key] = rec

    logging.info(f"Issues: {len(raw_issues)} → {len(clean_issues)} (removed {len(raw_issues) - len(clean_issues)} bot)")
    logging.info(f"PRs:    {len(raw_prs)} → {len(clean_prs)} (removed {len(raw_prs) - len(clean_prs)} bot)")

    if bot_pr_logins:
        logging.info("Bot PR breakdown:")
        for login, count in bot_pr_logins.most_common():
            logging.info(f"  {login}: {count}")

    report["bot_issues_removed"] = len(raw_issues) - len(clean_issues)
    report["bot_prs_removed"]    = len(raw_prs) - len(clean_prs)
    report["bot_pr_breakdown"]   = dict(bot_pr_logins)
    report["clean_issues"]       = len(clean_issues)
    report["clean_prs"]          = len(clean_prs)
    report["filters_applied"].append("bot_removal")

    # ── Step 3: Build issue→PR mapping ────────────────────────────────────────
    logging.info("\n" + "=" * 60)
    logging.info("STEP 3: Build issue → PR mapping")
    logging.info("=" * 60)

    issue_to_prs = build_issue_pr_map(clean_issues, clean_prs)

    issues_with_prs    = len(issue_to_prs)
    issues_without_prs = len(clean_issues) - issues_with_prs
    prs_linked         = len({p for prs in issue_to_prs.values() for p in prs})
    prs_standalone     = len(clean_prs) - prs_linked

    logging.info(f"Issues with linked PRs: {issues_with_prs}")
    logging.info(f"Issues without PRs:     {issues_without_prs}")
    logging.info(f"PRs linked to issues:   {prs_linked}")
    logging.info(f"PRs standalone:         {prs_standalone}")

    report["issues_with_prs"]    = issues_with_prs
    report["issues_without_prs"] = issues_without_prs
    report["prs_linked"]         = prs_linked
    report["prs_standalone"]     = prs_standalone

    # ── Step 4: Enrich and write ──────────────────────────────────────────────
    logging.info("\n" + "=" * 60)
    logging.info("STEP 4: Enrich records and write clean JSONL")
    logging.info("=" * 60)

    # Enrich issues with resolved_by_prs
    for issue_num, rec in clean_issues.items():
        rec["resolved_by_prs"] = issue_to_prs.get(issue_num, [])
        rec["has_linked_pr"] = len(rec["resolved_by_prs"]) > 0

    # Enrich PRs: ensure linked_issue_number is consistently int or None
    pr_to_issues = {}
    for issue_num, pr_nums in issue_to_prs.items():
        for pr_num in pr_nums:
            pr_to_issues.setdefault(pr_num, []).append(issue_num)

    for pr_num, rec in clean_prs.items():
        linked_issues = pr_to_issues.get(pr_num, [])
        rec["linked_issue_numbers"] = linked_issues
        # Keep the primary linked_issue_number as int or None
        raw_linked = rec.get("linked_issue_number")
        if raw_linked in (None, "None", "null", ""):
            rec["linked_issue_number"] = linked_issues[0] if linked_issues else None
        else:
            rec["linked_issue_number"] = int(raw_linked) if not isinstance(raw_linked, int) else raw_linked

    # Write issues
    issues_path = OUTPUT_DIR / "issues_clean.jsonl"
    with open(issues_path, "w") as f:
        for rec in sorted(clean_issues.values(), key=lambda r: r["issue_number"]):
            f.write(json.dumps(rec) + "\n")
    issues_mb = issues_path.stat().st_size / 1024 / 1024
    logging.info(f"  ✅ {issues_path.name}: {len(clean_issues)} records ({issues_mb:.1f} MB)")

    # Write PRs
    prs_path = OUTPUT_DIR / "prs_clean.jsonl"
    with open(prs_path, "w") as f:
        for rec in sorted(clean_prs.values(), key=lambda r: r["pr_number"]):
            f.write(json.dumps(rec) + "\n")
    prs_mb = prs_path.stat().st_size / 1024 / 1024
    logging.info(f"  ✅ {prs_path.name}: {len(clean_prs)} records ({prs_mb:.1f} MB)")

    # ── Step 5: Summary stats for training planning ───────────────────────────
    logging.info("\n" + "=" * 60)
    logging.info("STEP 5: Training data distribution stats")
    logging.info("=" * 60)

    # Issue label distribution
    label_counts = Counter()
    for rec in clean_issues.values():
        for label in rec.get("label_names", []):
            label_counts[label] += 1

    logging.info(f"\nTop 20 issue labels:")
    for label, count in label_counts.most_common(20):
        logging.info(f"  {label}: {count}")

    # PR merge status
    merged = sum(1 for r in clean_prs.values()
                 if r.get("pr", {}).get("merged_at") and r["pr"]["merged_at"] not in ("None", "null", None))
    closed_only = len(clean_prs) - merged
    logging.info(f"\nPR merge status: {merged} merged, {closed_only} closed-without-merge")

    # CI conclusion distribution
    ci_dist = Counter(r.get("ci_conclusion", "unknown") for r in clean_prs.values())
    logging.info(f"\nCI conclusion distribution:")
    for ci, count in ci_dist.most_common():
        logging.info(f"  {ci}: {count}")

    # Review count distribution (use `or []` because value can be None, not just missing)
    review_counts = [len(r.get("reviews") or []) for r in clean_prs.values()]
    has_reviews = sum(1 for c in review_counts if c > 0)
    logging.info(f"\nPRs with reviews: {has_reviews}/{len(clean_prs)} ({has_reviews/len(clean_prs)*100:.1f}%)")

    # Files-per-PR distribution (relevant for Patcher)
    # Note: 23 PRs have files=None (API returned null), use `or []` to handle
    files_counts = [len(r.get("files") or []) for r in clean_prs.values()]
    logging.info(f"\nFiles per PR: min={min(files_counts)}, "
                 f"median={sorted(files_counts)[len(files_counts)//2]}, "
                 f"max={max(files_counts)}, "
                 f"mean={sum(files_counts)/len(files_counts):.1f}")

    # Days open distribution (issues)
    days_list = []
    for rec in clean_issues.values():
        issue = rec.get("issue", {})
        created = issue.get("created_at")
        closed = issue.get("closed_at")
        if created and closed and created not in ("None",) and closed not in ("None",):
            try:
                c = datetime.fromisoformat(created.replace("Z", "+00:00"))
                d = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                days = (d - c).total_seconds() / 86400
                days_list.append(days)
            except Exception:
                pass

    if days_list:
        days_sorted = sorted(days_list)
        logging.info(f"\nIssue days_open: p10={days_sorted[len(days_sorted)//10]:.1f}, "
                     f"p50={days_sorted[len(days_sorted)//2]:.1f}, "
                     f"p90={days_sorted[int(len(days_sorted)*0.9)]:.1f}, "
                     f"max={days_sorted[-1]:.1f}")

    report["top_labels"]         = dict(label_counts.most_common(30))
    report["prs_merged"]         = merged
    report["prs_closed_only"]    = closed_only
    report["ci_distribution"]    = dict(ci_dist)
    report["prs_with_reviews"]   = has_reviews
    report["median_files_per_pr"] = sorted(files_counts)[len(files_counts) // 2] if files_counts else 0

    # Write report
    report_path = OUTPUT_DIR / "cleaning_report.json"
    json.dump(report, open(report_path, "w"), indent=2)
    logging.info(f"\n  📊 Report written to {report_path.name}")

    logging.info("\n" + "=" * 60)
    logging.info("DONE")
    logging.info(f"  issues_clean.jsonl: {len(clean_issues)} records → upload to Colab")
    logging.info(f"  prs_clean.jsonl:    {len(clean_prs)} records → upload to Colab")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
