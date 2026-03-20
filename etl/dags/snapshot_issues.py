"""
Issue Snapshot DAG (T+7, T+14)
==============================
Reads issue-level records from a cleaned issues table and materializes two
snapshot tables for T+7 and T+14, where T is issue creation timestamp.

Important:
- This is issue-level only (no PR join in this DAG).
- linked_pr_numbers are re-derived from filtered timeline/body/comments
  up to snapshot_date (does not trust precomputed linked_pr_numbers).
"""

from datetime import datetime, timedelta
import json
import logging
import re
import time

from airflow import DAG
from airflow.models import Param
from airflow.operators.python import PythonOperator


default_args = {
    "owner": "autobot",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 0,
    "retry_delay": timedelta(minutes=5),
}

BATCH_SIZE = 200
WRITE_BATCH_SIZE = 50

KEEP_TIMELINE_EVENTS = {
    "labeled",
    "unlabeled",
    "assigned",
    "unassigned",
    "cross-referenced",
    "connected",
    "review_requested",
    "review_request_removed",
    "commented",
    "committed",
    "milestoned",
    "demilestoned",
    "renamed",
    "locked",
    "unlocked",
    "merged",
    "closed",
    "reopened",
    "marked_as_duplicate",
    "unmarked_as_duplicate",
    "transferred",
}


def _parse_fqn(table_fqn):
    parts = [p.strip() for p in table_fqn.split(".")]
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"Table must be fully qualified as DATABASE.SCHEMA.TABLE, got: {table_fqn}"
        )
    return parts[0], parts[1], parts[2]


def _parse_dt(dt_str):
    if not dt_str:
        return None
    dt_str = str(dt_str).strip().replace("Z", "+00:00")
    if " " in dt_str and "+" not in dt_str and "T" not in dt_str:
        dt_str = dt_str.replace(" ", "T") + "+00:00"
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _snapshot_issue(raw_record, days_offset, snapshot_tier):
    issue = raw_record.get("issue", {})
    comments = raw_record.get("comments", [])
    timeline = raw_record.get("timeline", [])

    created_at = _parse_dt(issue.get("created_at"))
    if not created_at:
        return None

    snapshot_date = created_at + timedelta(days=days_offset)

    filtered_timeline = []
    for event in timeline:
        event_dt = _parse_dt(event.get("created_at"))
        if event_dt is None:
            filtered_timeline.append(event)
            continue
        if event_dt <= snapshot_date and event.get("event", "") in KEEP_TIMELINE_EVENTS:
            filtered_timeline.append(event)

    linked_pr_numbers = []
    for event in filtered_timeline:
        etype = event.get("event", "")
        if etype == "cross-referenced":
            src_issue = event.get("source", {}).get("issue", {})
            if src_issue.get("pull_request"):
                ref_num = src_issue.get("number")
                if ref_num:
                    linked_pr_numbers.append(ref_num)
        if etype == "connected":
            ref_num = event.get("source", {}).get("issue", {}).get("number")
            if ref_num:
                linked_pr_numbers.append(ref_num)

    filtered_comments = [
        c
        for c in comments
        if _parse_dt(c.get("created_at")) and _parse_dt(c.get("created_at")) <= snapshot_date
    ]

    fix_pattern = re.compile(
        r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*#(\d+)", re.IGNORECASE
    )
    body_text = (issue.get("body") or "") + " ".join(c.get("body", "") for c in filtered_comments)
    for match in fix_pattern.finditer(body_text):
        linked_pr_numbers.append(int(match.group(1)))
    linked_pr_numbers = list(set(linked_pr_numbers))

    snapshotted_issue = {k: v for k, v in issue.items()}
    snapshotted_issue["state"] = "open"
    snapshotted_issue["closed_at"] = None
    snapshotted_issue["state_reason"] = None
    snapshotted_issue["closed_by"] = None
    snapshotted_issue["updated_at"] = None

    snapshotted_raw = {
        "issue": snapshotted_issue,
        "comments": filtered_comments,
        "sub_issues": raw_record.get("sub_issues", []),
        "timeline": filtered_timeline,
        "linked_pr_numbers": linked_pr_numbers,
        "issue_number": raw_record.get("issue_number"),
        "repo": raw_record.get("repo"),
        "days_open": days_offset,
        "snapshot_tier": snapshot_tier,
        "snapshot_date": snapshot_date.isoformat(),
        "_snapshotted_at": datetime.utcnow().isoformat(),
    }

    return {
        "issue_number": issue.get("number"),
        "repo": raw_record.get("repo", "apache/airflow"),
        "title": (issue.get("title") or "")[:1000],
        "state": "open",
        "labels": json.dumps([l["name"] for l in issue.get("labels", []) if l.get("name")]),
        "assignee_count": len(issue.get("assignees") or []),
        "milestone": ((issue.get("milestone") or {}).get("title") or "")[:500],
        "comment_count_at_snapshot": len(filtered_comments),
        "linked_pr_count_at_snapshot": len(linked_pr_numbers),
        "created_at": issue.get("created_at"),
        "snapshot_date": snapshot_date.isoformat(),
        "days_open": days_offset,
        "snapshot_tier": snapshot_tier,
        "raw_json_snapshot": json.dumps(snapshotted_raw),
    }


TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {db}.{schema}.{table} (
    ISSUE_NUMBER                INTEGER         NOT NULL,
    REPO                        VARCHAR(200)    NOT NULL,
    TITLE                       VARCHAR(1000),
    STATE                       VARCHAR(20)     DEFAULT 'open',
    LABELS                      ARRAY,
    ASSIGNEE_COUNT              INTEGER,
    MILESTONE                   VARCHAR(500),
    COMMENT_COUNT_AT_SNAPSHOT   INTEGER,
    LINKED_PR_COUNT_AT_SNAPSHOT INTEGER,
    CREATED_AT                  TIMESTAMP_NTZ,
    SNAPSHOT_DATE               TIMESTAMP_NTZ,
    DAYS_OPEN                   INTEGER,
    SNAPSHOT_TIER               VARCHAR(10),
    RAW_JSON_SNAPSHOT           VARIANT,
    PRIMARY KEY (ISSUE_NUMBER, REPO)
)
"""


MERGE_SQL = """
MERGE INTO {db}.{schema}.{table} AS target
USING (
    SELECT
        %s::INTEGER           AS issue_number,
        %s::VARCHAR           AS repo,
        %s::VARCHAR           AS title,
        %s::VARCHAR           AS state,
        PARSE_JSON(%s)::ARRAY AS labels,
        %s::INTEGER           AS assignee_count,
        %s::VARCHAR           AS milestone,
        %s::INTEGER           AS comment_count_at_snapshot,
        %s::INTEGER           AS linked_pr_count_at_snapshot,
        %s::TIMESTAMP_NTZ     AS created_at,
        %s::TIMESTAMP_NTZ     AS snapshot_date,
        %s::INTEGER           AS days_open,
        %s::VARCHAR           AS snapshot_tier,
        PARSE_JSON(%s)        AS raw_json_snapshot
) AS src
ON target.issue_number = src.issue_number
AND target.repo        = src.repo
WHEN MATCHED THEN UPDATE SET
    title                       = src.title,
    state                       = src.state,
    labels                      = src.labels,
    assignee_count              = src.assignee_count,
    milestone                   = src.milestone,
    comment_count_at_snapshot   = src.comment_count_at_snapshot,
    linked_pr_count_at_snapshot = src.linked_pr_count_at_snapshot,
    snapshot_date               = src.snapshot_date,
    days_open                   = src.days_open,
    snapshot_tier               = src.snapshot_tier,
    raw_json_snapshot           = src.raw_json_snapshot
