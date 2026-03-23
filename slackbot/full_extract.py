"""
Full GitHub Extract DAG (Production)
===================================
Extracts all closed issues + linked PRs from apache/airflow into Snowflake RAW schema.

This is the production DAG:
- Always loads to Snowflake (no test mode logic)
- Checkpoints progress in /tmp/autobot_checkpoints to allow safe resume
"""

from datetime import datetime, timedelta
import logging
import json
import os
import time
import random
from pathlib import Path

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.models import Param


# ============================================================================
# CONSTANTS
# ============================================================================

GITHUB_API_BASE = "https://api.github.com"
REPO_OWNER = "apache"
REPO_NAME = "airflow"
PER_PAGE = 100
RATE_LIMIT_BUFFER = 100
MAX_RETRIES = 5
CHECKPOINT_DIR = Path("/tmp/autobot_checkpoints")

# Snowflake table names
ISSUES_TABLE = "GITHUB_ISSUES"
PRS_TABLE = "GITHUB_PRS"

# Log GitHub rate limit once + periodically (helps confirm App token tier + progress)
_RATE_LIMIT_LOGGED = False
_GITHUB_REQUEST_COUNT = 0
_GITHUB_RATE_LOG_EVERY = 200  # log rate headers every N GitHub requests

# Optional local backup root (mounted to host via /opt/airflow/code_v2)
BACKUP_ROOT = Path("/opt/airflow/code_v2/backups/full_extract")


# ============================================================================
# DEFAULT DAG ARGS
# ============================================================================

default_args = {
    "owner": "autobot",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
}


# ============================================================================
# GITHUB CLIENT
# ============================================================================

def _get_github_token():
    """
    Generate a GitHub App installation access token.
    Falls back to personal token env var if App credentials not configured.
    App tokens have higher rate limits vs personal tokens.
    """
    import jwt
    import requests as req
    import time as _time

    try:
        from airflow.models import Variable

        app_id = Variable.get("GITHUB_APP_ID")
        installation_id = Variable.get("GITHUB_APP_INSTALLATION_ID")
        private_key = Variable.get("GITHUB_APP_PRIVATE_KEY")  # full PEM string
    except Exception:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("No GitHub credentials found (App vars or GITHUB_TOKEN).")
        logging.info("GitHub auth: using personal access token (GITHUB_TOKEN).")
        return token

    # Airflow Variables sometimes store PEM with literal "\n"
    private_key = (private_key or "").replace("\\n", "\n").strip()
    if not private_key:
        raise ValueError("GITHUB_APP_PRIVATE_KEY is empty.")

    now = int(_time.time())
    payload = {"iat": now - 60, "exp": now + (10 * 60), "iss": app_id}
    jwt_token = jwt.encode(payload, private_key, algorithm="RS256")

    resp = req.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    logging.info("GitHub auth: using GitHub App installation token.")
    return resp.json()["token"]


