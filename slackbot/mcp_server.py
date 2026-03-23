import os
import json
import requests
from mcp.server.fastmcp import FastMCP
from google.cloud import aiplatform

# --- Configuration & Setup ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "autobot-demo-2")
GCP_LOCATION   = os.environ.get("GCP_LOCATION", "us-west1")

SCORER_ENDPOINT_ID = os.environ.get("SCORER_ENDPOINT_ID", "YOUR_SCORER_ENDPOINT_ID")
REASONER_ENDPOINT_ID = os.environ.get("REASONER_ENDPOINT_ID", "YOUR_REASONER_ENDPOINT_ID")

SYSTEM_PROMPT_SCORER = (
    "You are a bottleneck risk scorer for GitHub issues. "
    "Given an issue snapshot, output a single float 0.0-1.0 representing the bottleneck risk score.\n"
    "Output the float score only, nothing else.\n"
    "PROJECT: apache/airflow | P50=1d P75=5d P90=22d P95=44d"
)

SYSTEM_PROMPT_REASONER = (
    "You are a bottleneck analyst for GitHub issues. Given an issue snapshot and its risk score, "
    "write a 2-3 sentence explanation for a non-technical scrum master. Reference specific signals. No bullet points."
)

# Initialize GCP Vertex AI
if GCP_PROJECT_ID != "YOUR_GCP_PROJECT_ID":
    try:
        aiplatform.init(project=GCP_PROJECT_ID, location=GCP_LOCATION)
    except Exception as e:
        print(f"Failed to initialize Vertex AI: {e}")

# Initialize FastMCP Server
port = int(os.environ.get("PORT", 8080))
mcp = FastMCP("AutoBot-Slack-Orchestrator", host="0.0.0.0", port=port)


def fetch_open_issues_from_github(repo: str = "apache/airflow", limit: int = 50) -> list[dict]:
    """Fetch recent open issues directly from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/issues"
    params = {
        "state": "open",
        "per_page": limit,
        "sort": "created",
        "direction": "desc"
    }
    headers = {}
    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"
        
    print(f"Fetching {limit} open issues from {repo}...")
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"GitHub API fetch error: {e}")
        return []


def generate_issue_snapshot_text(item: dict) -> str:
    """Format a GitHub API issue item into a snapshot string."""
    title = item.get("title") or "N/A"
    body = str(item.get("body") or "")[:500]
    labels_list = [l.get("name", "") for l in item.get("labels", []) if isinstance(l, dict)]
    labels = ", ".join(labels_list)
    comments_count = item.get("comments", 0)
    
    return (
        f"TITLE: {title}\n"
        f"LABELS: {labels}\n"
        f"COMMENTS: {comments_count}\n"
        f"BODY: {body}\n"
    )

def predict_scorer_vertex(snapshot_text: str) -> float:
    """Invokes the Vertex AI Scorer endpoint."""
    if SCORER_ENDPOINT_ID == "YOUR_SCORER_ENDPOINT_ID":
        print("Scorer endpoint not configured. Returning 0.0")
        return 0.0
        
    # Construct exact text prompt for the model
    user_prompt = f"{SYSTEM_PROMPT_SCORER}\n\n{snapshot_text}"
    
    endpoint_name = f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/endpoints/{SCORER_ENDPOINT_ID}"
    endpoint = aiplatform.Endpoint(endpoint_name=endpoint_name)
    
    try:
        prediction = endpoint.predict(instances=[{"prompt": user_prompt, "max_tokens": 10}])
        if prediction.predictions:
            raw_output = str(prediction.predictions[0]).strip()
            # Attempt to parse float safely 
            import re
            match = re.search(r"0\.\d+|1\.0", raw_output)
            if match:
                return float(match.group())
            return float(raw_output)
    except Exception as e:
        print(f"Scorer Vertex API Error: {e}")
    return 0.0


def predict_reasoner_vertex(snapshot_text: str, score: float) -> str:
    """Invokes the Vertex AI Reasoner endpoint."""
    if REASONER_ENDPOINT_ID == "YOUR_REASONER_ENDPOINT_ID":
        return "Reasoner endpoint not configured."
        
    user_prompt = f"{SYSTEM_PROMPT_REASONER}\n\nRISK_SCORE: {score:.2f} | PROJECT: apache/airflow | P50=1d P75=5d P90=22d P95=44d\n{snapshot_text}"
    
    endpoint_name = f"projects/{GCP_PROJECT_ID}/locations/{GCP_LOCATION}/endpoints/{REASONER_ENDPOINT_ID}"
    endpoint = aiplatform.Endpoint(endpoint_name=endpoint_name)
    
    try:
        prediction = endpoint.predict(instances=[{"prompt": user_prompt, "max_tokens": 256}])
        if prediction.predictions:
            return str(prediction.predictions[0]).strip()
    except Exception as e:
        print(f"Reasoner Vertex API Error: {e}")
    return "Failed to generate reasoning."


@mcp.tool()
def get_top_risk_items(top_n: int = 5) -> str:
    """
    Retrieves all open issues/PRs from GitHub, scores them using the Vertex AI 
    Bottleneck Scorer model, and generates a narrative for the top risky items 
    using the Vertex AI Reasoner model.
    """
    if GCP_PROJECT_ID == "YOUR_GCP_PROJECT_ID":
        return "GCP_PROJECT_ID is not configured. Please set Vertex AI credentials."
        
    print("Fetching open issues from GitHub...")
    issues = fetch_open_issues_from_github(limit=50)
    
    if not issues:
        return "No open issues found or GitHub API failed."
        
    print(f"Scoring {len(issues)} issues via Vertex AI Scorer...")
    scored_issues = []
    
    for item in issues:
        issue_number = item.get("number", "Unknown")
        snapshot_text = generate_issue_snapshot_text(item)
        
        # 1. Get Score
        risk_score = predict_scorer_vertex(snapshot_text)
        
        scored_issues.append({
            "issue_number": issue_number,
            "title": item.get("title", "N/A"),
            "risk_score": risk_score,
            "snapshot_text": snapshot_text
        })

    # Sort descending by risk score
    scored_issues.sort(key=lambda x: x["risk_score"], reverse=True)
    top_issues = scored_issues[:top_n]
    
    # 2. Get Reasoning for the top issues
    print("Generating narratives via Vertex AI Reasoner...")
    response_lines = [f"🔍 *Top {len(top_issues)} Risk Items*"]
    for idx, item in enumerate(top_issues, 1):
        # Generate narrative reason. Per docs, Reasoner runs on high-scoring items.
        reasoning = "Score too low to require reasoning."
        if item["risk_score"] >= 0.65:
            reasoning = predict_reasoner_vertex(item["snapshot_text"], item["risk_score"])
            
        band = "HIGH" if item["risk_score"] >= 0.65 else ("MEDIUM" if item["risk_score"] >= 0.35 else "LOW")
        
        response_lines.append(
            f"{idx}. *Issue #{item['issue_number']}* - {item['title']}\n"
            f"   Risk Band: `{band}` | Score: `{item['risk_score']:.2f}`\n"
            f"   *Narrative:* {reasoning}\n"
        )
        
    return "\n".join(response_lines)


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        print(f"Starting MCP Server auto-discovery over SSE on 0.0.0.0:{port}... (Exposing get_top_risk_items)")
        mcp.run(transport='sse')
    else:
        print(f"Starting MCP Server auto-discovery over stdio... (Exposing get_top_risk_items)")
        mcp.run()
