"""
Clean Bot Issues DAG
====================
Builds a cleaned issues table in Snowflake from a source issues table by removing
rows that look like bot/dependency chores and keeping only valid closed issues.

Default source:
  AIRFLOW_ML.RAW.GITHUB_ISSUES

Default destination:
  AIRFLOW_ML.CLEANED.GITHUB_ISSUES

Omitted title patterns (case-insensitive):
  - chore(%
  - bump %
  - %dependabot%
"""

from datetime import datetime, timedelta
import logging
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


def _parse_fqn(table_fqn):
    """
    Parse a table identifier in DATABASE.SCHEMA.TABLE format.
    """
    parts = [p.strip() for p in table_fqn.split(".")]
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"Table must be fully qualified as DATABASE.SCHEMA.TABLE, got: {table_fqn}"
        )
    return parts[0], parts[1], parts[2]


def _build_omit_title_clause(omit_title_patterns):
    """
    Build SQL predicates like:
      AND LOWER(TITLE) NOT LIKE 'pattern'
    """
    if not omit_title_patterns:
        return "", []

    cleaned_patterns = []
    clauses = []
    for raw in omit_title_patterns:
        p = str(raw).strip().lower()
        if not p:
            continue
        # Minimal escaping for single quotes inside SQL string literals.
        p_escaped = p.replace("'", "''")
        cleaned_patterns.append(p)
        clauses.append(f"AND LOWER(TITLE) NOT LIKE '{p_escaped}'")

    return "\n              ".join(clauses), cleaned_patterns


def clean_bot_issues(**context):
    from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook

    started_epoch = time.time()
    started_utc = datetime.utcnow()
    params = context["params"]

    source_table = params["source_table"]
    destination_table = params["destination_table"]
    omit_title_patterns = params["omit_title_patterns"]

    src_db, src_schema, src_tbl = _parse_fqn(source_table)
    dst_db, dst_schema, dst_tbl = _parse_fqn(destination_table)
    omit_clause, cleaned_patterns = _build_omit_title_clause(omit_title_patterns)

    logging.info("=" * 70)
    logging.info("CLEAN BOT ISSUES — START")
    logging.info("Start time (UTC): %sZ", started_utc.isoformat())
    logging.info("Using Airflow Snowflake connection: snowflake_default")
    logging.info("Source: %s.%s.%s", src_db, src_schema, src_tbl)
    logging.info("Destination: %s.%s.%s", dst_db, dst_schema, dst_tbl)
    logging.info("Omitting title patterns (case-insensitive): %s", cleaned_patterns)
    logging.info("=" * 70)

    hook = SnowflakeHook(snowflake_conn_id="snowflake_default")
    conn = hook.get_conn()
    cursor = conn.cursor()

    try:
        cursor.execute(f"USE DATABASE {src_db}")
        cursor.execute(f"USE SCHEMA {src_schema}")

        source_count_sql = f"SELECT COUNT(*) FROM {src_db}.{src_schema}.{src_tbl}"
        cursor.execute(source_count_sql)
        source_count = cursor.fetchone()[0]
        logging.info("Source row count before cleaning: %s", source_count)

        create_schema_sql = f"CREATE SCHEMA IF NOT EXISTS {dst_db}.{dst_schema}"
        cursor.execute(create_schema_sql)
        logging.info("Ensured destination schema exists: %s.%s", dst_db, dst_schema)

        create_clean_table_sql = f"""
            CREATE OR REPLACE TABLE {dst_db}.{dst_schema}.{dst_tbl} AS
            SELECT *
            FROM {src_db}.{src_schema}.{src_tbl}
            WHERE STATE = 'closed'
              AND CLOSED_AT IS NOT NULL
              AND CREATED_AT IS NOT NULL
              AND DATEDIFF('day', CREATED_AT, CLOSED_AT) >= 0
              {omit_clause}
        """
        logging.info("Executing clean query with dynamic title omit filters.")
        cursor.execute(create_clean_table_sql)
        conn.commit()
        logging.info("Created/updated cleaned table: %s.%s.%s", dst_db, dst_schema, dst_tbl)

        dest_count_sql = f"SELECT COUNT(*) FROM {dst_db}.{dst_schema}.{dst_tbl}"
        cursor.execute(dest_count_sql)
        destination_count = cursor.fetchone()[0]
        removed_count = source_count - destination_count
        removed_pct = (removed_count / source_count * 100.0) if source_count else 0.0

        finished_utc = datetime.utcnow()
        duration_seconds = time.time() - started_epoch

        logging.info("Destination row count after cleaning: %s", destination_count)
        logging.info(
            "Rows removed by filters: %s (%.2f%% of source)", removed_count, removed_pct
        )
        logging.info("End time (UTC): %sZ", finished_utc.isoformat())
        logging.info("Duration: %.2f seconds", duration_seconds)
        logging.info("CLEAN BOT ISSUES — COMPLETE")
        logging.info("=" * 70)

    finally:
        cursor.close()
        conn.close()


with DAG(
    dag_id="clean_bot_issues",
    default_args=default_args,
    description=(
        "Clean bot-like GitHub issue rows into a destination table using Snowflake "
        "connection 'snowflake_default'."
    ),
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["clean", "github", "snowflake", "raw"],
    params={
        "source_table": Param(
            "AIRFLOW_ML.RAW.GITHUB_ISSUES",
            type="string",
            description="Source issues table in DATABASE.SCHEMA.TABLE format.",
        ),
        "destination_table": Param(
            "AIRFLOW_ML.CLEANED.GITHUB_ISSUES",
            type="string",
            description="Destination cleaned table in DATABASE.SCHEMA.TABLE format.",
        ),
        "omit_title_patterns": Param(
            ["chore(%", "bump %", "%dependabot%"],
            type="array",
            description=(
                "Case-insensitive LIKE patterns to omit from LOWER(TITLE). "
                "Each item becomes: AND LOWER(TITLE) NOT LIKE '<pattern>'."
            ),
        ),
    },
) as dag:
    clean_bot_issues_task = PythonOperator(
        task_id="clean_bot_issues_task",
        python_callable=clean_bot_issues,
        provide_context=True,
    )

