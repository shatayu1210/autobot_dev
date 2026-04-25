#!/usr/bin/env python3
"""
load_to_snowflake.py
====================
Standalone script to load JSONL extracts into a fresh Snowflake trial.
Run this from your Mac — no Airflow or Docker needed.

Prerequisites:
    pip install snowflake-connector-python

Recommended usage (load CLEANED data — bots removed, mapping enriched):
    # Step 1: run clean_and_consolidate.py first
    python3 clean_and_consolidate.py

    # Step 2: load the cleaned output
    python3 load_to_snowflake.py \\
        --account  your-account-id \\
        --user     your_username \\
        --password 'your_password' \\
        --source   cleaned \\
        --mode     both

Alternatively, load raw extracted data (includes bots, unresolved str→int mapping):
    python3 load_to_snowflake.py --source raw ...

Notes on JSONL record structure (from _fetch_issue_full_async / _fetch_pr_full_async):
  Issues:
    TOP-LEVEL: issue_number, repo, label_names, linked_pr_numbers, extracted_at
    NESTED:    issue.* (title, body, state, labels, assignees, milestone, comments, created_at, closed_at)
               comments[], timeline[], sub_issues[]

  PRs:
    TOP-LEVEL: pr_number, repo, linked_issue_number, ci_conclusion, extracted_at
               reviews[], files[], review_comments[], commits[], check_runs[]
               requested_reviewers: {"users": [...], "teams": [...]}   <-- dict, not array
               silent_reviewers: [login, ...]                          <-- flat list
    NESTED:    pr.* (title, body, state, merged_at, additions, deletions, changed_files,
                     base.sha, head.sha, created_at)
"""

import argparse
import json
import logging
from pathlib import Path

import snowflake.connector

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")

# Data directories
EXTRACTED_DATA_DIR = Path(__file__).parent / "extracted_data"   # raw batches
CLEANED_DATA_DIR   = Path(__file__).parent / "training_data"     # output of clean_and_consolidate.py
TEMP_DIR           = Path("/tmp/sf_load_temp")

# ── DDL ───────────────────────────────────────────────────────────────────────

CREATE_ISSUES_DDL = """
CREATE TABLE IF NOT EXISTS GITHUB_ISSUES (
    issue_number          INTEGER         COMMENT 'GitHub issue number (unique key)',
    repo                  VARCHAR(200)    COMMENT 'owner/name e.g. apache/airflow',
    title                 VARCHAR(2000),
    body                  TEXT            COMMENT 'Full issue body (markdown)',
    body_length           INTEGER         COMMENT 'Character count of body — proxy for issue detail',
    state                 VARCHAR(50)     COMMENT 'closed',
    label_names           ARRAY           COMMENT 'Flat string array of label names',
    has_milestone         BOOLEAN,
    milestone             VARCHAR(500),
    assignee_count        INTEGER,
    comment_count         INTEGER         COMMENT 'Total comments on issue',
    linked_pr_count       INTEGER         COMMENT 'PRs cross-referenced in body/comments via fixes/closes keywords',
    days_open             FLOAT           COMMENT 'created_at → closed_at in fractional days',
    created_at            TIMESTAMP_NTZ,
    closed_at             TIMESTAMP_NTZ,
    extracted_at          TIMESTAMP_NTZ,
    raw_json              VARIANT         COMMENT 'Full payload: issue + comments + timeline + sub_issues'
)
COMMENT = 'Closed GitHub issues apache/airflow post-2019, extracted by AutoBot ETL'
"""

