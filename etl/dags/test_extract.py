"""
Test GitHub Extract DAG (Samples Only)
=====================================
Runs the same GitHub extraction logic as `dags/full_extract.py` but:
- pulls ONLY 10 issues (and the PRs linked from those issues)
- writes JSON outputs to your *host* project folder via the existing mount:

  /opt/airflow/code_v2/test_output/issues_test.json
  /opt/airflow/code_v2/test_output/prs_test.json

So you can view results locally at:
  ./test_output/issues_test.json
  ./test_output/prs_test.json
"""

from datetime import datetime, timedelta
import json
import logging
import time
from pathlib import Path

from airflow import DAG
from airflow.operators.python import PythonOperator

import full_extract as prod


TEST_OUTPUT_DIR = Path("/opt/airflow/code_v2/test_output")
TEST_CHECKPOINT_DIR = TEST_OUTPUT_DIR / "checkpoints"


default_args = {
    "owner": "autobot",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 0,
    "retry_delay": timedelta(minutes=1),
}


def _load_checkpoint(name):
    TEST_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_CHECKPOINT_DIR / f"{name}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def _save_checkpoint(name, data):
    TEST_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_CHECKPOINT_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f)


def extract_issues_test(**_context):
    logging.info("=" * 60)
    logging.info("TEST EXTRACT — ISSUES (10)")
    logging.info(f"Repo: {prod.REPO_OWNER}/{prod.REPO_NAME}")
    logging.info("=" * 60)

    token = prod._get_github_token()
    session = prod._build_session(token)

    processed_numbers = set(_load_checkpoint("issues_processed") or [])
    all_linked_prs = dict(_load_checkpoint("linked_pr_numbers") or {})

    list_url = f"{prod.GITHUB_API_BASE}/repos/{prod.REPO_OWNER}/{prod.REPO_NAME}/issues"
    list_params = {"state": "closed", "sort": "created", "direction": "asc"}

    # NOTE: GitHub's /issues endpoint includes PRs; we must filter them out.
    # Also: "max_items=10" would mean "10 returned items" not "10 issues".
    remaining_quota = max(10 - len(processed_numbers), 0)
    issue_numbers_to_process = []
    scanned = 0
    for item in prod._paginate(session, list_url, list_params, max_items=None):
        scanned += 1
        num = item.get("number")
        if not num or num in processed_numbers:
            continue
        if item.get("pull_request"):
            continue
        issue_numbers_to_process.append(num)
        if len(issue_numbers_to_process) >= remaining_quota:
            break

    logging.info(
        f"Found {len(issue_numbers_to_process)} issues to process "
        f"(quota remaining={remaining_quota}, scanned_items={scanned})"
    )

    records = []
    total = len(issue_numbers_to_process)
    for idx, issue_number in enumerate(issue_numbers_to_process):
        logging.info(f"  Issue {idx+1}/{total}: #{issue_number}")
        record = prod._fetch_issue_full(session, issue_number)
        records.append(record)

        if record.get("linked_pr_numbers"):
            all_linked_prs[str(issue_number)] = record["linked_pr_numbers"]

        processed_numbers.add(issue_number)
        _save_checkpoint("issues_processed", list(processed_numbers))
        _save_checkpoint("linked_pr_numbers", all_linked_prs)

        time.sleep(0.5)

    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TEST_OUTPUT_DIR / "issues_test.json"
    with open(out_path, "w") as f:
        json.dump(records, f, indent=2, default=str)

    logging.info(f"Wrote {len(records)} issues to {out_path}")


def extract_prs_test(**_context):
    logging.info("=" * 60)
    logging.info("TEST EXTRACT — PRS (linked only)")
    logging.info(f"Repo: {prod.REPO_OWNER}/{prod.REPO_NAME}")
    logging.info("=" * 60)

    token = prod._get_github_token()
    session = prod._build_session(token)

    all_linked_prs = dict(_load_checkpoint("linked_pr_numbers") or {})
    if not all_linked_prs:
        logging.warning("No linked PRs found from issues phase. Run extract_issues_test first.")
        return

    pr_to_issues = {}
    for issue_str, pr_list in all_linked_prs.items():
        for pr_num in pr_list:
            pr_to_issues.setdefault(pr_num, []).append(int(issue_str))

    unique_pr_numbers = sorted(pr_to_issues.keys())
    records = []
    total = len(unique_pr_numbers)

    for idx, pr_number in enumerate(unique_pr_numbers):
        linked_issue = pr_to_issues.get(pr_number, [None])[0]
        logging.info(f"  PR {idx+1}/{total}: #{pr_number} (linked to issue #{linked_issue})")
        rec = prod._fetch_pr_full(session, pr_number, linked_issue)
        if rec is not None:
            records.append(rec)
        time.sleep(0.5)

    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TEST_OUTPUT_DIR / "prs_test.json"
    with open(out_path, "w") as f:
        json.dump(records, f, indent=2, default=str)

    logging.info(f"Wrote {len(records)} PRs to {out_path}")


with DAG(
    dag_id="test_extract",
    default_args=default_args,
    description="Test extract: 10 issues + linked PRs, output JSON locally",
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["test", "extract", "github", "samples"],
) as dag:
    extract_issues_task = PythonOperator(
        task_id="extract_issues_test",
        python_callable=extract_issues_test,
        provide_context=True,
    )

    extract_prs_task = PythonOperator(
        task_id="extract_prs_test",
        python_callable=extract_prs_test,
        provide_context=True,
    )

    extract_issues_task >> extract_prs_task

