"""snowflake_issue_to_messages.py

Fetch a single issue row from Snowflake (ICEBERG_ML.LABELLED.ISSUE_PRS_SIGNALS_LABELLED)
and build a messages payload that mirrors the training JSONL structure produced
by prepare_bottleneck_splits.py.

Usage:
  python3 snowflake_issue_to_messages.py --issue-number 13696
  python3 snowflake_issue_to_messages.py --issue-number 13696 --out messages.json

Environment (.env or shell):
  SNOWFLAKE_ACCOUNT
  SNOWFLAKE_USER
  SNOWFLAKE_PASSWORD
  SNOWFLAKE_WAREHOUSE (optional, default COMPUTE_WH)
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from datetime import datetime

import snowflake.connector
from dotenv import load_dotenv


load_dotenv()

SF_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT")
SF_USER = os.getenv("SNOWFLAKE_USER")
SF_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD")
SF_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")

SF_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "ICEBERG_ML")
SF_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "ADHOC")
SF_TABLE = os.getenv("SNOWFLAKE_TABLE", "BOTTLENECK_LABELLED")


PR_SAFE_FIELDS = [
    "pr_title",
    "pr_body",
    "mergeable",
    "mergeable_state",
    "rebaseable",
    "reviews_count",
    "approved_count",
    "changes_requested_count",
    "cal_risk_score",
    "hours_since_last_update",
]

_BODY_CHAR_LIMIT = 800
_COMMENTS_CHAR_LIMIT = 400
_PR_BODY_CHAR_LIMIT = 300
_COMMENT_BODY_CHAR_LIMIT = 100
_MAX_COMMENTS_IN_THREAD = 3


SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are an expert software engineering process analyst specialising in GitHub
    project bottleneck detection.

    Your task is to evaluate a GitHub issue (and any linked pull requests) and
    assign a BOTTLENECK RISK SCORE from 0.0 to 1.0, where:
      0.0 = No bottleneck risk — issue progressing smoothly
      1.0 = Maximum bottleneck risk — severely stuck, stalled, or blocked

    IMPORTANT — Observations must be grounded in the actual data provided:
    - Every "observation" field MUST cite a specific value from the issue or PR
      data (e.g. "No assignees (ASSIGNEES_COUNT=0)", "17,195 hours since last
      update", "PR #3 has 101 review comments and 0 approvals").
    - Do NOT copy signal names from these instructions. Only report signals that
      are actually present and notable in THIS specific issue.
    - Do NOT write generic observations like "lack of triage" or "stale updates"
      without backing them with a concrete data point from the issue.
    - Omit any signal category where the data shows no meaningful concern.
    - Not all issues have linked pull requests. The absence of a PR is NOT itself
      a bottleneck signal — many issues are legitimately resolved through
      discussion, documentation, or direct commits without a formal PR.

    You MUST respond ONLY with a valid JSON object in this exact format
    (no markdown fences, no extra text outside the JSON):

    {
      "teacher_risk_score": <float 0.0 to 1.0>,
      "teacher_reasons": [
        {"signal": "<short category name>", "observation": "<specific fact with numbers from the data>"},
        ...
      ],
      "teacher_confidence": "<low|medium|high>"
    }

    Use "high" confidence when the data clearly supports the score.
    Use "medium" when signals are mixed or partially available.
    Use "low" when data is too sparse to score reliably (e.g., no body, no PRs,
    very few signals). Low-confidence responses will be discarded.
"""
)


def _sanitize_prs(prs_raw) -> list[dict]:
    if prs_raw is None:
        return []
    if isinstance(prs_raw, str):
        try:
            prs_raw = json.loads(prs_raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(prs_raw, list):
        return []

    sanitized: list[dict] = []
    for pr in prs_raw:
        if not isinstance(pr, dict):
            continue
        clean_pr = {k: v for k, v in pr.items() if k.lower() in PR_SAFE_FIELDS}
        sanitized.append(clean_pr)
    return sanitized


def _compress_comments(raw) -> str:
    if not raw:
        return "(no comments)"

    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:_COMMENTS_CHAR_LIMIT]
    else:
        data = raw

    if not isinstance(data, list) or not data:
        return "(no comments)"

    excerpts: list[str] = []
    for i, comment in enumerate(data[:_MAX_COMMENTS_IN_THREAD], 1):
        if not isinstance(comment, dict):
            continue
        author = comment.get("author") or comment.get("user") or "unknown"
        body = " ".join(str(comment.get("body") or "").split())[:_COMMENT_BODY_CHAR_LIMIT]
        if body:
            excerpts.append(f"[Comment {i}] {author}: {body}")

    remaining = len(data) - _MAX_COMMENTS_IN_THREAD
    if remaining > 0:
        excerpts.append(f"... (+{remaining} more comments not shown)")

    return "\n".join(excerpts) if excerpts else "(no comments)"