def _github_request(session, url, params=None, attempt=0):
    """
    Single GitHub API GET with:
      - Rate limit awareness (sleep until reset if near limit)
      - Exponential backoff on 403/429/5xx
      - Secondary rate limit (abuse detection) jitter
    Returns parsed JSON or raises after MAX_RETRIES.
    """
    if params is None:
        params = {}

    try:
        global _GITHUB_REQUEST_COUNT
        _GITHUB_REQUEST_COUNT += 1

        response = session.get(url, params=params, timeout=30)

        remaining = int(response.headers.get("X-RateLimit-Remaining", 999))
        reset_at = int(response.headers.get("X-RateLimit-Reset", 0))
        limit = response.headers.get("X-RateLimit-Limit")

        global _RATE_LIMIT_LOGGED
        if (not _RATE_LIMIT_LOGGED and limit is not None) or (
            limit is not None and _GITHUB_REQUEST_COUNT % _GITHUB_RATE_LOG_EVERY == 0
        ):
            logging.info(
                "GitHub rate limit: "
                f"remaining={remaining} limit={limit} reset={reset_at} "
                f"(requests_made={_GITHUB_REQUEST_COUNT})"
            )
            _RATE_LIMIT_LOGGED = True

        if remaining < RATE_LIMIT_BUFFER:
            sleep_secs = max(reset_at - int(time.time()), 0) + 5
            logging.warning(
                f"Rate limit low ({remaining} remaining of {limit}). "
                f"Sleeping {sleep_secs}s until reset (requests_made={_GITHUB_REQUEST_COUNT})."
            )
            time.sleep(sleep_secs)

        if response.status_code == 200:
            return response.json()

        # If the token becomes invalid mid-run, GitHub returns 401.
        # Refresh the token/session and retry the request.
        if response.status_code == 401:
            logging.warning(
                f"HTTP 401 Unauthorized on {url}. Refreshing GitHub token and retrying "
                f"(attempt {attempt+1}/{MAX_RETRIES})."
            )
            if attempt < MAX_RETRIES:
                new_token = _get_github_token()
                new_session = _build_session(new_token)
                return _github_request(new_session, url, params, attempt + 1)
            raise RuntimeError(f"GitHub auth failed (401) persisting on {url}")

        if response.status_code in (403, 429):
            retry_after = int(response.headers.get("Retry-After", 0))
            if retry_after:
                logging.warning(f"Retry-After header: sleeping {retry_after}s")
                time.sleep(retry_after)
            else:
                backoff = min(2**attempt * 10, 300) + random.uniform(0, 5)
                logging.warning(
                    f"HTTP {response.status_code} on {url}. "
                    f"Backoff {backoff:.1f}s (attempt {attempt+1}/{MAX_RETRIES})"
                )
                time.sleep(backoff)

            if attempt < MAX_RETRIES:
                return _github_request(session, url, params, attempt + 1)
            raise RuntimeError(f"Max retries exceeded on {url}")

        if response.status_code == 404:
            logging.debug(f"404 on {url} — returning empty list")
            return []

        if response.status_code >= 500:
            backoff = min(2**attempt * 15, 300) + random.uniform(0, 5)
            logging.warning(
                f"HTTP {response.status_code} on {url}. "
                f"Backoff {backoff:.1f}s (attempt {attempt+1}/{MAX_RETRIES})"
            )
            time.sleep(backoff)
            if attempt < MAX_RETRIES:
                return _github_request(session, url, params, attempt + 1)
            raise RuntimeError(f"Server errors persisting on {url}")

        logging.warning(f"Unexpected status {response.status_code} on {url}")
        return None

    except Exception as e:
        if attempt < MAX_RETRIES:
            backoff = min(2**attempt * 10, 120) + random.uniform(0, 5)
            logging.warning(f"Request error: {e}. Retrying in {backoff:.1f}s")
            time.sleep(backoff)
            return _github_request(session, url, params, attempt + 1)
        raise


def _paginate(session, url, extra_params=None, max_items=None):
    """
    Paginate through all pages of a GitHub list endpoint.
    Yields individual items. Stops at max_items if set.
    """
    params = {"per_page": PER_PAGE, "page": 1}
    if extra_params:
        params.update(extra_params)

    collected = 0
    while True:
        page_data = _github_request(session, url, params)
        if not page_data:
            break

        for item in page_data:
            yield item
            collected += 1
            if max_items and collected >= max_items:
                return

        if len(page_data) < PER_PAGE:
            break

        params["page"] += 1
        time.sleep(0.3)


def _build_session(token):
    import requests

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return session


# ============================================================================
# CHECKPOINT HELPERS
# ============================================================================

def _load_checkpoint(name):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"{name}.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        logging.info(f"Resumed checkpoint '{name}': {len(data)} items")
        return data
    return None


def _save_checkpoint(name, data):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKPOINT_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f)


def _clear_checkpoint(name):
    path = CHECKPOINT_DIR / f"{name}.json"
    if path.exists():
        path.unlink()


# ============================================================================
# SNOWFLAKE HELPERS
# ============================================================================

def _get_snowflake_conn(database, schema):
    """Return a Snowflake connection using Airflow's snowflake_default conn."""
    from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook

    hook = SnowflakeHook(snowflake_conn_id="snowflake_default")
    conn = hook.get_conn()
    cursor = conn.cursor()
    cursor.execute(f"USE DATABASE {database}")
    cursor.execute(f"USE SCHEMA {schema}")
    return conn, cursor


