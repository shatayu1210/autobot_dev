"""
Full GitHub Extract DAG (Production v2.0 - Optimized)
====================================================
Extracts closed issues + linked PRs from apache/airflow into Snowflake RAW.

Optimized for AutoBot v4 Training Data:
- Scorer/Reasoner: Timeline, comment gaps, days open [cite: 63-91]
- Planner: Issue context, repo symbols [cite: 110-124]
- Patcher: Unified diffs (ground truth) [cite: 141-150]
- Critic: CI check-runs, review comments [cite: 156-167]
"""

from datetime import datetime, timedelta
import logging
import json
import os
import time
import asyncio
import re
import random
from pathlib import Path
from typing import List, Dict, Any

import httpx
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.models import Param, Variable
from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook

# ============================================================================
# CONSTANTS & CONFIG (Restored from Original)
# ============================================================================

GITHUB_API_BASE = "https://api.github.com"
REPO_OWNER = "apache"
REPO_NAME = "airflow"
# Persisted via Docker volume (survives container restart)
CHECKPOINT_DIR = Path("/opt/airflow/code_v2/checkpoints")
EXTRACTED_DATA_DIR = Path("/opt/airflow/code_v2/extracted_data")  # local sink output
LOCAL_TEMP_DIR = Path("/tmp/autobot_bulk_load")  # ephemeral staging for Snowflake PUT
CONCURRENCY_LIMIT = 15

ISSUES_TABLE = "GITHUB_ISSUES"
PRS_TABLE = "GITHUB_PRS"

default_args = {
    "owner": "autobot",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
}

# ============================================================================
# EFFICIENCY ENGINES: ASYNC & BULK LOAD
# ============================================================================

def _get_github_token():
    """Consistent with original: App Token first, then Variable/Env."""
    try:
        import jwt
        app_id = Variable.get("GITHUB_APP_ID")
        install_id = Variable.get("GITHUB_APP_INSTALLATION_ID")
        pk = Variable.get("GITHUB_APP_PRIVATE_KEY").replace("\\n", "\n").strip()
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
        token = jwt.encode(payload, pk, algorithm="RS256")
        resp = httpx.post(f"https://api.github.com/app/installations/{install_id}/access_tokens",
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        return resp.json()["token"]
    except:
        return Variable.get("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN"))

