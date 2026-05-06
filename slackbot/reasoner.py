import requests
from config import HF_TOKEN, REASONER_ENDPOINT

HEADERS = {
    "Authorization": f"Bearer {HF_TOKEN}",
    "Content-Type": "application/json"
}

def _snapshot_tier(days_open: int) -> str:
    """Map days_open to the snapshot tier label used in training data."""
    if days_open <= 1:  return "T+1"
    elif days_open <= 7:  return "T+7"
    elif days_open <= 14: return "T+14"
    elif days_open <= 30: return "T+30"
    else:                 return "T+30+"


def build_prompt(
    title: str,
    body: str,
    labels: list = None,
    days_open: int = 0,
    assignee_count: int = 0,
    comment_count: int = 0,
    linked_pr_count: int = 0,
    pr_states: list = None,
    ci_status: str = "none",
    max_comment_gap_days: float = 0.0,
    comments_text: str = "",
    silent_reviewers: int = 0,
    pr_review_feedback: str = "",
    risk_score: float = None,
    risk_band: str = None,
) -> str:
    """
    Build a prompt that closely matches the format the reasoner was trained on.

    Training format:
      Risk Score: 0.63 (medium)
      PROJECT: apache/airflow | P50=1d P75=6d P90=23d P95=47d
      ISSUE: {title} | LABELS: ... | ASSIGNEES: 0 | DAYS_OPEN: 7 | SNAPSHOT_TIER: T+7 |
             COMMENT_COUNT: 4 | MAX_COMMENT_GAP_DAYS: 0.0 | LINKED_PR_COUNT: 2 |
             PR_STATES: closed | SILENT_REVIEWERS: 13 | CI: failure
      BODY: ...
      COMMENTS: - [timestamp] author: text...
      PR Review Feedback: [PR #N STATE by reviewer]: text...
    """
    body_truncated    = (body or "No description provided.")[:2000]
    labels_str        = ", ".join(labels) if labels else "none"
    pr_states_str     = ", ".join(pr_states) if pr_states else "none"
    snapshot_tier     = _snapshot_tier(days_open)

    # Risk score line — only if we have it from the sentinel
    risk_line = ""
    if risk_score is not None and risk_band:
        risk_line = f"Risk Score: {risk_score:.2f} ({risk_band})\n\n"

    # Build the comments section
    comments_section = f"COMMENTS: {comments_text}" if comments_text else f"COMMENTS: {comment_count} comments"

    # Build PR review feedback section
    pr_feedback_section = f"\nPR Review Feedback:\n{pr_review_feedback}" if pr_review_feedback else ""

    return (
        f"{risk_line}"
        f"PROJECT: apache/airflow | P50=1d P75=6d P90=23d P95=47d\n"
        f"ISSUE: {title} | LABELS: {labels_str} | ASSIGNEES: {assignee_count} | "
        f"DAYS_OPEN: {days_open} | SNAPSHOT_TIER: {snapshot_tier} | "
        f"COMMENT_COUNT: {comment_count} | MAX_COMMENT_GAP_DAYS: {max_comment_gap_days} | "
        f"LINKED_PR_COUNT: {linked_pr_count} | PR_STATES: {pr_states_str} | "
        f"SILENT_REVIEWERS: {silent_reviewers} | CI: {ci_status}\n"
        f"BODY: {body_truncated}\n"
        f"{comments_section}"
        f"{pr_feedback_section}\n\n"
        f"Analysis:"
    )


def parse_analysis(generated_text: str) -> dict:
    """
    Parse the reasoner's narrative output.
    The model was trained to produce a free-form narrative paragraph.
    We store the full narrative and also extract a short summary from the first sentence.
    """
    narrative = generated_text.strip()

    # Extract first sentence as summary
    first_sentence = narrative.split(".")[0].strip() + "." if "." in narrative else narrative[:200]

    # Extract last sentence as suggested action
    sentences = [s.strip() for s in narrative.split(".") if s.strip()]
    suggested = (sentences[-1] + ".") if len(sentences) > 1 else "Review manually."

    return {
        "narrative":        narrative,           # full model output
        "summary":          first_sentence,      # first sentence → shown as summary in Slack
        "root_cause":       narrative,            # full narrative → shown as root cause
        "suggested_action": suggested,           # last sentence → shown as action
    }


