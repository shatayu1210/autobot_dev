import os
import httpx
from google import genai


def fetch_github_issue(owner: str, repo: str, issue_number: int, github_token: str = "") -> dict:
    """Fetch a GitHub issue and its comments via REST API."""
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    base_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"

    with httpx.Client(timeout=30.0) as client:
        issue_resp = client.get(base_url, headers=headers)
        issue_resp.raise_for_status()
        issue = issue_resp.json()

        comments = []
        try:
            comments_resp = client.get(f"{base_url}/comments", headers=headers)
            comments_resp.raise_for_status()
            raw_comments = comments_resp.json()
            comments = [c.get("body", "") for c in raw_comments if c.get("body")]
        except Exception:
            pass

    labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]

    return {
        "title": issue.get("title", ""),
        "body": issue.get("body", "") or "",
        "labels": labels,
        "comments": comments,
        "number": issue_number,
        "url": issue.get("html_url", ""),
    }


def call_llm(
    prompt: str,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    """Call Gemini via google-genai client. Uses ADC or GEMINI_API_KEY."""
    client = genai.Client()

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

    if system_prompt:
        contents = [{"role": "user", "parts": [{"text": system_prompt + "\n\n" + prompt}]}]
    else:
        contents = prompt

    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=genai.types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    return response.text


def validate_diff(diff_text: str) -> str:
    """Validate that diff_text looks like a proper unified diff.
    Returns 'VALID' or an error description string."""
    if not diff_text or not diff_text.strip():
        return "ERROR: diff is empty"

    lines = diff_text.strip().splitlines()

    has_minus_header = any(line.startswith("--- ") for line in lines)
    has_plus_header = any(line.startswith("+++ ") for line in lines)
    has_hunk = any(line.startswith("@@ ") for line in lines)

    if not has_minus_header:
        return "ERROR: missing '--- ' file header"
    if not has_plus_header:
        return "ERROR: missing '+++ ' file header"
    if not has_hunk:
        return "ERROR: missing '@@ ' hunk header"

    return "VALID"