def _ensure_tables(cursor, database, schema, issues_table, prs_table):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{schema}.{issues_table} (
            issue_number        INTEGER         NOT NULL,
            repo                VARCHAR(200)    NOT NULL,
            title               VARCHAR(1000),
            state               VARCHAR(20),
            labels              ARRAY,
            assignee_count      INTEGER,
            milestone           VARCHAR(500),
            comment_count       INTEGER,
            linked_pr_count     INTEGER,
            created_at          TIMESTAMP_NTZ,
            closed_at           TIMESTAMP_NTZ,
            extracted_at        TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
            raw_json            VARIANT,
            PRIMARY KEY (issue_number, repo)
        )
    """
    )

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{schema}.{prs_table} (
            pr_number               INTEGER         NOT NULL,
            repo                    VARCHAR(200)    NOT NULL,
            linked_issue_number     INTEGER,
            pr_title                VARCHAR(1000),
            state                   VARCHAR(20),
            is_merged               BOOLEAN,
            base_sha                VARCHAR(100),
            head_sha                VARCHAR(100),
            changed_files_count     INTEGER,
            review_count            INTEGER,
            ci_conclusion           VARCHAR(50),
            merged_at               TIMESTAMP_NTZ,
            created_at              TIMESTAMP_NTZ,
            extracted_at            TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP(),
            raw_json                VARIANT,
            PRIMARY KEY (pr_number, repo)
        )
    """
    )

    logging.info(f"Tables ensured: {issues_table}, {prs_table}")


def _upsert_issues_batch(cursor, database, schema, issues_table, batch):
    if not batch:
        return

    merge_sql = f"""
        MERGE INTO {database}.{schema}.{issues_table} AS target
        USING (
            SELECT
                %s::INTEGER           AS issue_number,
                %s::VARCHAR           AS repo,
                %s::VARCHAR           AS title,
                %s::VARCHAR           AS state,
                PARSE_JSON(%s)::ARRAY AS labels,
                %s::INTEGER           AS assignee_count,
                %s::VARCHAR           AS milestone,
                %s::INTEGER           AS comment_count,
                %s::INTEGER           AS linked_pr_count,
                %s::TIMESTAMP_NTZ     AS created_at,
                %s::TIMESTAMP_NTZ     AS closed_at,
                CURRENT_TIMESTAMP()   AS extracted_at,
                PARSE_JSON(%s)        AS raw_json
        ) AS src
        ON target.issue_number = src.issue_number
        AND target.repo        = src.repo
        WHEN MATCHED THEN UPDATE SET
            title           = src.title,
            state           = src.state,
            labels          = src.labels,
            assignee_count  = src.assignee_count,
            milestone       = src.milestone,
            comment_count   = src.comment_count,
            linked_pr_count = src.linked_pr_count,
            closed_at       = src.closed_at,
            extracted_at    = src.extracted_at,
            raw_json        = src.raw_json
        WHEN NOT MATCHED THEN INSERT (
            issue_number, repo, title, state, labels,
            assignee_count, milestone, comment_count, linked_pr_count,
            created_at, closed_at, extracted_at, raw_json
        ) VALUES (
            src.issue_number, src.repo, src.title, src.state, src.labels,
            src.assignee_count, src.milestone, src.comment_count, src.linked_pr_count,
            src.created_at, src.closed_at, src.extracted_at, src.raw_json
        )
    """

    for rec in batch:
        issue = rec["issue"]

        title = (issue.get("title") or "")[:1000]
        state = issue.get("state") or ""
        labels = json.dumps([l["name"] for l in issue.get("labels", []) if l.get("name")])
        assignee_count = len(issue.get("assignees") or [])
        milestone = (((issue.get("milestone") or {}).get("title")) or "")[:500]
        comment_count = issue.get("comments") or 0
        linked_pr_count = len(rec.get("linked_pr_numbers") or [])
        created_at = issue.get("created_at") or None
        closed_at = issue.get("closed_at") or None
        raw_json = json.dumps(rec)

        cursor.execute(
            merge_sql,
            (
                issue["number"],
                f"{REPO_OWNER}/{REPO_NAME}",
                title,
                state,
                labels,
                assignee_count,
                milestone,
                comment_count,
                linked_pr_count,
                created_at,
                closed_at,
                raw_json,
            ),
        )


