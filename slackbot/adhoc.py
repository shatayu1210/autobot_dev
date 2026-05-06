"""
slackbot/adhoc.py

Phase 6 — Adhoc query handler for Slack @mention events.

Replicates the VS Code orchestrator's orch → adhoc (GitHub APIs + GraphRAG) path.
Called by slack_orchestrator.py when a user @mentions AutoBot.

Architecture:
  User query → Guardrail → Classifier → Tool Planner (OpenAI)
             → Tool Executor (GitHub REST + GraphRAG)
             → Summarizer (OpenAI)
             → Human-readable Slack reply

GraphRAG tools are loaded only if Neo4j is reachable.
If Neo4j is offline, adhoc falls back to live GitHub API tools only.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import openai
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)

from config import HF_TOKEN

# ── OpenAI client (pointing to HF Inference Endpoint) ─────────────────────────
_openai_client = openai.OpenAI(
    base_url="https://bl8fcekrofz3h9qc.us-east-1.aws.endpoints.huggingface.cloud/v1/",
    api_key=HF_TOKEN or "hf_dummy"
)

ADHOC_MODEL = "tgi"  # HF TGI endpoints accept any model string


def _chat(system: str, user: str) -> str:
    """Simple synchronous OpenAI chat call."""
    resp = _openai_client.chat.completions.create(
        model=ADHOC_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.0,
        max_tokens=1024,
    )
    return resp.choices[0].message.content.strip()


# ── GitHub REST helpers ────────────────────────────────────────────────────────

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "apache")
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO_NAME", "airflow")


def _github_get(path: str) -> dict | list | None:
    """Authenticated GET against the GitHub REST API. Returns parsed JSON or None."""
    if not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO_NAME}/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return None


def gh_get_issue(issue_number: int) -> dict | None:
    """Get a single issue by number."""
    data = _github_get(f"issues/{issue_number}")
    if not data:
        return None
    return {
        "issue_number":  data.get("number"),
        "title":         data.get("title", ""),
        "body":          (data.get("body") or "")[:1000],
        "state":         data.get("state", ""),
        "created_at":    data.get("created_at", ""),
        "closed_at":     data.get("closed_at"),
        "html_url":      data.get("html_url", ""),
        "labels":        [lb["name"] for lb in (data.get("labels") or [])],
        "assignee":      (data.get("assignee") or {}).get("login", "nobody"),
        "comments":      data.get("comments", 0),
    }


def gh_get_issue_comments(issue_number: int) -> list | None:
    """Get comments on an issue (top 5 for brevity)."""
    data = _github_get(f"issues/{issue_number}/comments")
    if not data:
        return None
    return [
        {
            "author":     c.get("user", {}).get("login", "unknown"),
            "created_at": c.get("created_at", ""),
            "body":       (c.get("body") or "")[:300],
        }
        for c in data[:5]
    ]


def gh_get_pr(pr_number: int) -> dict | None:
    """Get a single pull request by number."""
    data = _github_get(f"pulls/{pr_number}")
    if not data:
        return None
    return {
        "pr_number":  data.get("number"),
        "title":      data.get("title", ""),
        "state":      data.get("state", ""),
        "merged":     data.get("merged", False),
        "merged_at":  data.get("merged_at"),
        "html_url":   data.get("html_url", ""),
        "author":     (data.get("user") or {}).get("login", "unknown"),
        "merged_by":  ((data.get("merged_by") or {}).get("login") or "nobody"),
        "changed_files": data.get("changed_files", 0),
    }


def gh_get_pr_files(pr_number: int) -> list | None:
    """Get files changed in a pull request."""
    data = _github_get(f"pulls/{pr_number}/files")
    if not data:
        return None
    return [
        {"filename": f.get("filename", ""), "status": f.get("status", "")}
        for f in data[:20]
    ]


def gh_search_issues(query: str, max_results: int = 8) -> list | None:
    """Search issues/PRs using GitHub search syntax."""
    if not GITHUB_TOKEN:
        return None
    q = f"repo:{GITHUB_OWNER}/{GITHUB_REPO_NAME} {query}"
    url = (
        "https://api.github.com/search/issues"
        f"?q={urllib.parse.quote(q)}&per_page={max_results}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])[:max_results]
            return [
                {
                    "number":     i.get("number"),
                    "title":      i.get("title", ""),
                    "state":      i.get("state", ""),
                    "html_url":   i.get("html_url", ""),
                    "created_at": i.get("created_at", ""),
                }
                for i in items
            ]
    except urllib.error.HTTPError:
        return None


# ── GraphRAG tools (optional — degraded gracefully when Neo4j offline) ────────

def _load_graphrag_tools() -> dict:
    """Attempt to load GraphRAG tools. Returns empty dict if Neo4j is unavailable."""
    tools = {}
    try:
        import sys
        graphrag_path = os.path.join(os.path.dirname(__file__), "../autobot_vscode/local_orchestrator")
        if graphrag_path not in sys.path:
            sys.path.insert(0, graphrag_path)

        from graphrag_client import (  # type: ignore
            similar_issues,
            linked_prs_for_issues,
            neo4j_available,
        )

        if neo4j_available():
            print("[Adhoc] GraphRAG (Neo4j) is available ✅")
            tools["graphrag_similar_issues"] = (
                lambda issue_number, k=5: similar_issues(int(issue_number), int(k)),
                "Find top-K issues similar to a given issue using GraphRAG vector search. Args: issue_number (int), k (int, default 5)",
            )
            tools["graphrag_linked_prs"] = (
                lambda issue_numbers: linked_prs_for_issues([int(n) for n in issue_numbers]),
                "Find PRs linked to a list of issue numbers in the graph. Args: issue_numbers (list[int])",
            )
        else:
            print("[Adhoc] GraphRAG (Neo4j) offline — skipping GraphRAG tools")
    except ImportError:
        print("[Adhoc] graphrag_client not importable — GraphRAG tools disabled")
    return tools


# ── Unified tool registry ─────────────────────────────────────────────────────

GITHUB_TOOLS: dict[str, tuple[Any, str]] = {
    "get_issue": (
        lambda issue_number: gh_get_issue(int(issue_number)),
        "Get a GitHub issue by number. Args: issue_number (int)",
    ),
    "get_issue_comments": (
        lambda issue_number: gh_get_issue_comments(int(issue_number)),
        "Get comments on a GitHub issue. Args: issue_number (int)",
    ),
    "get_pr": (
        lambda pr_number: gh_get_pr(int(pr_number)),
        "Get a GitHub pull request by number. Args: pr_number (int)",
    ),
    "get_pr_files": (
        lambda pr_number: gh_get_pr_files(int(pr_number)),
        "Get files changed in a pull request. Args: pr_number (int)",
    ),
    "search_issues": (
        lambda query, max_results=8: gh_search_issues(str(query), int(max_results)),
        "Search GitHub issues/PRs by keyword. Args: query (str), max_results (int, default 8)",
    ),
}

# Load GraphRAG tools at module startup
GITHUB_TOOLS.update(_load_graphrag_tools())


# ── Query classifier ──────────────────────────────────────────────────────────

_GRAPHRAG_KEYWORDS = [
    "similar to", "like issue", "like #", "historical", "in the past",
    "past issues", "closed like", "resolved like", "related issues",
    "similar bugs", "same kind", "how long did it take",
    "average resolution", "files usually modified",
    "prs that fixed", "prs linked to", "historically",
]

_LIVE_ONLY_KEYWORDS = [
    "latest", "recent issues", "opened today", "opened this week",
    "status of pr", "who merged", "who closed",
    "current assignee", "what changed in pr", "files in pr",
]


def _classify_query(query: str) -> str:
    """Returns 'graphrag', 'live', or 'mixed'."""
    q = query.lower()
    if any(kw in q for kw in _GRAPHRAG_KEYWORDS):
        return "graphrag"
    if any(kw in q for kw in _LIVE_ONLY_KEYWORDS):
        return "live"
    return "mixed"


# ── Guardrail ─────────────────────────────────────────────────────────────────

_GUARDRAIL_SYSTEM = (
    "You are a security guardrail. Classify if the user's query is relevant to:\n"
    "1. GitHub issues, pull requests, commits, code reviews, or CI status.\n"
    "2. The Apache Airflow software repository.\n"
    "3. General software engineering tasks within the scope of this project.\n\n"
    "If relevant, output exactly: YES\n"
    "If NOT relevant (e.g. general chat, jokes, recipes, unrelated domains), output exactly: NO"
)


# ── JSON helper ───────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    fence = re.match(r"^```(?:json)?\s*\n([\s\S]*?)\n```\s*$", text)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON from: {text[:200]}")


# ── Main public function ───────────────────────────────────────────────────────

def handle_adhoc_query(query: str, issue_number: int = None) -> str:
    """
    Handle a Slack @mention adhoc query using GitHub APIs + optional GraphRAG.

    Flow:
      1. Guardrail — reject off-topic queries
      2. Classifier — route to graphrag / live / mixed tool set
      3. Tool Planner (OpenAI) — choose which tools to call
      4. Tool Executor — call GitHub REST APIs and/or Neo4j
      5. Summarizer (OpenAI) — synthesize into a human-readable Slack reply

    Args:
        query:        Clean user query (bot mention already stripped)
        issue_number: Optional issue number parsed from query (#NNNNN)

    Returns:
        str: Formatted response to post back to Slack
    """
    print(f"[Adhoc] Handling query: '{query}' | issue_number={issue_number}")

    # ── Step 1: Guardrail ─────────────────────────────────────────────────────
    try:
        relevance = _chat(_GUARDRAIL_SYSTEM, query).upper()
        if "NO" in relevance and "YES" not in relevance:
            print("[Adhoc] Guardrail blocked off-topic query.")
            return (
                "🤖 I'm *AutoBot*, here to help with Apache Airflow GitHub issues, PRs, "
                "and code review questions. I can't help with queries outside that scope!"
            )
    except Exception as e:
        print(f"[Adhoc] Guardrail error (proceeding): {e}")

    # ── Step 2: Classify + select active tools ────────────────────────────────
    route = _classify_query(query)
    print(f"[Adhoc] Query route: {route}")

    if route == "graphrag":
        active_tools = {k: v for k, v in GITHUB_TOOLS.items() if "graphrag" in k}
        if not active_tools:
            return (
                "🔍 You asked about historical or similar issues, which requires the "
                "GraphRAG database. It's currently offline. "
                "Start Neo4j (`cd graphrag && docker compose up -d`) to enable this feature.\n\n"
                "_In the meantime, try: `@AutoBot search for issues about scheduler crash`_"
            )
    elif route == "live":
        active_tools = {k: v for k, v in GITHUB_TOOLS.items() if "graphrag" not in k}
    else:
        active_tools = GITHUB_TOOLS

    # ── Step 3: Tool Planner ──────────────────────────────────────────────────
    tool_descriptions = "\n".join(
        f"- {name}: {desc}" for name, (_, desc) in active_tools.items()
    )
    plan_system = (
        f"You are a tool-calling planner for GitHub queries about {GITHUB_OWNER}/{GITHUB_REPO_NAME}.\n"
        "Available tools:\n"
        f"{tool_descriptions}\n\n"
        "Given the user's question, output a JSON object with a 'calls' array.\n"
        'Each call: {"tool": "<name>", "args": {"<param>": <value>}}.\n'
        "Max 4 calls. Use the minimum number of calls needed.\n"
        "Output JSON only — no commentary, no markdown fences."
    )

    try:
        raw_plan = _chat(plan_system, query)
        plan_data = _extract_json(raw_plan)
        calls = plan_data.get("calls", [])
        if not isinstance(calls, list):
            calls = []
        # Security: only allow tools in the active set
        calls = [c for c in calls if c.get("tool") in active_tools]
    except Exception as e:
        print(f"[Adhoc] Tool planner error: {e}")
        calls = []

    # ── Step 4: Tool Executor ─────────────────────────────────────────────────
    tool_results: list[str] = []
    tools_called: list[str] = []

    for call in calls:
        tool_name = call.get("tool")
        args = call.get("args", {})
        if tool_name not in active_tools:
            continue

        print(f"[Adhoc] Calling tool: {tool_name}({args})")
        fn, _ = active_tools[tool_name]

        try:
            result = fn(**args) if isinstance(args, dict) else fn(args)
            result_str = json.dumps(result, separators=(",", ":"), default=str)[:3000]
            tool_results.append(f"[{tool_name}] → {result_str}")
            tools_called.append(tool_name)
        except Exception as e:
            print(f"[Adhoc] Tool {tool_name} error: {e}")
            tool_results.append(f"[{tool_name}] → ERROR: {e}")
            tools_called.append(f"{tool_name}(error)")

    # ── Step 5: Summarizer ────────────────────────────────────────────────────
    summary_system = (
        f"You are AutoBot, a helpful GitHub assistant for the {GITHUB_OWNER}/{GITHUB_REPO_NAME} repository.\n"
        "Answer the user's question using ONLY the provided tool results. DO NOT output raw JSON.\n"
        "FORMATTING RULES:\n"
        "- Write in a conversational, natural language paragraph format.\n"
        "- DO NOT just dump a rigid list of bullet points like Title/State/Author.\n"
        "- Weave the facts (what the issue/PR is about, who authored it, its state) naturally into your sentences.\n"
        "- Convert ISO timestamps (e.g. 2024-06-02T03:49:11Z) to 'MM/DD/YY at HH:MM UTC' format.\n"
        "- Reference issues as #N (e.g. #66353) and PRs as PR #N.\n"
        "- Include the GitHub link naturally at the end.\n"
        "- If the tool results don't contain enough data to answer, say so clearly."
    )

    if tool_results:
        summary_user = (
            f"User question: {query}\n\n"
            f"Tool results:\n" + "\n\n".join(tool_results)
        )
    else:
        # No tools were called — answer directly from LLM knowledge
        summary_user = (
            f"User question: {query}\n\n"
            "No tool results available. Answer based on your knowledge of Apache Airflow, "
            "or explain that you need a specific issue/PR number to look up."
        )

    try:
        answer = _chat(summary_system, summary_user)
    except Exception as e:
        print(f"[Adhoc] Summarizer error: {e}")
        answer = f"⚠️ I encountered an error generating the response: {e}"

    # Add a subtle footer showing which tools were used
    if tools_called:
        clean_tools = [t for t in tools_called if "(error)" not in t]
        if clean_tools:
            answer += f"\n\n_Tools used: {', '.join(f'`{t}`' for t in clean_tools)}_"

    print(f"[Adhoc] Response ready ({len(answer)} chars). Tools: {tools_called}")
    return answer


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing adhoc query handler...\n")

    test_queries = [
        ("what is issue #66353?", 66353),
        ("search for issues about scheduler crash", None),
        ("what is the capital of France?", None),   # should be guardrailed
    ]

    for query, issue_num in test_queries:
        print(f"\n{'='*60}")
        print(f"Query: {query}")
        print(f"{'='*60}")
        response = handle_adhoc_query(query, issue_num)
        print(f"Response:\n{response}")