class GitHubAsyncClient:
    def __init__(self, token):
        self._token = token
        self.client = httpx.AsyncClient(
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=60.0,
        )
        self.semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        self._rate_limit_until: float = 0.0  # epoch seconds to sleep until
        self._token_lock = asyncio.Lock()  # prevent concurrent token refreshes

    def _refresh_token(self):
        """Fetch a fresh GitHub App token and hot-swap it into the httpx session headers."""
        logging.warning("GitHub token expired (401). Refreshing token...")
        new_token = _get_github_token()
        self._token = new_token
        self.client.headers["Authorization"] = f"token {new_token}"
        logging.info("GitHub token refreshed successfully.")

    async def _wait_for_rate_limit(self):
        """If a rate limit window is active, sleep until it clears."""
        now = time.time()
        if self._rate_limit_until > now:
            sleep_secs = self._rate_limit_until - now + 2  # +2s buffer
            logging.warning(
                f"Rate limit active — sleeping {sleep_secs:.0f}s until reset "
                f"({time.strftime('%H:%M:%S', time.gmtime(self._rate_limit_until))} UTC)"
            )
            await asyncio.sleep(sleep_secs)

    async def request(self, url, params=None, method="GET", json_data=None):
        await self._wait_for_rate_limit()
        async with self.semaphore:
            for attempt in range(8):  # more attempts since rate-limit sleeps don't count
                try:
                    resp = (
                        await self.client.post(url, json=json_data)
                        if method == "POST"
                        else await self.client.get(url, params=params)
                    )

                    # Log remaining quota periodically
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    reset_at = resp.headers.get("X-RateLimit-Reset")
                    if remaining and int(remaining) < 200:
                        logging.warning(
                            f"GitHub rate limit low: {remaining} remaining, "
                            f"resets at {time.strftime('%H:%M:%S UTC', time.gmtime(int(reset_at)))}"
                        )

                    if resp.status_code == 200:
                        return resp.json()

                    if resp.status_code == 401:
                        # Token expired — refresh and retry without consuming an attempt
                        async with self._token_lock:
                            self._refresh_token()
                        continue

                    if resp.status_code in (403, 429):
                        # Prefer X-RateLimit-Reset over Retry-After for primary limit
                        reset_ts = resp.headers.get("X-RateLimit-Reset")
                        retry_after = resp.headers.get("Retry-After")

                        if reset_ts:
                            self._rate_limit_until = float(reset_ts)
                            sleep_secs = max(float(reset_ts) - time.time(), 0) + 2
                            logging.warning(
                                f"Primary rate limit hit (HTTP {resp.status_code}). "
                                f"Sleeping {sleep_secs:.0f}s until "
                                f"{time.strftime('%H:%M:%S UTC', time.gmtime(float(reset_ts)))} — "
                                f"then retrying {url}"
                            )
                            await asyncio.sleep(sleep_secs)
                            self._rate_limit_until = 0.0
                        elif retry_after:
                            # Secondary / abuse rate limit
                            sleep_secs = int(retry_after) + 2
                            logging.warning(
                                f"Secondary rate limit (Retry-After={retry_after}s). "
                                f"Sleeping {sleep_secs}s — then retrying {url}"
                            )
                            await asyncio.sleep(sleep_secs)
                        else:
                            # Fallback: 60s
                            logging.warning(f"HTTP {resp.status_code} with no reset header — sleeping 60s")
                            await asyncio.sleep(60)

                        # Do NOT increment attempt — rate limit sleeps are free retries
                        continue

                    if resp.status_code == 404:
                        return None  # Issue genuinely doesn't exist

                    resp.raise_for_status()

                except httpx.TimeoutException:
                    backoff = min(2 ** attempt * 5, 60) + random.random()
                    logging.warning(f"Timeout on {url} (attempt {attempt+1}/8). Retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                except Exception as e:
                    backoff = min(2 ** attempt * 3, 60) + random.random()
                    logging.warning(f"Request error on {url} (attempt {attempt+1}/8): {e}. Retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)

        logging.error(f"All retries exhausted for {url} — returning None")
        return None

def _write_local(table_name, records):
    """Persist JSONL to the Docker-volume-backed extracted_data dir (local sink mode)."""
    if not records: return
    EXTRACTED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_path = EXTRACTED_DATA_DIR / f"{table_name}_{int(time.time())}.jsonl"
    with open(file_path, "w") as f:
        for r in records: f.write(json.dumps(r) + "\n")
    logging.info(f"Local mode: wrote {len(records)} records to {file_path}")

def _bulk_load_snowflake(table_name, records, database, schema):
    """Uses PUT + COPY INTO for massive ingestion speed. Retries on transient DNS/connection errors."""
    if not records: return
    LOCAL_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    file_path = LOCAL_TEMP_DIR / f"{table_name}_{int(time.time())}.jsonl"
    with open(file_path, "w") as f:
        for r in records: f.write(json.dumps(r) + "\n")

    for sf_attempt in range(5):
        try:
            hook = SnowflakeHook(snowflake_conn_id="snowflake_default")
            conn = hook.get_conn()
            with conn.cursor() as cur:
                cur.execute(f"USE DATABASE {database}"); cur.execute(f"USE SCHEMA {schema}")
                cur.execute(f"CREATE OR REPLACE TEMPORARY STAGE {table_name}_STG")
                cur.execute(f"PUT file://{file_path} @{table_name}_STG")

                if table_name == ISSUES_TABLE:
                    cur.execute(f"""
                        COPY INTO {table_name} (
                            issue_number, repo, title, state, labels,
                            assignee_count, milestone, comment_count, linked_pr_count,
                            created_at, closed_at, extracted_at, raw_json
                        )
                        FROM (
                            SELECT 
                                $1:issue_number::INTEGER,
                                $1:repo::VARCHAR,
                                SUBSTR($1:issue:title::VARCHAR, 1, 1000),
                                $1:issue:state::VARCHAR,
                                $1:label_names::ARRAY,
                                COALESCE(ARRAY_SIZE($1:issue:assignees::ARRAY), 0),
                                SUBSTR($1:issue:milestone:title::VARCHAR, 1, 500),
                                IFNULL($1:issue:comments::INTEGER, 0),
                                COALESCE(ARRAY_SIZE($1:linked_pr_numbers::ARRAY), 0),
                                $1:issue:created_at::TIMESTAMP_NTZ,
                                $1:issue:closed_at::TIMESTAMP_NTZ,
                                $1:extracted_at::TIMESTAMP_NTZ,
                                $1
                            FROM @{table_name}_STG
                        )
                        FILE_FORMAT=(TYPE='JSON')
                    """)
                elif table_name == PRS_TABLE:
                    cur.execute(f"""
                        COPY INTO {table_name} (
                            pr_number, repo, linked_issue_number, pr_title, state,
                            is_merged, base_sha, head_sha, changed_files_count,
                            review_count, ci_conclusion, merged_at, created_at,
                            extracted_at, raw_json
                        )
                        FROM (
                            SELECT 
                                $1:pr_number::INTEGER,
                                $1:repo::VARCHAR,
                                $1:linked_issue_number::INTEGER,
                                SUBSTR($1:pr:title::VARCHAR, 1, 1000),
                                $1:pr:state::VARCHAR,
                                CASE WHEN $1:pr:merged_at IS NOT NULL THEN TRUE ELSE FALSE END,
                                $1:pr:base:sha::VARCHAR,
                                $1:pr:head:sha::VARCHAR,
                                IFNULL($1:pr:changed_files::INTEGER, 0),
                                COALESCE(ARRAY_SIZE($1:reviews::ARRAY), 0),
                                $1:ci_conclusion::VARCHAR,
                                $1:pr:merged_at::TIMESTAMP_NTZ,
                                $1:pr:created_at::TIMESTAMP_NTZ,
                                $1:extracted_at::TIMESTAMP_NTZ,
                                $1
                            FROM @{table_name}_STG
                        )
                        FILE_FORMAT=(TYPE='JSON')
                    """)
                else:
                    cur.execute(f"COPY INTO {table_name} (raw_json) FROM @{table_name}_STG FILE_FORMAT=(TYPE='JSON')")
            conn.commit()
            return  # success
        except Exception as e:
            if sf_attempt < 4:
                wait = 30 * (sf_attempt + 1)
                logging.warning(f"Snowflake connection error (attempt {sf_attempt+1}/5): {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logging.error(f"Snowflake bulk load failed after 5 attempts: {e}")
                raise

# ============================================================================
# SIGNAL FETCHERS (AutoBot v4 Signal Compliance)
# ============================================================================

async def _fetch_issue_full_async(client, num):
    """Signals: Timeline friction, comment gaps [cite: 75, 84-87]"""
    base = f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{num}"
    tasks = [client.request(base), client.request(f"{base}/comments"), 
             client.request(f"{base}/timeline"), client.request(f"{base}/sub_issues")]
    issue, comments, timeline, sub_issues = await asyncio.gather(*tasks, return_exceptions=True)
    
    if not isinstance(issue, dict):
        logging.warning(f"Issue #{num} returned non-dict response; skipping.")
        return None
        
    linked_prs = []
    fix_p = re.compile(r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*#(\d+)", re.IGNORECASE)
    body = (issue.get("body") or "") + " ".join([c.get("body", "") for c in (comments or []) if isinstance(c, dict)])
    linked_prs.extend([int(m.group(1)) for m in fix_p.finditer(body)])
    
    label_names = [l.get("name") for l in issue.get("labels", []) if isinstance(l, dict) and l.get("name")] if isinstance(issue, dict) else []
    
    return {"issue": issue, "comments": comments, "timeline": timeline, "sub_issues": sub_issues, 
            "linked_pr_numbers": list(set(linked_prs)), "issue_number": num, "repo": f"{REPO_OWNER}/{REPO_NAME}", 
            "label_names": label_names, "extracted_at": datetime.utcnow().isoformat()}

async def _fetch_pr_full_async(client, num, linked_issue=None):
    """Signals: Unified Diffs (Patcher) and Review Bodies (Critic) [cite: 148, 162-163]"""
    base = f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{num}"
    tasks = [client.request(base), client.request(f"{base}/files"), 
             client.request(f"{base}/reviews"), client.request(f"{base}/comments"),
             client.request(f"{base}/commits"), client.request(f"{base}/requested_reviewers")]
    pr, files, reviews, r_comments, commits, req_reviewers = await asyncio.gather(*tasks, return_exceptions=True)
    
    check_runs = []
    ci_conclusion = "none"
    if pr and isinstance(pr, dict) and pr.get("head", {}).get("sha"):
        res = await client.request(f"{GITHUB_API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/commits/{pr['head']['sha']}/check-runs")
        check_runs = res.get("check_runs", []) if res else []
        conclusions = [cr.get("conclusion") for cr in check_runs if isinstance(cr, dict) and cr.get("conclusion")]
        if not conclusions: ci_conclusion = "none"
        elif all(c == "success" for c in conclusions): ci_conclusion = "success"
        elif any(c in ("failure", "timed_out", "cancelled") for c in conclusions): ci_conclusion = "failure"
        else: ci_conclusion = "mixed"

    req_reviewers = req_reviewers if isinstance(req_reviewers, dict) else {"users": [], "teams": []}
    reviews = reviews if isinstance(reviews, list) else []
    
    reviewers_who_submitted = {r.get("user", {}).get("login") for r in reviews if isinstance(r, dict) and r.get("user")}
    requested_logins = [u.get("login") for u in req_reviewers.get("users", [])]
    silent_reviewers = [login for login in requested_logins if login not in reviewers_who_submitted]

    return {"pr": pr, "files": files, "reviews": reviews, "review_comments": r_comments, 
            "commits": commits, "requested_reviewers": req_reviewers, "silent_reviewers": silent_reviewers,
            "check_runs": check_runs, "ci_conclusion": ci_conclusion, "linked_issue_number": linked_issue, 
            "pr_number": num, "repo": f"{REPO_OWNER}/{REPO_NAME}", "extracted_at": datetime.utcnow().isoformat()}

# ============================================================================
# TASK DEFINITIONS
# ============================================================================

def extract_issues(**context):
    async def _run():
        params = context["params"]
        sink_mode = params.get("sink_mode", "snowflake")
        client = GitHubAsyncClient(_get_github_token())

        # Checkpoints — persisted to Docker volume, survives restarts
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        processed = set(json.load(open(CHECKPOINT_DIR / "issues_processed.json")) if (CHECKPOINT_DIR / "issues_processed.json").exists() else [])
        linked_prs = json.load(open(CHECKPOINT_DIR / "linked_pr_numbers.json")) if (CHECKPOINT_DIR / "linked_pr_numbers.json").exists() else {}
        logging.info(f"=== extract_issues START [sink={sink_mode}]: checkpoint has {len(processed)} issues already done. Targeting {params['issues_pull_size']} total. ===")

        # GraphQL Discovery
        query = "query($o:String!,$n:String!,$c:String){repository(owner:$o,name:$n){issues(states:CLOSED,first:100,after:$c,orderBy:{field:CREATED_AT,direction:DESC}){pageInfo{hasNextPage,endCursor}nodes{number,createdAt}}}}"
        nums, cursor = [], None
        while len(nums) < params["issues_pull_size"]:
            data = await client.request(f"{GITHUB_API_BASE}/graphql", method="POST", json_data={"query":query,"variables":{"o":REPO_OWNER,"n":REPO_NAME,"c":cursor}})
            if not data or not data.get("data"):
                logging.error(f"GraphQL discovery returned empty response (cursor={cursor}). Stopping discovery.")
                break
            nodes = data["data"]["repository"]["issues"]["nodes"]
            last_node = None
            for n in nodes:
                last_node = n
                if n["createdAt"] < "2019-01-01T00:00:00Z": break
                nums.append(n["number"])
            page_info = data["data"]["repository"]["issues"]["pageInfo"]
            if not page_info["hasNextPage"] or (last_node and last_node["createdAt"] < "2019-01-01T00:00:00Z"): break
            cursor = page_info["endCursor"]

        nums = [n for n in nums if n not in processed]
        logging.info(f"Discovery complete: {len(nums)} issues remaining to fetch after checkpoint filter.")

        for i in range(0, len(nums), 100):
            chunk = nums[i:i+100]
            results = await asyncio.gather(*[_fetch_issue_full_async(client, n) for n in chunk])
            batch = [r for r in results if r and isinstance(r, dict)]
            for r in batch:
                linked_prs[str(r["issue"]["number"])] = r["linked_pr_numbers"]
                processed.add(r["issue"]["number"])

            if sink_mode == "local":
                _write_local(params["issues_table"], batch)
            else:
                _bulk_load_snowflake(params["issues_table"], batch, params["snowflake_database"], params["snowflake_schema"])

            json.dump(list(processed), open(CHECKPOINT_DIR / "issues_processed.json", "w"))
            json.dump(linked_prs, open(CHECKPOINT_DIR / "linked_pr_numbers.json", "w"))
            remaining = max(len(nums) - (i + 100), 0)
            sink_label = "JSONL" if sink_mode == "local" else "Snowflake"
            logging.info(f"Progress: {len(processed)} issues total processed. Wrote {len(batch)} to {sink_label}. ~{remaining} remaining this run.")

        logging.info(f"=== extract_issues DONE: {len(processed)} issues total, {sum(len(v) for v in linked_prs.values())} linked PR refs found. ===")

    asyncio.run(_run())

def extract_prs(**context):
    async def _run():
        params = context["params"]
        sink_mode = params.get("sink_mode", "snowflake")
        client = GitHubAsyncClient(_get_github_token())

        # Load linked_pr_numbers for supplementary issue→PR enrichment (best-effort)
        linked_data = json.load(open(CHECKPOINT_DIR / "linked_pr_numbers.json")) \
            if (CHECKPOINT_DIR / "linked_pr_numbers.json").exists() else {}
        pr_to_issue = {p: int(i) for i, prs in linked_data.items() for p in prs}

        # === GraphQL Discovery: enumerate ALL closed+merged PRs directly ===
        # Most PRs are NOT linked via "fixes #N" keywords in issue bodies.
        # The correct approach is direct enumeration, same as extract_issues.
        #
        # RESILIENCE: After a successful discovery, the full PR list is cached to
        # CHECKPOINT_DIR/pr_discovery_cache.json. On retry, if GraphQL fails (e.g. network
        # down at task startup), we fall back to the cache — preventing the false-success
        # where 0 PRs are discovered and the task exits claiming it is done.
        PR_DISCOVERY_CACHE = CHECKPOINT_DIR / "pr_discovery_cache.json"

        pr_query = "query($o:String!,$n:String!,$c:String){repository(owner:$o,name:$n){pullRequests(states:[CLOSED,MERGED],first:100,after:$c,orderBy:{field:CREATED_AT,direction:DESC}){pageInfo{hasNextPage,endCursor}nodes{number,createdAt}}}}"
        all_pr_nums, cursor = [], None
        while len(all_pr_nums) < params["prs_pull_size"]:
            data = await client.request(
                f"{GITHUB_API_BASE}/graphql", method="POST",
                json_data={"query": pr_query, "variables": {"o": REPO_OWNER, "n": REPO_NAME, "c": cursor}}
            )
            if not data or not data.get("data"):
                logging.error(f"GraphQL PR discovery returned empty response (cursor={cursor}). Stopping discovery.")
                break
            nodes = data["data"]["repository"]["pullRequests"]["nodes"]
            last_node = None
            for n in nodes:
                last_node = n
                if n["createdAt"] < "2019-01-01T00:00:00Z": break
                all_pr_nums.append(n["number"])
            page_info = data["data"]["repository"]["pullRequests"]["pageInfo"]
            if not page_info["hasNextPage"] or (last_node and last_node["createdAt"] < "2019-01-01T00:00:00Z"): break
            cursor = page_info["endCursor"]

        if all_pr_nums:
            # Successful discovery — persist to cache so future retries can fall back
            json.dump(all_pr_nums, open(PR_DISCOVERY_CACHE, "w"))
            logging.info(f"PR Discovery complete: {len(all_pr_nums)} total PRs found via GraphQL. Cache updated.")
        else:
            # Discovery failed (network down) — load cached list from previous successful run
            if PR_DISCOVERY_CACHE.exists():
                all_pr_nums = json.load(open(PR_DISCOVERY_CACHE))
                logging.warning(
                    f"GraphQL discovery returned 0 PRs (network issue?). "
                    f"Falling back to cached discovery list: {len(all_pr_nums)} PRs."
                )
            else:
                raise RuntimeError(
                    "GraphQL PR discovery failed (network unreachable) AND no local cache exists. "
                    "Fix network connectivity and retrigger the task."
                )

        processed = set(json.load(open(CHECKPOINT_DIR / "prs_processed.json")) if (CHECKPOINT_DIR / "prs_processed.json").exists() else [])
        pr_list = [p for p in all_pr_nums if p not in processed]

        logging.info(
            f"=== extract_prs START [sink={sink_mode}]: discovered {len(all_pr_nums)} PRs. "
            f"Checkpoint has {len(processed)} done. {len(pr_list)} remaining to fetch. ==="
        )

        for i in range(0, len(pr_list), 100):
            chunk = pr_list[i:i+100]
            results = await asyncio.gather(*[_fetch_pr_full_async(client, p, pr_to_issue.get(p)) for p in chunk])
            batch = [r for r in results if r and isinstance(r.get("pr"), dict) and r["pr"].get("number")]
            for r in batch:
                processed.add(r["pr"]["number"])

            if sink_mode == "local":
                _write_local(params["prs_table"], batch)
            else:
                _bulk_load_snowflake(params["prs_table"], batch, params["snowflake_database"], params["snowflake_schema"])

            json.dump(list(processed), open(CHECKPOINT_DIR / "prs_processed.json", "w"))
            remaining = max(len(pr_list) - (i + 100), 0)
            sink_label = "JSONL" if sink_mode == "local" else "Snowflake"
            logging.info(f"Progress: {len(processed)} PRs total processed. Wrote {len(batch)} to {sink_label}. ~{remaining} remaining this run.")

        logging.info(f"=== extract_prs DONE: {len(processed)} PRs total processed. ===")

    asyncio.run(_run())

# ============================================================================
# DAG DEFINITION (Original Descriptions & Hints)
# ============================================================================

with DAG(
    dag_id="full_extract",
    default_args=default_args,
    description="Full GitHub extract: closed issues + linked PRs from apache/airflow into Snowflake RAW",
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    params={
        "sink_mode": Param("local", type="string", description="'local' = write JSONL to Docker volume (travel/unstable network). 'snowflake' = load to Snowflake."),
        "issues_pull_size": Param(60000, type="integer", description="Max closed issues to pull (post-2019)."),
        "prs_pull_size": Param(60000, type="integer", description="Safety cap on linked PRs."),
        "snowflake_database": Param("AIRFLOW_ML", type="string", description="Target Snowflake database."),
        "snowflake_schema": Param("RAW", type="string", description="Target Snowflake schema."),
        "issues_table": Param(ISSUES_TABLE, type="string", description="Target Snowflake issues table name."),
        "prs_table": Param(PRS_TABLE, type="string", description="Target Snowflake PRs table name."),
    }
) as dag:
    t1 = PythonOperator(task_id="extract_issues", python_callable=extract_issues)
    t2 = PythonOperator(task_id="extract_prs", python_callable=extract_prs)
    t1 >> t2