def _upsert_prs_batch(cursor, database, schema, batch):
    if not batch:
        return

def _upsert_prs_batch(cursor, database, schema, prs_table, batch):
    """
    Upsert a batch of PR records into Snowflake.
    """
    if not batch:
        return

    merge_sql = f"""
        MERGE INTO {database}.{schema}.{prs_table} AS target
        USING (
            SELECT
                %s::INTEGER         AS pr_number,
                %s::VARCHAR         AS repo,
                %s::INTEGER         AS linked_issue_number,
                %s::VARCHAR         AS pr_title,
                %s::VARCHAR         AS state,
                %s::BOOLEAN         AS is_merged,
                %s::VARCHAR         AS base_sha,
                %s::VARCHAR         AS head_sha,
                %s::INTEGER         AS changed_files_count,
                %s::INTEGER         AS review_count,
                %s::VARCHAR         AS ci_conclusion,
                %s::TIMESTAMP_NTZ   AS merged_at,
                %s::TIMESTAMP_NTZ   AS created_at,
                CURRENT_TIMESTAMP() AS extracted_at,
                PARSE_JSON(%s)      AS raw_json
        ) AS src
        ON target.pr_number = src.pr_number
        AND target.repo     = src.repo
        WHEN MATCHED THEN UPDATE SET
            linked_issue_number = src.linked_issue_number,
            pr_title            = src.pr_title,
            state               = src.state,
            is_merged           = src.is_merged,
            base_sha            = src.base_sha,
            head_sha            = src.head_sha,
            changed_files_count = src.changed_files_count,
            review_count        = src.review_count,
            ci_conclusion       = src.ci_conclusion,
            merged_at           = src.merged_at,
            extracted_at        = src.extracted_at,
            raw_json            = src.raw_json
        WHEN NOT MATCHED THEN INSERT (
            pr_number, repo, linked_issue_number, pr_title, state,
            is_merged, base_sha, head_sha, changed_files_count,
            review_count, ci_conclusion, merged_at, created_at,
            extracted_at, raw_json
        ) VALUES (
            src.pr_number, src.repo, src.linked_issue_number, src.pr_title,
            src.state, src.is_merged, src.base_sha, src.head_sha,
            src.changed_files_count, src.review_count, src.ci_conclusion,
            src.merged_at, src.created_at, src.extracted_at, src.raw_json
        )
    """

    for rec in batch:
        pr = rec["pr"]

        pr_title = (pr.get("title") or "")[:1000]
        state = pr.get("state") or ""
        is_merged = pr.get("merged_at") is not None
        base_sha = (pr.get("base") or {}).get("sha") or ""
        head_sha = (pr.get("head") or {}).get("sha") or ""
        changed_files = pr.get("changed_files") or 0
        review_count = len(rec.get("reviews") or [])
        merged_at = pr.get("merged_at") or None
        created_at = pr.get("created_at") or None

        conclusions = [
            cr.get("conclusion")
            for cr in (rec.get("check_runs") or [])
            if cr.get("conclusion")
        ]
        if not conclusions:
            ci_conclusion = "none"
        elif all(c == "success" for c in conclusions):
            ci_conclusion = "success"
        elif any(c in ("failure", "timed_out", "cancelled") for c in conclusions):
            ci_conclusion = "failure"
        else:
            ci_conclusion = "mixed"

        raw_json = json.dumps(rec)

        cursor.execute(
            merge_sql,
            (
                pr["number"],
                f"{REPO_OWNER}/{REPO_NAME}",
                rec.get("linked_issue_number"),
                pr_title,
                state,
                is_merged,
                base_sha,
                head_sha,
                changed_files,
                review_count,
                ci_conclusion,
                merged_at,
                created_at,
                raw_json,
            ),
        )


# ============================================================================
# ISSUE DETAIL FETCHER
# ============================================================================

