import requests
import time
from datetime import datetime
from config import GITHUB_TOKEN, GITHUB_REPO, POLL_INTERVAL_SECONDS
from cache import init_cache, is_seen, mark_seen, get_seen_count

GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/issues"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

def _compute_days_open(created_at: str) -> int:
    """Compute how many days an issue has been open since creation."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(created.tzinfo)
        return (now - created).days
    except Exception:
        return 0


def _fetch_pr_states_with_numbers(issue_number: int) -> tuple[list[str], list[int]]:
    """
    Fetch states and numbers of PRs linked to this issue via timeline API.
    Returns (pr_states, pr_numbers).
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/timeline"
    headers_tl = {**HEADERS, "Accept": "application/vnd.github.mockingbird-preview+json"}
    try:
        resp = requests.get(url, headers=headers_tl, timeout=10)
        if resp.status_code != 200:
            return ["none"], []
        events = resp.json()
        pr_states, pr_numbers = [], []
        for evt in events:
            if evt.get("event") == "cross-referenced":
                source = evt.get("source", {})
                issue_ref = source.get("issue", {})
                if "pull_request" in issue_ref:
                    pr_states.append(issue_ref.get("state", "unknown"))
                    pr_numbers.append(issue_ref.get("number", 0))
        return (pr_states or ["none"]), pr_numbers
    except Exception:
        return ["none"], []


def _fetch_issue_comments(issue_number: int) -> tuple[str, float]:
    """
    Fetch issue comments. Returns:
      - formatted comment string matching training format
      - max gap in days between consecutive comments (MAX_COMMENT_GAP_DAYS)
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue_number}/comments"
    try:
        resp = requests.get(url, headers=HEADERS, params={"per_page": 10}, timeout=10)
        if resp.status_code != 200:
            return "", 0.0
        comments = resp.json()
        if not comments:
            return "", 0.0

        lines = []
        timestamps = []
        for c in comments[:10]:  # cap at 10 comments
            author = c.get("user", {}).get("login", "unknown")
            created = c.get("created_at", "")
            body = (c.get("body") or "")[:200].replace("\n", " ")
            lines.append(f"- [{created}] {author}: {body}...")
            try:
                timestamps.append(datetime.fromisoformat(created.replace("Z", "+00:00")))
            except Exception:
                pass

        # Compute max gap between consecutive comments
        max_gap = 0.0
        if len(timestamps) > 1:
            timestamps.sort()
            gaps = [(timestamps[i+1] - timestamps[i]).total_seconds() / 86400
                    for i in range(len(timestamps) - 1)]
            max_gap = round(max(gaps), 1)

        return "\n".join(lines), max_gap
    except Exception:
        return "", 0.0


def _fetch_pr_review_feedback(pr_numbers: list[int]) -> tuple[int, str]:
    """
    Fetch PR review activity for linked PRs. Returns:
      - silent_reviewers: count of reviewers who commented but did not approve
      - feedback_text: formatted review feedback matching training format
    """
    if not pr_numbers:
        return 0, ""

    feedback_lines = []
    all_reviewers = set()
    approvers = set()

    for pr_num in pr_numbers[:3]:  # cap at 3 PRs
        # Fetch reviews
        url = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_num}/reviews"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                for review in resp.json()[:10]:
                    reviewer = review.get("user", {}).get("login", "unknown")
                    state = review.get("state", "")  # APPROVED, CHANGES_REQUESTED, COMMENTED
                    body = (review.get("body") or "")[:200].replace("\n", " ")
                    all_reviewers.add(reviewer)
                    if state == "APPROVED":
                        approvers.add(reviewer)
                    if body:
                        feedback_lines.append(
                            f"[PR #{pr_num} {state} by {reviewer}]: {body}..."
                        )
        except Exception:
            pass

        # Fetch inline review comments (top 5)
        url2 = f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_num}/comments"
        try:
            resp2 = requests.get(url2, headers=HEADERS, params={"per_page": 5}, timeout=10)
            if resp2.status_code == 200:
                for comment in resp2.json()[:5]:
                    reviewer = comment.get("user", {}).get("login", "unknown")
                    body = (comment.get("body") or "")[:150].replace("\n", " ")
                    path = comment.get("path", "")
                    all_reviewers.add(reviewer)
                    if body:
                        feedback_lines.append(
                            f"[{reviewer} on {path}]: {body}..."
                        )
        except Exception:
            pass

    silent = len(all_reviewers - approvers)
    feedback_text = "\n".join(feedback_lines[:10]) if feedback_lines else ""
    return silent, feedback_text


def fetch_open_issues(max_pages: int = 1) -> list[dict]:
    """
    Fetch open issues from GitHub API with rich metadata matching scorer training format.
    Fields collected: title, body, labels, assignees, days_open, comment_count,
                      linked_pr_count, pr_states, ci_status.

    NOTE: max_pages=1, per_page=5 for testing. Set max_pages=3, per_page=30 for production.
    """
    issues = []

    import random

    # Dual-Snapshot Strategy:
    # 1. Always fetch page 1 (the 30 most recently updated issues) to catch live activity.
    # 2. Fetch a random page (between 2 and 50) to continuously audit the deep backlog.
    pages_to_fetch = [1, random.randint(2, 50)]

    for page_num in pages_to_fetch:
        params = {
            "state": "open",
            "per_page": 30,
            "page": page_num,
            "sort": "updated",
            "direction": "desc"
        }

        response = requests.get(GITHUB_API_URL, headers=HEADERS, params=params)

        if response.status_code == 403:
            print("❌ GitHub rate limit hit. Check your token.")
            break
        elif response.status_code != 200:
            print(f"❌ GitHub API error: {response.status_code} — {response.text[:200]}")
            break

        page_data = response.json()

        if not page_data:
            break  # no more pages

        for item in page_data:
            # Skip pull requests — GitHub returns them mixed with issues
            if "pull_request" in item:
                continue

            issue_number   = item["number"]
            created_at     = item["created_at"]
            days_open      = _compute_days_open(created_at)
            comment_count  = item.get("comments", 0)
            assignee_count = len(item.get("assignees", []))

            # Fetch linked PR states + numbers (1 API call)
            pr_states, pr_numbers = _fetch_pr_states_with_numbers(issue_number)
            linked_pr_count = len([s for s in pr_states if s != "none"])

            # Fetch issue comments text + max gap (1 API call)
            comments_text, max_comment_gap_days = _fetch_issue_comments(issue_number)

            # Fetch PR review feedback + silent reviewers (1-2 API calls per PR)
            silent_reviewers, pr_review_feedback = _fetch_pr_review_feedback(pr_numbers)

            issues.append({
                # Core fields
                "issue_number":         issue_number,
                "title":                item["title"],
                "body":                 item.get("body") or "",
                "url":                  item["html_url"],
                "created_at":           created_at,
                "labels":               [lb["name"] for lb in item.get("labels", [])],
                # Rich metadata for scorer and reasoner (matches training format)
                "days_open":            days_open,
                "comment_count":        comment_count,
                "assignee_count":       assignee_count,
                "linked_pr_count":      linked_pr_count,
                "pr_states":            pr_states,
                "pr_numbers":           pr_numbers,
                "ci_status":            "none",   # placeholder — requires extra PR head SHA lookup
                "max_comment_gap_days": max_comment_gap_days,
                "comments_text":        comments_text,
                "silent_reviewers":     silent_reviewers,
                "pr_review_feedback":   pr_review_feedback,
            })

        print(f"  Page {page_num}: fetched {len(page_data)} items")

    return issues

def filter_new_issues(issues: list[dict]) -> list[dict]:
    """Remove issues we've already processed."""
    new_issues = []
    for issue in issues:
        if not is_seen(issue["issue_number"]):
            new_issues.append(issue)
    return new_issues