CREATE_PRS_DDL = """
CREATE TABLE IF NOT EXISTS GITHUB_PRS (
    pr_number             INTEGER         COMMENT 'GitHub PR number (unique key)',
    repo                  VARCHAR(200),
    linked_issue_number   INTEGER         COMMENT 'Issue this PR resolves (when known from keyword scan)',
    pr_title              VARCHAR(2000),
    pr_body               TEXT            COMMENT 'Full PR description (markdown)',
    state                 VARCHAR(50)     COMMENT 'closed or merged',
    is_merged             BOOLEAN,
    base_sha              VARCHAR(40)     COMMENT 'Target branch HEAD sha',
    head_sha              VARCHAR(40)     COMMENT 'PR branch HEAD sha',
    additions             INTEGER         COMMENT 'Total lines added across all files',
    deletions             INTEGER         COMMENT 'Total lines removed across all files',
    changed_files_count   INTEGER,
    review_count          INTEGER         COMMENT 'Number of submitted review objects',
    reviewer_count        INTEGER         COMMENT 'Requested reviewers (users only)',
    silent_reviewer_count INTEGER         COMMENT 'Requested but never submitted a review',
    ci_conclusion         VARCHAR(50)     COMMENT 'success | failure | mixed | none',
    has_ci                BOOLEAN,
    days_to_merge         FLOAT           COMMENT 'created_at → merged_at in fractional days (NULL if not merged)',
    merged_at             TIMESTAMP_NTZ,
    created_at            TIMESTAMP_NTZ,
    extracted_at          TIMESTAMP_NTZ,
    raw_json              VARIANT         COMMENT 'Full payload: pr + files(diffs) + reviews + check_runs + commits'
)
COMMENT = 'Closed+merged GitHub PRs apache/airflow post-2019, extracted by AutoBot ETL'
"""

# Note on COPY INTO field mappings:
#   $1:issue_number            → top-level key in JSONL record
#   $1:issue:title             → nested under issue{} object (GitHub API response)
#   $1:label_names             → top-level, pre-extracted flat list
#   $1:requested_reviewers:users → nested under dict {"users":[...], "teams":[...]}
#   $1:silent_reviewers        → top-level flat list of login strings

COPY_ISSUES_SQL = """
COPY INTO GITHUB_ISSUES (
    issue_number, repo,
    title, body, body_length,
    state, label_names, has_milestone, milestone,
    assignee_count, comment_count, linked_pr_count, days_open,
    created_at, closed_at, extracted_at,
    raw_json
)
FROM (
    SELECT
        $1:issue_number::INTEGER,
        $1:repo::VARCHAR,

        SUBSTR(COALESCE($1:issue:title::VARCHAR, ''), 1, 2000),
        $1:issue:body::TEXT,
        LENGTH(COALESCE($1:issue:body::TEXT, '')),

        $1:issue:state::VARCHAR,
        $1:label_names::ARRAY,
        CASE WHEN $1:issue:milestone IS NOT NULL AND NOT ($1:issue:milestone = 'null'::VARIANT) THEN TRUE ELSE FALSE END,
        SUBSTR(COALESCE($1:issue:milestone:title::VARCHAR, ''), 1, 500),

        COALESCE(ARRAY_SIZE($1:issue:assignees::ARRAY), 0),
        COALESCE($1:issue:comments::INTEGER, 0),
        COALESCE(ARRAY_SIZE($1:linked_pr_numbers::ARRAY), 0),
        DATEDIFF('hour',
            TRY_TO_TIMESTAMP_NTZ($1:issue:created_at::VARCHAR),
            TRY_TO_TIMESTAMP_NTZ($1:issue:closed_at::VARCHAR)) / 24.0,

        TRY_TO_TIMESTAMP_NTZ($1:issue:created_at::VARCHAR),
        TRY_TO_TIMESTAMP_NTZ($1:issue:closed_at::VARCHAR),
        TRY_TO_TIMESTAMP_NTZ($1:extracted_at::VARCHAR),

        $1

    FROM @GITHUB_ISSUES_LOAD_STG
)
FILE_FORMAT = (TYPE='JSON' STRIP_OUTER_ARRAY=FALSE)
ON_ERROR = CONTINUE
"""