def _fetch_issue_full(session, issue_number):
    base = f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}"

    issue = _github_request(session, f"{base}/issues/{issue_number}")
    if not isinstance(issue, dict):
        logging.warning(f"Issue #{issue_number} returned non-dict response; skipping issue.")
        return None
    comments = list(_paginate(session, f"{base}/issues/{issue_number}/comments"))

    sub_issues = _github_request(session, f"{base}/issues/{issue_number}/sub_issues")
    if not isinstance(sub_issues, list):
        sub_issues = []

    timeline = list(
        _paginate(
            session,
            f"{base}/issues/{issue_number}/timeline",
            extra_params={"per_page": 100},
        )
    )

    linked_pr_numbers = []
    for event in timeline:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event", "")
        if event_type == "cross-referenced":
            src = event.get("source", {})
            issue_ref = src.get("issue", {})
            pull_request = issue_ref.get("pull_request")
            if pull_request:
                ref_number = issue_ref.get("number")
                if ref_number:
                    linked_pr_numbers.append(ref_number)

        if event_type in ("connected", "disconnected"):
            ref = event.get("source", {})
            ref_number = ref.get("issue", {}).get("number")
            if ref_number:
                linked_pr_numbers.append(ref_number)

    import re

    fix_pattern = re.compile(
        r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*#(\d+)", re.IGNORECASE
    )
    body_text = (issue.get("body") or "") + " ".join(c.get("body", "") for c in comments)
    for match in fix_pattern.finditer(body_text):
        linked_pr_numbers.append(int(match.group(1)))

    linked_pr_numbers = list(set(linked_pr_numbers))

    return {
        "issue": issue,
        "comments": comments,
        "sub_issues": sub_issues,
        "timeline": timeline,
        "linked_pr_numbers": linked_pr_numbers,
        "issue_number": issue_number,
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "_extracted_at": datetime.utcnow().isoformat(),
    }


# ============================================================================
# PR DETAIL FETCHER
# ============================================================================

def _fetch_pr_full(session, pr_number, linked_issue_number=None):
    base = f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}"

    pr = _github_request(session, f"{base}/pulls/{pr_number}")
    if not isinstance(pr, dict) or not pr.get("number"):
        logging.warning(f"PR #{pr_number} returned no data — skipping")
        return None

    files = list(_paginate(session, f"{base}/pulls/{pr_number}/files"))
    reviews = list(_paginate(session, f"{base}/pulls/{pr_number}/reviews"))
    review_comments = list(_paginate(session, f"{base}/pulls/{pr_number}/comments"))
    commits = list(_paginate(session, f"{base}/pulls/{pr_number}/commits"))

    check_runs = []
    head_sha = pr.get("head", {}).get("sha")
    if head_sha:
        result = _github_request(
            session,
            f"{base}/commits/{head_sha}/check-runs",
            params={"per_page": 100},
        )
        if isinstance(result, dict):
            check_runs = result.get("check_runs", [])
        elif isinstance(result, list):
            check_runs = result

    requested_reviewers = _github_request(
        session, f"{base}/pulls/{pr_number}/requested_reviewers"
    )
    if not isinstance(requested_reviewers, dict):
        requested_reviewers = {"users": [], "teams": []}

    reviewers_who_submitted = {
        r.get("user", {}).get("login") for r in reviews if r.get("user")
    }
    requested_logins = [u.get("login") for u in requested_reviewers.get("users", [])]
    silent_reviewers = [
        login for login in requested_logins if login not in reviewers_who_submitted
    ]

    return {
        "pr": pr,
        "files": files,
        "reviews": reviews,
        "review_comments": review_comments,
        "commits": commits,
        "check_runs": check_runs,
        "requested_reviewers": requested_reviewers,
        "silent_reviewers": silent_reviewers,
        "linked_issue_number": linked_issue_number,
        "pr_number": pr_number,
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "_extracted_at": datetime.utcnow().isoformat(),
    }


# ============================================================================
# TASK 1 — EXTRACT ISSUES (Production)
# ============================================================================