def build_user_prompt(row: dict) -> str:
    prs = _sanitize_prs(row.get("PRS"))
    # Limit to the most recent 2 PRs if many exist
    if len(prs) > 2:
        prs = prs[-2:]
    body = (row.get("BODY") or "")[:_BODY_CHAR_LIMIT]
    comments = _compress_comments(row.get("COMMENTS"))

    lines = [
        "## Issue Details",
        f"- **Title**: {row.get('TITLE', 'N/A')}",
        f"- **Author Association**: {row.get('AUTHOR_ASSOCIATION', 'N/A')}",
        f"- **Labels**: {row.get('LABELS_TEXT', 'none')}",
        f"- **Label Count**: {row.get('LABEL_COUNT', 0)} / Changes: {row.get('LABEL_CHANGES_COUNT', 0)}",
        f"- **Is Bug**: {row.get('LABEL_BUG')} | Is API: {row.get('LABEL_API')} | Is Docs: {row.get('LABEL_DOCUMENTATION')}",
        "",
        "### Quality Signals",
        f"- Has Repro Steps: {row.get('HAS_REPRO_STEPS')}",
        f"- Has Stack Trace: {row.get('HAS_STACK_TRACE')}",
        f"- Has Error Message: {row.get('HAS_ERROR_MESSAGE')}",
        f"- Has Expected/Actual: {row.get('HAS_EXPECTED_ACTUAL')}",
        f"- Has Code Block: {row.get('HAS_CODE_BLOCK')}",
        "",
        "### Engagement",
        f"- Has Assignees: {row.get('HAS_ASSIGNEES')} | Assignees Count: {row.get('ASSIGNEES_COUNT', 0)}",
        f"- Has Milestone: {row.get('HAS_MILESTONE')} | Milestones Count: {row.get('MILESTONES_COUNT', 0)}",
        f"- Mentions Count: {row.get('MENTIONS_COUNT', 0)}",
        "",
        "### Timing Signals",
        f"- Created Day of Week: {row.get('CREATED_DOW')} | Hour: {row.get('CREATED_HOUR')} | Weekend: {row.get('CREATED_IS_WEEKEND')}",
        f"- Hours Since Last Update: {row.get('HOURS_SINCE_LAST_UPDATE', 'N/A')}",
        f"- Time to First Activity (hrs): {row.get('TIME_TO_FIRST_ACTIVITY_HOURS', 'N/A')}",
        f"- Time to First Comment (hrs): {row.get('TIME_TO_FIRST_COMMENT_HOURS', 'N/A')}",
        f"- Time to First Label (hrs): {row.get('TIME_TO_FIRST_LABEL_HOURS', 'N/A')}",
        f"- Time to First Assignment (hrs): {row.get('TIME_TO_FIRST_ASSIGNMENT_HOURS', 'N/A')}",
        f"- Time to First Milestone (hrs): {row.get('TIME_TO_FIRST_MILESTONE_HOURS', 'N/A')}",
        "",
        "### Activity Counts",
        f"- Comments: {row.get('COMMENTS_COUNT', 0)} | Timeline Comments: {row.get('TIMELINE_COMMENTS_COUNT', 0)}",
        f"- Total Timeline Events: {row.get('TOTAL_TIMELINE_EVENTS', 0)}",
        f"- Cross References: {row.get('CROSS_REFERENCES_COUNT', 0)} | References: {row.get('REFERENCES_COUNT', 0)}",
        f"- Reassignments: {row.get('REASSIGNMENTS_COUNT', 0)} | Renames: {row.get('RENAMES_COUNT', 0)}",
        f"- Has Reopenings: {row.get('HAS_REOPENINGS')} | Reopenings Count: {row.get('REOPENINGS_COUNT', 0)}",
        f"- Subscriptions: {row.get('SUBSCRIPTIONS_COUNT', 0)}",
        "",
        "### Heuristic Risk Score (Calculated)",
        f"- Final Combined CAL Risk Score (0-100): {row.get('CAL_RISK_SCORE', 'N/A')}",
        "",
        "### Issue Body (excerpt)",
        body or "(no body)",
        "",
        "### Key Comments (up to 5 excerpts)",
        comments,
    ]

    if prs:
        lines.append("")
        lines.append(f"## Linked Pull Requests ({len(prs)} PR(s))")
        for i, pr in enumerate(prs, 1):
            lines.append(f"\n### PR #{i}")
            for field in PR_SAFE_FIELDS:
                val = pr.get(field)
                if val is None:
                    continue
                if field == "pr_body" and isinstance(val, str):
                    val = val[:_PR_BODY_CHAR_LIMIT]
                lines.append(f"- **{field}**: {val}")
    else:
        lines.append("")
        lines.append("## Linked Pull Requests")
        lines.append("None — this issue has no linked PRs.")

    lines.append("")
    lines.append(
        """Based on ALL the above information, assign the bottleneck risk score,
        reasons(give 3 concise bullet points on why it is a bottleneck.), and confidence. Remember: you are evaluating whether this issue
        is a bottleneck in the development process — stuck, stalled, blocked,
        or slow to resolve."""
    )
    return "\n".join(lines)