COPY_PRS_SQL = """
COPY INTO GITHUB_PRS (
    pr_number, repo, linked_issue_number,
    pr_title, pr_body, state, is_merged,
    base_sha, head_sha,
    additions, deletions, changed_files_count,
    review_count, reviewer_count, silent_reviewer_count,
    ci_conclusion, has_ci,
    days_to_merge, merged_at, created_at, extracted_at,
    raw_json
)
FROM (
    SELECT
        $1:pr_number::INTEGER,
        $1:repo::VARCHAR,
        $1:linked_issue_number::INTEGER,

        SUBSTR(COALESCE($1:pr:title::VARCHAR, ''), 1, 2000),
        $1:pr:body::TEXT,
        $1:pr:state::VARCHAR,
        CASE WHEN $1:pr:merged_at IS NOT NULL AND NOT ($1:pr:merged_at = 'null'::VARIANT) THEN TRUE ELSE FALSE END,

        $1:pr:base:sha::VARCHAR,
        $1:pr:head:sha::VARCHAR,
        COALESCE($1:pr:additions::INTEGER, 0),
        COALESCE($1:pr:deletions::INTEGER, 0),
        COALESCE($1:pr:changed_files::INTEGER, 0),

        -- reviews is a top-level list
        COALESCE(ARRAY_SIZE($1:reviews::ARRAY), 0),

        -- requested_reviewers is {"users":[...], "teams":[...]} — extract users only
        COALESCE(ARRAY_SIZE($1:requested_reviewers:users::ARRAY), 0),

        -- silent_reviewers is a top-level flat list of login strings
        COALESCE(ARRAY_SIZE($1:silent_reviewers::ARRAY), 0),

        $1:ci_conclusion::VARCHAR,
        CASE WHEN $1:ci_conclusion::VARCHAR NOT IN ('none', '') AND $1:ci_conclusion IS NOT NULL THEN TRUE ELSE FALSE END,

        -- days_to_merge is NULL for closed (not merged) PRs
        CASE WHEN $1:pr:merged_at IS NOT NULL AND NOT ($1:pr:merged_at = 'null'::VARIANT)
             THEN DATEDIFF('hour',
                    TRY_TO_TIMESTAMP_NTZ($1:pr:created_at::VARCHAR),
                    TRY_TO_TIMESTAMP_NTZ($1:pr:merged_at::VARCHAR)) / 24.0
             ELSE NULL END,

        TRY_TO_TIMESTAMP_NTZ($1:pr:merged_at::VARCHAR),
        TRY_TO_TIMESTAMP_NTZ($1:pr:created_at::VARCHAR),
        TRY_TO_TIMESTAMP_NTZ($1:extracted_at::VARCHAR),

        $1

    FROM @GITHUB_PRS_LOAD_STG
)
FILE_FORMAT = (TYPE='JSON' STRIP_OUTER_ARRAY=FALSE)
ON_ERROR = CONTINUE
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_and_dedup(source_dir: Path, glob_pattern: str, key_field: str) -> list:
    """Read all matching JSONL files from source_dir, dedup by key_field keeping latest extracted_at."""
    files = sorted(source_dir.glob(glob_pattern))
    if not files:
        logging.warning(f"No files found matching '{source_dir / glob_pattern}'")
        return []
    logging.info(f"Found {len(files)} files in {source_dir.name}/ matching '{glob_pattern}'")
    best = {}
    skipped = 0
    for f in files:
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                key = rec.get(key_field)
                if key is None:
                    skipped += 1
                    continue
                ts = rec.get("extracted_at", "")
                if key not in best or ts > best[key].get("extracted_at", ""):
                    best[key] = rec
    logging.info(f"  → {len(best)} unique records (skipped {skipped} malformed lines)")
    return list(best.values())


def write_consolidated(records: list, filename: str) -> Path:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TEMP_DIR / filename
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    size_mb = path.stat().st_size / 1024 / 1024
    logging.info(f"  Consolidated JSONL written → {path}  ({size_mb:.1f} MB, {len(records)} records)")
    return path


def sf_exec(cur, sql: str, label: str = ""):
    logging.info(f"  SQL: {label or sql[:80].strip()}")
    cur.execute(sql)


def sf_load_table(cur, table: str, stage: str, jsonl_path: Path, copy_sql: str):
    sf_exec(cur, f"CREATE OR REPLACE TEMPORARY STAGE {stage}", f"CREATE STAGE {stage}")
    sf_exec(cur, f"PUT file://{jsonl_path} @{stage} AUTO_COMPRESS=TRUE OVERWRITE=TRUE", f"PUT → @{stage}")
    rows_before = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    cur.execute(copy_sql)
    result = cur.fetchone()
    rows_after = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    logging.info(f"  COPY result: {result}")
    logging.info(f"  Rows in {table}: {rows_before} → {rows_after} (+{rows_after - rows_before})")
    return rows_after - rows_before


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Load AutoBot JSONL extracts into Snowflake")
    parser.add_argument("--account",   required=True,
                        help="Snowflake account identifier (e.g. wztrwxd-kob06981)")
    parser.add_argument("--user",      required=True)
    parser.add_argument("--password",  required=True)
    parser.add_argument("--database",  default="AIRFLOW_ML")
    parser.add_argument("--schema",    default="RAW")
    parser.add_argument("--warehouse", default="COMPUTE_WH")
    parser.add_argument("--mode",      choices=["issues", "prs", "both"], default="both")
    parser.add_argument("--source",    choices=["raw", "cleaned"], default="cleaned",
                        help="'cleaned' (default): reads training_data/issues_clean.jsonl + prs_clean.jsonl "
                             "after running clean_and_consolidate.py. "
                             "'raw': reads all GITHUB_ISSUES_*.jsonl + GITHUB_PRS_*.jsonl from extracted_data/.")
    parser.add_argument("--recreate",  action="store_true",
                        help="DROP and recreate tables (wipes existing data)")
    args = parser.parse_args()

    # Select source directory and file patterns
    if args.source == "cleaned":
        source_dir     = CLEANED_DATA_DIR
        issues_pattern = "issues_clean.jsonl"
        prs_pattern    = "prs_clean.jsonl"
        logging.info("Source: CLEANED (training_data/) — bots removed, issue→PR mapping enriched")
        if not (source_dir / issues_pattern).exists() and not (source_dir / prs_pattern).exists():
            logging.error(
                f"Cleaned files not found in {source_dir}. "
                "Run 'python3 clean_and_consolidate.py' first."
            )
            return
    else:
        source_dir     = EXTRACTED_DATA_DIR
        issues_pattern = "GITHUB_ISSUES_*.jsonl"
        prs_pattern    = "GITHUB_PRS_*.jsonl"
        logging.info("Source: RAW (extracted_data/) — includes bots, linked_issue_number as string")

    logging.info(f"Connecting → account={args.account}  db={args.database}.{args.schema}")
    conn = snowflake.connector.connect(
        account=args.account,
        user=args.user,
        password=args.password,
        warehouse=args.warehouse,
        session_parameters={"QUERY_TAG": "autobot_etl_load"},
    )

    try:
        with conn.cursor() as cur:
            # ── Bootstrap database + schema (safe on fresh account) ──────────
            sf_exec(cur, f"CREATE DATABASE IF NOT EXISTS {args.database}",
                    f"CREATE DATABASE IF NOT EXISTS {args.database}")
            sf_exec(cur, f"USE DATABASE {args.database}")
            sf_exec(cur, f"CREATE SCHEMA IF NOT EXISTS {args.schema}",
                    f"CREATE SCHEMA IF NOT EXISTS {args.schema}")
            sf_exec(cur, f"USE SCHEMA {args.schema}")
            sf_exec(cur, f"USE WAREHOUSE {args.warehouse}")

            # ── Issues ────────────────────────────────────────────────────────
            if args.mode in ("issues", "both"):
                logging.info("\n========== LOADING ISSUES ==========")
                if args.recreate:
                    sf_exec(cur, "DROP TABLE IF EXISTS GITHUB_ISSUES")
                cur.execute(CREATE_ISSUES_DDL)
                records = load_and_dedup(source_dir, issues_pattern, "issue_number")
                if records:
                    path = write_consolidated(records, "issues_consolidated.jsonl")
                    loaded = sf_load_table(cur, "GITHUB_ISSUES", "GITHUB_ISSUES_LOAD_STG",
                                           path, COPY_ISSUES_SQL)
                    conn.commit()
                    logging.info(f"✅  Issues complete — {len(records)} unique, {loaded} new rows inserted")
                else:
                    logging.warning(f"⚠️   No issue records found in {source_dir}")

            # ── PRs ───────────────────────────────────────────────────────────
            if args.mode in ("prs", "both"):
                logging.info("\n========== LOADING PRs ==========")
                if args.recreate:
                    sf_exec(cur, "DROP TABLE IF EXISTS GITHUB_PRS")
                cur.execute(CREATE_PRS_DDL)
                records = load_and_dedup(source_dir, prs_pattern, "pr_number")
                if records:
                    path = write_consolidated(records, "prs_consolidated.jsonl")
                    loaded = sf_load_table(cur, "GITHUB_PRS", "GITHUB_PRS_LOAD_STG",
                                           path, COPY_PRS_SQL)
                    conn.commit()
                    logging.info(f"✅  PRs complete — {len(records)} unique, {loaded} new rows inserted")
                else:
                    logging.warning(f"⚠️   No PR records found in {source_dir}")

    finally:
        conn.close()
        logging.info("\nDone.")


if __name__ == "__main__":
    main()