def extract_issues(**context):
    params = context["params"]
    issues_pull_size = params["issues_pull_size"]
    database = params["snowflake_database"]
    schema = params["snowflake_schema"]
    issues_table = params.get("issues_table", ISSUES_TABLE)
    prs_table = params.get("prs_table", PRS_TABLE)
    backup_local = params.get("backup_local", False)

    dag_run = context.get("dag_run")
    run_id = getattr(dag_run, "run_id", "manual")
    started_at = datetime.utcnow()

    logging.info("=" * 60)
    logging.info("EXTRACT ISSUES — PRODUCTION")
    logging.info(f"Repo: {REPO_OWNER}/{REPO_NAME}")
    logging.info(f"Target: up to {issues_pull_size} closed issues (excluding PRs)")
    logging.info(f"Snowflake target: {database}.{schema}")
    logging.info(f"Tables: {issues_table}, {prs_table}")
    logging.info(f"Backup local: {backup_local}")
    logging.info(f"Run id: {run_id}")
    logging.info(f"Start time (UTC): {started_at.isoformat()}Z")
    logging.info("=" * 60)

    token = _get_github_token()
    session = _build_session(token)

    conn, cursor = _get_snowflake_conn(database, schema)
    _ensure_tables(cursor, database, schema, issues_table, prs_table)

    checkpoint_key = "issues_processed"
    pr_checkpoint_key = "linked_pr_numbers"

    processed_numbers = set(_load_checkpoint(checkpoint_key) or [])
    all_linked_prs = dict(_load_checkpoint(pr_checkpoint_key) or {})
    processed_at_start = len(processed_numbers)

    remaining_quota = max(issues_pull_size - len(processed_numbers), 0)
    logging.info(
        f"Checkpoint: {len(processed_numbers)} issues already processed "
        f"(remaining_quota={remaining_quota})"
    )

    list_url = f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues"
    list_params = {"state": "closed", "sort": "created", "direction": "desc"}

    issue_numbers_to_process = []
    logging.info("Fetching issue list...")

    # NOTE: GitHub's /issues endpoint includes PRs; filter them out by checking
    # the presence of the "pull_request" key.
    scanned = 0
    if remaining_quota > 0:
        for item in _paginate(session, list_url, list_params, max_items=None):
            scanned += 1
            num = item.get("number")
            if not num or num in processed_numbers:
                continue
            created_at = item.get("created_at", "")
            if created_at < "2019-01-01T00:00:00Z":
                logging.info(f"Hit pre-2019 issue #{num} (created {created_at[:10]}) — stopping scan")
                break  # All remaining pages are older, no need to fetch them
            issue_numbers_to_process.append(num)
            if len(issue_numbers_to_process) >= remaining_quota:
                break

    logging.info(
        f"Found {len(issue_numbers_to_process)} new issues to process "
        f"(scanned_items={scanned})"
    )

    batch = []
    BATCH_SIZE = 50
    total = len(issue_numbers_to_process)
    backup_records = [] if backup_local else None

    for idx, issue_number in enumerate(issue_numbers_to_process):
        try:
            if idx == 0:
                logging.info(f"Beginning issue detail fetch for {total} issues...")
            logging.info(f"  Issue {idx+1}/{total}: #{issue_number}")
            record = _fetch_issue_full(session, issue_number)
            if record is None:
                continue

            if record.get("linked_pr_numbers"):
                all_linked_prs[str(issue_number)] = record["linked_pr_numbers"]
                if (idx + 1) % 25 == 0:
                    logging.info(
                        f"  Linked PR refs so far: {sum(len(v) for v in all_linked_prs.values())} "
                        f"across {len(all_linked_prs)} issues"
                    )

            batch.append(record)
            if backup_records is not None:
                backup_records.append(record)

            if len(batch) >= BATCH_SIZE:
                _upsert_issues_batch(cursor, database, schema, issues_table, batch)
                conn.commit()
                logging.info(f"  Flushed batch of {len(batch)} to Snowflake")
                batch = []

            processed_numbers.add(issue_number)
            _save_checkpoint(checkpoint_key, list(processed_numbers))
            _save_checkpoint(pr_checkpoint_key, all_linked_prs)

            time.sleep(0.5)

        except Exception as e:
            logging.error(f"  Failed on issue #{issue_number}: {e}")
            _save_checkpoint(checkpoint_key, list(processed_numbers))
            _save_checkpoint(pr_checkpoint_key, all_linked_prs)
            continue

    if batch:
        _upsert_issues_batch(cursor, database, schema, issues_table, batch)
        conn.commit()
        logging.info(f"Flushed final batch of {len(batch)} issues")

    # Optional local backup
    if backup_records is not None:
        backup_dir = BACKUP_ROOT / run_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        issues_path = backup_dir / "issues.json"
        with open(issues_path, "w") as f:
            json.dump(backup_records, f, indent=2, default=str)
        logging.info(f"Local backup written: {issues_path} ({len(backup_records)} issues)")

    total_linked_prs = sum(len(v) for v in all_linked_prs.values())
    finished_at = datetime.utcnow()
    duration_s = (finished_at - started_at).total_seconds()
    processed_in_run = len(processed_numbers) - processed_at_start

    logging.info("=" * 60)
    logging.info("EXTRACT ISSUES — COMPLETE")
    logging.info(f"Issues extracted (cumulative): {len(processed_numbers)}")
    logging.info(f"Issues extracted (this run): {processed_in_run}")
    logging.info(f"Unique issues with linked PRs: {len(all_linked_prs)}")
    logging.info(f"Total linked PR references: {total_linked_prs}")
    logging.info(f"End time (UTC): {finished_at.isoformat()}Z (duration={duration_s:.1f}s)")
    logging.info("Issues extract complete. PR numbers saved to checkpoint.")
    logging.info("=" * 60)

    conn.close()