def poll_once() -> list[dict]:
    """
    Run one poll cycle.
    Returns list of new unseen issues ready for scoring.
    """
    print(f"\n{'='*50}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Polling GitHub...")
    print(f"Repo: {GITHUB_REPO}")
    print(f"Total issues seen so far: {get_seen_count()}")

    # Fetch from GitHub
    all_issues = fetch_open_issues()
    print(f"Fetched {len(all_issues)} open issues (excl. PRs)")

    # Return ALL fetched issues without filtering them by the seen cache
    # so they get continuously scored every 5 minutes.
    if all_issues:
        for issue in all_issues:
            print(f"  → #{issue['issue_number']}: {issue['title'][:70]}")
    else:
        print("  No new issues this cycle.")

    return all_issues

def run_poller(scoring_callback=None):
    """
    Main polling loop — runs every POLL_INTERVAL_SECONDS.
    scoring_callback: function to call with new issues list.
                      If None, just prints them (for testing).
    """
    init_cache()
    print(f"Poller started. Interval: {POLL_INTERVAL_SECONDS}s ({POLL_INTERVAL_SECONDS//60} min)")
    print(f"Watching: {GITHUB_REPO}")

    while True:
        try:
            new_issues = poll_once()

            if new_issues:
                if scoring_callback:
                    # Hand off to Sentinel (Phase 2)
                    scoring_callback(new_issues)
                else:
                    # Phase 1 test mode — just mark as seen
                    print("\n[TEST MODE] Marking as seen without scoring...")
                    for issue in new_issues:
                        mark_seen(issue["issue_number"], issue["title"])
                        print(f"  Marked #{issue['issue_number']} as seen")

            print(f"\nSleeping {POLL_INTERVAL_SECONDS//60} min until next poll...")
            time.sleep(POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\nPoller stopped.")
            break
        except Exception as e:
            print(f"❌ Poller error: {e}")
            print("Retrying in 60s...")
            time.sleep(60)

if __name__ == "__main__":
    from sentinel import run_sentinel
    from reasoner import run_reasoner
    from notifier import run_notifier

    def scoring_callback(new_issues):
        high_issues = run_sentinel(new_issues)
        if not high_issues:
            print("No HIGH severity issues this cycle.")
            return
        analyzed = run_reasoner(high_issues)
        run_notifier(analyzed)

    run_poller(scoring_callback=scoring_callback)