WHEN NOT MATCHED THEN INSERT (
    issue_number, repo, title, state, labels, assignee_count, milestone,
    comment_count_at_snapshot, linked_pr_count_at_snapshot, created_at,
    snapshot_date, days_open, snapshot_tier, raw_json_snapshot
) VALUES (
    src.issue_number, src.repo, src.title, src.state, src.labels,
    src.assignee_count, src.milestone, src.comment_count_at_snapshot,
    src.linked_pr_count_at_snapshot, src.created_at, src.snapshot_date,
    src.days_open, src.snapshot_tier, src.raw_json_snapshot
)
"""


def _write_batch(cursor, table_fqn, batch):
    if not batch:
        return
    db, schema, table = _parse_fqn(table_fqn)
    sql = MERGE_SQL.format(db=db, schema=schema, table=table)
    for rec in batch:
        cursor.execute(
            sql,
            (
                rec["issue_number"],
                rec["repo"],
                rec["title"],
                rec["state"],
                rec["labels"],
                rec["assignee_count"],
                rec["milestone"],
                rec["comment_count_at_snapshot"],
                rec["linked_pr_count_at_snapshot"],
                rec["created_at"],
                rec["snapshot_date"],
                rec["days_open"],
                rec["snapshot_tier"],
                rec["raw_json_snapshot"],
            ),
        )


def _count_rows(cursor, table_fqn):
    db, schema, table = _parse_fqn(table_fqn)
    cursor.execute(f"SELECT COUNT(*) FROM {db}.{schema}.{table}")
    return cursor.fetchone()[0]


def snapshot_issues(**context):
    from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook

    params = context["params"]
    source_table = params["source_table"]
    destination_table_t7 = params["destination_table_t7"]
    destination_table_t14 = params["destination_table_t14"]

    started_epoch = time.time()
    started_utc = datetime.utcnow()
    snapshots = [(7, "T+7", destination_table_t7), (14, "T+14", destination_table_t14)]

    src_db, src_schema, src_tbl = _parse_fqn(source_table)

    logging.info("=" * 72)
    logging.info("SNAPSHOT ISSUES — START")
    logging.info("Start time (UTC): %sZ", started_utc.isoformat())
    logging.info("Connection: snowflake_default")
    logging.info("Source table: %s", source_table)
    logging.info("Snapshots: T+7 and T+14 where T is issue created_at")
    logging.info("Destination T+7: %s", destination_table_t7)
    logging.info("Destination T+14: %s", destination_table_t14)
    logging.info("=" * 72)

    hook = SnowflakeHook(snowflake_conn_id="snowflake_default")
    conn = hook.get_conn()
    cursor = conn.cursor()

    try:
        for _, tier, dst in snapshots:
            db, schema, table = _parse_fqn(dst)
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {db}.{schema}")
            cursor.execute(TABLE_DDL.format(db=db, schema=schema, table=table))
            logging.info("[%s] Ensured destination table: %s", tier, dst)

        before_counts = {}
        for _, tier, dst in snapshots:
            before_counts[tier] = _count_rows(cursor, dst)
            logging.info("[%s] Row count before run: %s", tier, before_counts[tier])

        cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM {src_db}.{src_schema}.{src_tbl}
            WHERE STATE = 'closed'
              AND CLOSED_AT IS NOT NULL
              AND CREATED_AT IS NOT NULL
            """
        )
        total_issues = cursor.fetchone()[0]
        logging.info("Eligible source issues: %s", total_issues)

        offset = 0
        total_written = {"T+7": 0, "T+14": 0}
        skipped = 0

        while offset < total_issues:
            cursor.execute(
                f"""
                SELECT ISSUE_NUMBER, REPO, RAW_JSON
                FROM {src_db}.{src_schema}.{src_tbl}
                WHERE STATE = 'closed'
                  AND CLOSED_AT IS NOT NULL
                  AND CREATED_AT IS NOT NULL
                ORDER BY ISSUE_NUMBER
                LIMIT {BATCH_SIZE} OFFSET {offset}
                """
            )
            rows = cursor.fetchall()
            if not rows:
                break

            logging.info(
                "Processing source rows %s-%s of %s",
                offset + 1,
                offset + len(rows),
                total_issues,
            )

            tier_batches = {dst: [] for _, _, dst in snapshots}
            for issue_number, _repo, raw_json_value in rows:
                try:
                    raw = (
                        json.loads(raw_json_value)
                        if isinstance(raw_json_value, str)
                        else raw_json_value
                    )
                except Exception as exc:
                    logging.warning("JSON parse error on issue #%s: %s", issue_number, exc)
                    skipped += 1
                    continue

                for days, tier, dst in snapshots:
                    snap = _snapshot_issue(raw, days, tier)
                    if snap is None:
                        skipped += 1
                        continue
                    tier_batches[dst].append(snap)

                    if len(tier_batches[dst]) >= WRITE_BATCH_SIZE:
                        _write_batch(cursor, dst, tier_batches[dst])
                        conn.commit()
                        total_written[tier] += len(tier_batches[dst])
                        logging.info(
                            "[%s] Flushed %s records (running total=%s)",
                            tier,
                            len(tier_batches[dst]),
                            total_written[tier],
                        )
                        tier_batches[dst] = []

            for _, tier, dst in snapshots:
                if tier_batches[dst]:
                    _write_batch(cursor, dst, tier_batches[dst])
                    conn.commit()
                    total_written[tier] += len(tier_batches[dst])

            offset += len(rows)
            time.sleep(0.1)

        logging.info("Snapshot writes complete. Skipped issues: %s", skipped)

        for _, tier, dst in snapshots:
            after_count = _count_rows(cursor, dst)
            logging.info(
                "[%s] Row count after run: %s (delta=%s, written_this_run=%s)",
                tier,
                after_count,
                after_count - before_counts[tier],
                total_written[tier],
            )

            db, schema, table = _parse_fqn(dst)
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    AVG(COMMENT_COUNT_AT_SNAPSHOT) AS avg_comments,
                    AVG(LINKED_PR_COUNT_AT_SNAPSHOT) AS avg_linked_prs,
                    SUM(CASE WHEN LINKED_PR_COUNT_AT_SNAPSHOT > 0 THEN 1 ELSE 0 END) AS with_prs,
                    SUM(CASE WHEN LINKED_PR_COUNT_AT_SNAPSHOT = 0 THEN 1 ELSE 0 END) AS without_prs
                FROM {db}.{schema}.{table}
                """
            )
            r = cursor.fetchone()
            logging.info(
                "[%s] sanity: total=%s avg_comments=%.1f avg_linked_prs=%.2f with_prs=%s without_prs=%s",
                tier,
                r[0],
                round(r[1] or 0, 1),
                round(r[2] or 0, 2),
                r[3],
                r[4],
            )

        finished_utc = datetime.utcnow()
        duration_seconds = time.time() - started_epoch
        logging.info("End time (UTC): %sZ", finished_utc.isoformat())
        logging.info("Duration: %.2f seconds", duration_seconds)
        logging.info("SNAPSHOT ISSUES — COMPLETE")
        logging.info("=" * 72)

    finally:
        cursor.close()
        conn.close()


with DAG(
    dag_id="snapshot_issues",
    default_args=default_args,
    description=(
        "Create T+7 and T+14 issue snapshots from cleaned issues table. "
        "T is issue creation timestamp."
    ),
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["snapshot", "issues", "snowflake", "prelab"],
    params={
        "source_table": Param(
            "AIRFLOW_ML.CLEANED.GITHUB_ISSUES",
            type="string",
            description=(
                "Source table in DATABASE.SCHEMA.TABLE format. "
                "Snapshots are created for days_open=7 and days_open=14."
            ),
        ),
        "destination_table_t7": Param(
            "AIRFLOW_ML.PRELAB.SCORER_ISSUES_T7",
            type="string",
            description=(
                "Destination table for T+7 snapshot "
                "(T = issue creation timestamp)."
            ),
        ),
        "destination_table_t14": Param(
            "AIRFLOW_ML.PRELAB.SCORER_ISSUES_T14",
            type="string",
            description=(
                "Destination table for T+14 snapshot "
                "(T = issue creation timestamp)."
            ),
        ),
    },
) as dag:
    snapshot_issues_task = PythonOperator(
        task_id="snapshot_issues_task",
        python_callable=snapshot_issues,
        provide_context=True,
    )

