import os
import json
import httpx
from google.adk.agents import Agent
from google import genai

# --- Configuration ---
CODER_ENDPOINT_ID = os.environ.get("CODER_ENDPOINT_ID", "")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8080")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

VERTEX_PREDICT_URL = (
    f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}"
    f"/locations/{GCP_REGION}/endpoints/{CODER_ENDPOINT_ID}:rawPredict"
)

MODEL = "gemini-3-flash-preview"
FALLBACK_MODEL = "gemini-2.5-flash-preview-04-17"


# --- Fallback ---

async def _call_gemini_fallback(prompt: str, max_tokens: int = 2048) -> str:
    """Fallback: calls Gemini when the Vertex AI vLLM endpoint is unavailable."""
    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=FALLBACK_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.2,
            ),
        )
        return response.text
    except Exception as fallback_err:
        return (
            f"Error: Both primary endpoint and Gemini fallback failed. "
            f"Fallback error: {str(fallback_err)}"
        )


# --- Tools ---

def fetch_issue(owner: str, repo: str, issue_number: int) -> dict:
    """Fetches issue details directly from the GitHub REST API.

    Args:
        owner: The GitHub repository owner.
        repo: The GitHub repository name.
        issue_number: The issue number to fetch.

    Returns:
        A dictionary containing the issue title, body, labels, comments, and timeline.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    base = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        issue_resp = httpx.get(
            f"{base}/issues/{issue_number}", headers=headers, timeout=30
        )
        issue_resp.raise_for_status()
        issue_data = issue_resp.json()

        comments_resp = httpx.get(
            f"{base}/issues/{issue_number}/comments", headers=headers, timeout=30
        )
        comments = comments_resp.json() if comments_resp.status_code == 200 else []

        timeline_resp = httpx.get(
            f"{base}/issues/{issue_number}/timeline",
            headers={**headers, "Accept": "application/vnd.github.mockingbird-preview+json"},
            timeout=30,
        )
        timeline = timeline_resp.json() if timeline_resp.status_code == 200 else []

        return {
            "number": issue_data.get("number"),
            "title": issue_data.get("title", ""),
            "body": issue_data.get("body", ""),
            "labels": [l.get("name", "") for l in issue_data.get("labels", [])],
            "state": issue_data.get("state", ""),
            "comments": [
                {"author": c.get("user", {}).get("login", ""), "body": c.get("body", "")}
                for c in comments
            ],
            "timeline_events": [
                {"event": e.get("event", ""), "actor": e.get("actor", {}).get("login", "")}
                for e in timeline
            ],
        }
    except Exception as e:
        return {"error": f"Failed to fetch issue: {str(e)}"}


def build_repo_index(repo_path: str) -> dict:
    """Builds a Tree-sitter index of the repository for code navigation.

    Parses Python files in the repository using Tree-sitter to extract
    function and class definitions with their file paths and line ranges.

    Args:
        repo_path: Absolute path to the locally cloned repository root.

    Returns:
        A dictionary mapping file paths to lists of symbol definitions
        (functions, classes) with their line ranges.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["python", "build_treesitter_index.py", repo_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            index_path = os.path.join(repo_path, "treesitter_index.json")
            if os.path.exists(index_path):
                with open(index_path, "r") as f:
                    return json.load(f)
        return {"error": f"Index build failed: {result.stderr[:500]}"}
    except Exception as e:
        return {"error": f"Failed to build repo index: {str(e)}"}


def get_code_spans(issue_text: str, repo_index: dict) -> str:
    """Retrieves relevant code spans from the repo index using keyword heuristics.

    Given the issue text (title + body), searches the Tree-sitter index for
    files and functions that are likely relevant based on keyword matching.

    Args:
        issue_text: Combined issue title and body text for keyword extraction.
        repo_index: The Tree-sitter index dictionary from build_repo_index.

    Returns:
        A formatted string of relevant code spans with file paths and line ranges.
    """
    if not repo_index or "error" in repo_index:
        return "No repo index available."

    # Simple keyword extraction from issue text
    keywords = set()
    for word in issue_text.lower().split():
        cleaned = word.strip(".,;:!?()[]{}\"'`#")
        if len(cleaned) > 3 and cleaned.isalpha():
            keywords.add(cleaned)

    relevant_spans = []
    for file_path, symbols in repo_index.items():
        file_name = os.path.basename(file_path).lower()
        # Check if any keyword matches the file name or symbol names
        for symbol in symbols:
            symbol_name = symbol.get("name", "").lower()
            if any(kw in file_name or kw in symbol_name for kw in keywords):
                relevant_spans.append({
                    "file": file_path,
                    "symbol": symbol.get("name"),
                    "type": symbol.get("type"),
                    "start_line": symbol.get("start_line"),
                    "end_line": symbol.get("end_line"),
                })

    if not relevant_spans:
        return "No relevant code spans found for the given issue."

    # Format top 20 most relevant spans
    output_lines = ["# Relevant Code Spans\n"]
    for span in relevant_spans[:20]:
        output_lines.append(
            f"- **{span['file']}**: `{span['type']} {span['symbol']}` "
            f"(lines {span['start_line']}-{span['end_line']})"
        )
    return "\n".join(output_lines)


async def call_planner_endpoint(prompt: str) -> str:
    """Calls the Coder Vertex AI endpoint with the planner_lora adapter.

    Sends the planning prompt to the vLLM-served Qwen2.5-Coder-7B-Instruct
    model with the planner LoRA adapter for structured plan generation.

    Args:
        prompt: The formatted planning prompt including issue data and code spans.

    Returns:
        The raw model output string containing the structured plan.
    """
    import google.auth
    import google.auth.transport.requests

    try:
        credentials, _ = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)

        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": "planner_lora",
            "prompt": prompt,
            "max_tokens": 2048,
            "temperature": 0.2,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                VERTEX_PREDICT_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()

            # vLLM returns completions in choices[0].text
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("text", "")
            return json.dumps(result)
    except Exception as e:
        print(f"[Planner] Primary endpoint failed: {e}. Falling back to Gemini.")
        return await _call_gemini_fallback(prompt, max_tokens=2048)


