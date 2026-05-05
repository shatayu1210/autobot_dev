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

def fetch_open_issues(max_pages: int = 3) -> list[dict]:
    """
    Fetch open issues from GitHub API.
    Returns list of {issue_number, title, body} dicts.
    Skips pull requests (GitHub API returns PRs as issues too).
    """
    issues = []

    for page in range(1, max_pages + 1):
        params = {
            "state": "open",
            "per_page": 30,
            "page": page,
            "sort": "created",
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

            issues.append({
                "issue_number": item["number"],
                "title": item["title"],
                "body": item.get("body") or "",  # body can be None
                "url": item["html_url"],
                "created_at": item["created_at"],
                "labels": [l["name"] for l in item.get("labels", [])]
            })

        print(f"  Page {page}: fetched {len(page_data)} items")

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

    # Filter already-seen
    new_issues = filter_new_issues(all_issues)
    print(f"New unseen issues: {len(new_issues)}")

    if new_issues:
        for issue in new_issues:
            print(f"  → #{issue['issue_number']}: {issue['title'][:70]}")
    else:
        print("  No new issues this cycle.")

    return new_issues

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