def analyze_issue(issue: dict) -> dict:
    import time

    prompt = build_prompt(
        title                = issue["title"],
        body                 = issue["body"],
        labels               = issue.get("labels", []),
        days_open            = issue.get("days_open", 0),
        assignee_count       = issue.get("assignee_count", 0),
        comment_count        = issue.get("comment_count", 0),
        linked_pr_count      = issue.get("linked_pr_count", 0),
        pr_states            = issue.get("pr_states", ["none"]),
        ci_status            = issue.get("ci_status", "none"),
        max_comment_gap_days = issue.get("max_comment_gap_days", 0.0),
        comments_text        = issue.get("comments_text", ""),
        silent_reviewers     = issue.get("silent_reviewers", 0),
        pr_review_feedback   = issue.get("pr_review_feedback", ""),
        risk_score           = issue.get("confidence_score"),   # from sentinel
        risk_band            = issue.get("predicted_class"),    # from sentinel
    )

    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 200,
            "temperature": 0.3,
            "return_full_text": False
        }
    }

    # Retry up to 3 times on 503 — HF endpoint may be scaling up from zero
    max_retries = 3
    retry_wait  = 30  # seconds between retries

    for attempt in range(1, max_retries + 1):
        response = requests.post(
            REASONER_ENDPOINT,
            headers=HEADERS,
            json=payload,
            timeout=120
        )

        if response.status_code == 200:
            break

        if response.status_code == 503 and attempt < max_retries:
            print(f"    ⏳ Reasoner 503 (scaling up) — retrying in {retry_wait}s... (attempt {attempt}/{max_retries})")
            time.sleep(retry_wait)
            continue

        raise Exception(f"Endpoint error: {response.status_code} — {response.text[:200]}")

    result    = response.json()
    generated = result[0]["generated_text"]
    analysis  = parse_analysis(generated)

    return {
        **issue,
        "analysis":      analysis,
        "raw_reasoning": generated.strip()
    }


def run_reasoner(high_issues: list[dict]) -> list[dict]:
    if not high_issues:
        print("Reasoner: no HIGH issues to analyze.")
        return []

    print(f"\nReasoner analyzing {len(high_issues)} HIGH severity issues via HF Endpoint...")
    print("-" * 50)

    analyzed = []

    for i, issue in enumerate(high_issues):
        num   = issue["issue_number"]
        title = issue["title"][:60]
        print(f"  [{i+1}/{len(high_issues)}] #{num}: {title}...")

        try:
            result = analyze_issue(issue)
            a      = result["analysis"]
            print(f"    SUMMARY:    {a['summary'][:80]}")
            print(f"    ROOT CAUSE: {a['root_cause'][:80]}")
            print(f"    ACTION:     {a['suggested_action'][:80]}")
            analyzed.append(result)

        except Exception as e:
            print(f"    ❌ Reasoner failed for #{num}: {e}")
            analyzed.append({
                **issue,
                "analysis": {
                    "summary":          issue["title"],
                    "root_cause":       "Analysis failed.",
                    "suggested_action": "Review manually."
                },
                "raw_reasoning": ""
            })

    print("-" * 50)
    print(f"Reasoner done: {len(analyzed)} issues analyzed")
    return analyzed


if __name__ == "__main__":
    test_high_issues = [
        {
            "issue_number": 99901,
            "title": "Scheduler crashes silently when DAG has 500+ task instances",
            "body": "When a DAG runs with more than 500 task instances the scheduler drops tasks without any error logs.",
            "url": "https://github.com/apache/airflow/issues/99901",
            "created_at": "2026-05-04T10:00:00Z",
            "labels": ["bug", "scheduler"],
            "predicted_class": "high",
            "is_high_severity": True,
            "raw_output": "high"
        }
    ]

    analyzed = run_reasoner(test_high_issues)
    print(f"\n{'='*50}")
    for issue in analyzed:
        print(f"#{issue['issue_number']}: {issue['title'][:60]}")
        print(f"  Summary:    {issue['analysis']['summary'][:100]}")
        print(f"  Root Cause: {issue['analysis']['root_cause'][:100]}")
        print(f"  Action:     {issue['analysis']['suggested_action'][:100]}")
    print("✅ Phase 7 Reasoner test complete")