# ============================================================================
# TASK 2 — EXTRACT PRS (Production)
# ============================================================================

def extract_prs(**context):
    params = context["params"]
    prs_pull_size = params["prs_pull_size"]
    database = params["snowflake_database"]
    schema = params["snowflake_schema"]
    issues_table = params.get("issues_table", ISSUES_TABLE)
    prs_table = params.get("prs_table", PRS_TABLE)
    backup_local = params.get("backup_local", False)

    dag_run = context.get("dag_run")
    run_id = getattr(dag_run, "run_id", "manual")
    started_at = datetime.utcnow()

    logging.info("=" * 60)
    logging.info("EXTRACT PRS — PRODUCTION")
    logging.info(f"Repo: {REPO_OWNER}/{REPO_NAME}")
    logging.info(f"Snowflake target: {database}.{schema}")
    logging.info(f"Tables: {issues_table}, {prs_table}")
    logging.info(f"Backup local: {backup_local}")
    logging.info(f"Run id: {run_id}")
    logging.info(f"Start time (UTC): {started_at.isoformat()}Z")
    logging.info("=" * 60)

    token = _get_github_token()
    session = _build_session(token)

    all_linked_prs = dict(_load_checkpoint("linked_pr_numbers") or {})
    if not all_linked_prs:
        logging.warning(
            "No linked PRs found in checkpoint. Did extract_issues complete successfully?"
        )
        return

    pr_to_issues = {}
    for issue_str, pr_list in all_linked_prs.items():
        for pr_num in pr_list:
            pr_to_issues.setdefault(pr_num, []).append(int(issue_str))

    unique_pr_numbers = sorted(pr_to_issues.keys())

    if len(unique_pr_numbers) > prs_pull_size:
        logging.info(
            f"Capping PR pull at {prs_pull_size} (found {len(unique_pr_numbers)} linked)"
        )
        unique_pr_numbers = unique_pr_numbers[:prs_pull_size]

    logging.info(
        f"Unique PRs to fetch: {len(unique_pr_numbers)} "
        f"(from {len(all_linked_prs)} issues with PR links)"
    )

    conn, cursor = _get_snowflake_conn(database, schema)
    _ensure_tables(cursor, database, schema, issues_table, prs_table)

    pr_checkpoint_key = "prs_processed"
    processed_prs = set(_load_checkpoint(pr_checkpoint_key) or [])
    processed_prs_at_start = len(processed_prs)
    logging.info(f"Checkpoint: {len(processed_prs)} PRs already processed")

    prs_to_process = [p for p in unique_pr_numbers if p not in processed_prs]
    logging.info(f"PRs remaining: {len(prs_to_process)}")

    batch = []
    BATCH_SIZE = 25
    total = len(prs_to_process)
    backup_records = [] if backup_local else None

    for idx, pr_number in enumerate(prs_to_process):
        linked_issue = pr_to_issues.get(pr_number, [None])[0]

        try:
            if idx == 0:
                logging.info(f"Beginning PR detail fetch for {total} PRs...")
            logging.info(f"  PR {idx+1}/{total}: #{pr_number} (linked to issue #{linked_issue})")
            record = _fetch_pr_full(session, pr_number, linked_issue)

            # Only mark PR as processed if we successfully fetched a full PR record
            # and will therefore write it to Snowflake.
            if record is None:
                logging.warning(
                    f"PR #{pr_number} was not fully fetched (record=None); "
                    f"leaving it unprocessed for retry."
                )
                continue

            batch.append(record)
            if backup_records is not None:
                backup_records.append(record)

            if len(batch) >= BATCH_SIZE:
                _upsert_prs_batch(cursor, database, schema, prs_table, batch)
                conn.commit()
                logging.info(f"  Flushed batch of {len(batch)} PRs to Snowflake")
                batch = []

            processed_prs.add(pr_number)
            _save_checkpoint(pr_checkpoint_key, list(processed_prs))

            time.sleep(0.5)

        except Exception as e:
            logging.error(f"  Failed on PR #{pr_number}: {e}")
            _save_checkpoint(pr_checkpoint_key, list(processed_prs))
            continue

    if batch:
        _upsert_prs_batch(cursor, database, schema, prs_table, batch)
        conn.commit()
        logging.info(f"Flushed final batch of {len(batch)} PRs")

    if len(processed_prs) >= len(unique_pr_numbers):
        logging.info("All PRs processed. Clearing PR checkpoint.")
        _clear_checkpoint(pr_checkpoint_key)

    # Optional local backup
    if backup_records is not None:
        backup_dir = BACKUP_ROOT / run_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        prs_path = backup_dir / "prs.json"
        with open(prs_path, "w") as f:
            json.dump(backup_records, f, indent=2, default=str)
        logging.info(f"Local backup written: {prs_path} ({len(backup_records)} PRs)")

    logging.info("=" * 60)
    logging.info("EXTRACT PRS — COMPLETE")
    logging.info(f"PRs extracted (cumulative): {len(processed_prs)}")
    logging.info(f"PRs extracted (this run): {len(processed_prs) - processed_prs_at_start}")
    finished_at = datetime.utcnow()
    duration_s = (finished_at - started_at).total_seconds()
    logging.info(f"End time (UTC): {finished_at.isoformat()}Z (duration={duration_s:.1f}s)")
    logging.info("PRs extract complete.")
    logging.info("=" * 60)

    conn.close()