# --- Planner Agent ---
planner = Agent(
    name="planner",
    model=MODEL,
    description="Analyzes GitHub issues and generates structured patch plans using the Planner model.",
    instruction="""
    You are the Planner agent in the AutoBot VS Code pipeline.
    Your job is to analyze a GitHub issue and produce a structured patch plan.

    **Workflow:**
    1. Use `fetch_issue` to retrieve the full issue details (title, body, labels, comments, timeline).
    2. Use `build_repo_index` to create a Tree-sitter index of the target repository.
    3. Use `get_code_spans` with the issue text and repo index to find relevant code locations.
    4. Use `call_planner_endpoint` with a formatted prompt to generate the structured plan.

    **Planning Prompt Format (for call_planner_endpoint):**
    Construct the prompt as:
    ```
    ISSUE_TITLE: <title>
    ISSUE_BODY: <body>
    ISSUE_LABELS: <labels>
    DISCUSSION: <comments summary>

    RELEVANT_CODE_SPANS:
    <code spans from get_code_spans>

    TASK: Generate a structured patch plan. For each file that needs changes:
    - File path (must exist in the repo index)
    - Function/class to modify
    - Description of the change
    - Whether it's an ADD, MODIFY, or DELETE operation

    REQUIRES_CODE_CHANGE: YES/NO
    ```

    **Output Format:**
    Return a structured plan in markdown with:
    - `REQUIRES_CODE_CHANGE: YES` or `NO` as the first line
    - If YES: a numbered list of file changes with paths, symbols, and descriptions
    - If NO: a summary explaining why no code change is needed

    If you receive feedback from the critic that your plan needs revision, refine your plan accordingly.
    Always validate that file paths in your plan exist in the repo index.
    """,
    tools=[fetch_issue, build_repo_index, get_code_spans, call_planner_endpoint],
)

root_agent = planner