def build_agenda_prompt(row: dict, risk_score: str, confidence: str, reasons: str) -> str:
    """Combines critical issue metadata and bottleneck assessment for the agenda model.
    This version is lean to avoid prompt length limits.
    """
    title = row.get('TITLE', 'N/A')
    body = (row.get('BODY') or "")[:400]
    labels = row.get('LABELS_TEXT', 'none')
    
    agenda_context = [
        "## Issue Summary",
        f"- **Title**: {title}",
        f"- **Labels**: {labels}",
        f"- **Body Excerpt**: {body}",
        "",
        "## Bottleneck Analysis Results",
        f"- **Risk Score**: {risk_score}",
        f"- **Confidence**: {confidence}",
        f"- **Reasons**: {reasons}",
        "",
        "## Task",
        "Generate a professional meeting agenda with EXACTLY 3 concise bullet points discussing how to address the identified bottlenecks and move the issue forward.",
        "Start directly with the 3 points."
    ]
    
    return "\n".join(agenda_context)


def build_messages_payload(row: dict) -> dict:
    user_prompt = build_user_prompt(row)
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    }


def _fetch_issue_row(issue_number: int) -> dict:
    if not SF_ACCOUNT or not SF_USER or not SF_PASSWORD:
        raise RuntimeError(
            "Missing Snowflake creds: ensure SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD are set."
        )

    conn = snowflake.connector.connect(
        account=SF_ACCOUNT,
        user=SF_USER,
        password=SF_PASSWORD,
        warehouse=SF_WAREHOUSE,
        database=SF_DATABASE,
        schema=SF_SCHEMA,
    )
    try:
        cur = conn.cursor()
        raise RuntimeError(
            "This table does not contain ISSUE_NUMBER. Use _fetch_issue_row_by_where(...) instead."
        )

        desc = cur.description
        if not desc:
            raise RuntimeError("Snowflake returned no description; query may have failed.")

        row = cur.fetchone()
        if row is None:
            raise KeyError(
                f"Issue {issue_number} not found in {SF_DATABASE}.{SF_SCHEMA}.{SF_TABLE}."
            )

        columns = [d[0].upper() for d in desc]
        return dict(zip(columns, row))
    finally:
        conn.close()


def _get_snowflake_connection() -> snowflake.connector.SnowflakeConnection:
    import time
    last_err = None
    for attempt in range(3):
        try:
            print(f"DEBUG: Snowflake connection attempt {attempt+1} for account: {SF_ACCOUNT}")
            conn = snowflake.connector.connect(
                account=SF_ACCOUNT,
                user=SF_USER,
                password=SF_PASSWORD,
                warehouse=SF_WAREHOUSE,
                database=SF_DATABASE,
                schema=SF_SCHEMA,
                login_timeout=30,
            )
            print("DEBUG: Snowflake connection established.")
            return conn
        except Exception as e:
            last_err = e
            print(f"DEBUG: Connection attempt {attempt+1} failed: {e}")
            time.sleep(1)
    
    raise RuntimeError(f"Snowflake backend unreachable for account '{SF_ACCOUNT}' after 3 attempts. Error: {last_err}")

def _fetch_issue_row_by_where(where_sql: str, params: tuple, *, schema: str, table: str) -> dict:
    if not SF_ACCOUNT or not SF_USER or not SF_PASSWORD:
        raise RuntimeError(
            "Missing Snowflake creds: ensure SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD are set."
        )

    conn = _get_snowflake_connection()
    try:
        cur = conn.cursor()
        query = f"""
        SELECT *
        FROM {SF_DATABASE}.{schema}.{table}
        WHERE {where_sql}
        LIMIT 1
        """.strip()
        cur.execute(query, params)

        desc = cur.description
        if not desc:
            raise RuntimeError("Snowflake returned no description; query may have failed.")

        row = cur.fetchone()
        if row is None:
            raise KeyError(
                f"No row matched WHERE clause in {SF_DATABASE}.{schema}.{table}: {where_sql}"
            )

        columns = [d[0].upper() for d in desc]
        return dict(zip(columns, row))
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", type=int, default=None)
    parser.add_argument("--title", type=str, default="")
    parser.add_argument(
        "--where",
        type=str,
        default="",
        help="Optional raw SQL predicate after WHERE, e.g. \"TITLE ILIKE %s\".",
    )
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    t0 = datetime.utcnow()

    schema = SF_SCHEMA
    table = SF_TABLE

    if args.where:
        row = _fetch_issue_row_by_where(args.where, tuple(), schema=schema, table=table)
    elif args.title:
        row = _fetch_issue_row_by_where("TITLE = %s", (args.title,), schema=schema, table=table)
    elif args.issue_number is not None:
        row = _fetch_issue_row_by_where(
            "ISSUE_NUMBER = %s", (args.issue_number,), schema=schema, table=table
        )
    else:
        raise SystemExit(
            "Provide one of: --where, --title, or --issue-number."
        )

    payload = build_messages_payload(row)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[OK] Wrote messages payload → {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    dt_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
    print(f"[Done] issue_number={args.issue_number} | elapsed_ms={dt_ms}")


if __name__ == "__main__":
    main()