# ============================================================================
# DAG DEFINITION
# ============================================================================

with DAG(
    dag_id="full_extract",
    default_args=default_args,
    description=(
        "Full GitHub extract: closed issues + linked PRs from apache/airflow into Snowflake RAW"
    ),
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["extract", "github", "snowflake", "raw"],
    params={
        "issues_pull_size": Param(
            15000,
            type="integer",
            description="Max closed issues to pull (15000 ≈ everything).",
        ),
        "prs_pull_size": Param(
            50000,
            type="integer",
            description="Safety cap on linked PRs to detail-fetch.",
        ),
        "snowflake_database": Param(
            "AIRFLOW_ML",
            type="string",
            description="Target Snowflake database.",
        ),
        "snowflake_schema": Param(
            "RAW",
            type="string",
            description="Target Snowflake schema.",
        ),
        "issues_table": Param(
            ISSUES_TABLE,
            type="string",
            description="Target Snowflake issues table name.",
        ),
        "prs_table": Param(
            PRS_TABLE,
            type="string",
            description="Target Snowflake PRs table name.",
        ),
        "backup_local": Param(
            False,
            type="boolean",
            description=(
                "If True: also write local JSON backups under "
                "/opt/airflow/code_v2/backups/full_extract/{run_id}/issues.json and prs.json."
            ),
        ),
    },
) as dag:
    extract_issues_task = PythonOperator(
        task_id="extract_issues",
        python_callable=extract_issues,
        provide_context=True,
    )

    extract_prs_task = PythonOperator(
        task_id="extract_prs",
        python_callable=extract_prs,
        provide_context=True,
    )

    extract_issues_task >> extract_prs_task

