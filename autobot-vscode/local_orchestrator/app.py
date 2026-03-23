"""
Local orchestrator for the AutoBot VS Code extension.

Modes (AUTOBOT_MODE):
  - google_ai → Google AI Studio API key (GOOGLE_API_KEY); uses ChatGoogleGenerativeAI. No Vertex project needed.
  - vertex    → Vertex AI GenerativeModel.generateContent (Gemini on Vertex; GCP + ADC).
  - ollama    → local Ollama.
  - stub      → canned JSON (no LLM).

Google AI Studio (API key): https://aistudio.google.com/apikey — set AUTOBOT_MODE=google_ai and GOOGLE_API_KEY.

Apache Airflow test (Vertex): clone repo, .env with GCP_* and AUTOBOT_MODE=vertex, gcloud auth application-default login.

POST /api/orchestrate  JSON: { "command": "ask_issue"|"plan_patch"|"accept_plan"|"open_pr", ... }
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

AUTOBOT_MODE = os.environ.get("AUTOBOT_MODE", "stub").lower()

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "").strip()
# Gemini on Vertex is most reliably available in us-central1; us-west1 often 404s for the same model ID.
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1").strip()
# Vertex: prefer names without "-001" for auto-updated versions (see inference docs).
# If you still get 404, try gemini-1.5-flash-002 and confirm region in Model Garden.
VERTEX_MODEL = os.environ.get("VERTEX_MODEL", "gemini-2.5-flash").strip()

# Google AI Studio / Gemini Developer API (API key — not Vertex)
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
# Google AI (API key): 2.0-flash is deprecated for new users; use 2.5+ (see Google error NOT_FOUND).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "apache").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "airflow").strip()

SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "build",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
}

_google_llm = None
_vertex_initialized = False


def get_google_ai_llm():
    global _google_llm
    if _google_llm is not None:
        return _google_llm
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set for google_ai mode")
    from langchain_google_genai import ChatGoogleGenerativeAI

    _google_llm = ChatGoogleGenerativeAI(
        model=GEMINI_MODEL,
        google_api_key=GOOGLE_API_KEY,
    )
    return _google_llm


def google_ai_chat(system: str, user: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = get_google_ai_llm()
    resp = llm.invoke(
        [SystemMessage(content=system), HumanMessage(content=user)])
    content = getattr(resp, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(resp)


def _ensure_vertex_init() -> None:
    """Vertex AI SDK init (same project/region as REST generateContent)."""
    global _vertex_initialized
    if _vertex_initialized:
        return
    if not GCP_PROJECT_ID:
        raise RuntimeError("GCP_PROJECT_ID is not set for vertex mode")
    import vertexai

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_LOCATION)
    _vertex_initialized = True


def vertex_chat(system: str, user: str) -> str:
    """
    Vertex AI Gemini via GenerativeModel.generateContent (see inference docs).
    https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/inference
    """
    _ensure_vertex_init()
    from vertexai.generative_models import GenerativeModel

    model = GenerativeModel(VERTEX_MODEL, system_instruction=system)
    response = model.generate_content(user)
    if not response.candidates:
        fr = getattr(response, "prompt_feedback", None)
        return f"[blocked or empty response] {fr!r}"
    try:
        return (response.text or "").strip()
    except ValueError:
        return ""


def ollama_chat(system: str, user: str, timeout_s: int = 600) -> str:
    url = f"{OLLAMA_HOST}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = body.get("message") or {}
    return str(msg.get("content") or "")


def ollama_available() -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n([\s\S]*?)\n```\s*$", text)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


def fetch_github_issue(issue_number: int) -> dict[str, Any] | None:
    if not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues/{issue_number}"
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
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return {
        "issue_number": issue_number,
        "title": data.get("title", ""),
        "body": data.get("body") or "",
        "state": data.get("state", ""),
        "html_url": data.get("html_url", ""),
        "note": f"live GitHub issue from {GITHUB_OWNER}/{GITHUB_REPO}",
    }


def collect_repo_files(repo_root: str, max_files: int = 280) -> list[str]:
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in root.rglob("*"):
        if len(out) >= max_files:
            break
        if p.is_dir():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        rel_s = str(rel).replace("\\", "/")
        if "airflow/www/static/dist" in rel_s:
            continue
        parts = rel.parts
        if any(x in SKIP_DIR_NAMES for x in parts):
            continue
        if p.name.startswith(".") and p.name != ".flake8":
            continue
        if rel_s.endswith((".py", ".yaml", ".yml", ".md", ".rst")):
            out.append(rel_s)
    out.sort()
    return out[:max_files]


def readme_snippet(repo_root: str, max_lines: int = 35) -> str:
    root = Path(repo_root).expanduser().resolve()
    for name in ("README.md", "README.rst"):
        p = root / name
        if p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                return "\n".join(lines[:max_lines])
            except OSError:
                continue
    return ""


def build_repo_context(repo_path: str) -> str:
    files = collect_repo_files(repo_path)
    readme = readme_snippet(repo_path)
    lines = [
        f"Repository root: {repo_path}",
        f"File sample ({len(files)} paths, Python/YAML/Markdown only; truncated):",
    ]
    lines.extend(files[:280])
    if readme:
        lines.append("\n--- README excerpt ---\n")
        lines.append(readme)
    return "\n".join(lines)


def stub_ask_issue(issue_number: int) -> dict[str, Any]:
    gh = fetch_github_issue(issue_number)
    if gh:
        return gh
    return {
        "issue_number": issue_number,
        "title": f"[STUB] Issue #{issue_number}",
        "body": "Stub issue — set GITHUB_TOKEN + GITHUB_OWNER/GITHUB_REPO for live GitHub data.",
        "state": "open",
        "html_url": f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/issues/{issue_number}",
    }


def stub_plan(issue_number: int, repo_path: str) -> dict[str, Any]:
    plan = {
        "summary": "Stub plan (AUTOBOT_MODE=stub).",
        "files": ["airflow/__init__.py"],
        "steps": ["Inspect code", "Add fix", "Test"],
    }
    code_spans = [
        {
            "file": "airflow/example_dags/example.py",
            "symbol": "main",
            "start_line": 1,
            "end_line": 20,
        }
    ]
    return {
        "issue_number": issue_number,
        "repo_path": repo_path,
        "plan": plan,
        "code_spans": code_spans,
        "note": "stub planner",
    }


def parse_plan_response(raw: str, backend: str) -> dict[str, Any]:
    parsed = extract_json_object(raw)
    plan = {
        "summary": str(parsed.get("summary", "")),
        "files": list(parsed.get("files") or []),
        "steps": list(parsed.get("steps") or []),
    }
    spans = parsed.get("code_spans") or []
    code_spans: list[dict[str, Any]] = []
    for s in spans:
        if not isinstance(s, dict):
            continue
        start_line = max(1, int(s.get("start_line", 1)))
        end_line = max(start_line, int(s.get("end_line", max(10, start_line))))
        code_spans.append(
            {
                "file": str(s.get("file", "airflow/__init__.py")),
                "symbol": str(s.get("symbol", "unknown")),
                "start_line": start_line,
                "end_line": end_line,
            }
        )
    if not code_spans:
        code_spans = [
            {
                "file": "README.md",
                "symbol": "n/a",
                "start_line": 1,
                "end_line": 5,
            }
        ]
    return {
        "plan": plan,
        "code_spans": code_spans,
        "raw_model_text": raw[:4000],
        "note": f"planner via {backend}",
    }


def llm_plan(
    chat_fn: Callable[[str, str], str],
    issue_number: int,
    repo_path: str,
    issue_title: str,
    issue_body: str,
    backend: str,
) -> dict[str, Any]:
    system = (
        "You are the Planner for the Apache Airflow codebase. "
        "Output a single JSON object only — no markdown fences, no commentary. "
        "Schema: {\n"
        '  "summary": string,\n'
        '  "files": string[],\n'
        '  "steps": string[],\n'
        '  "code_spans": [{ "file": string, "symbol": string, "start_line": number, "end_line": number }]\n'
        "}\n"
        "Use file paths that appear in the repository listing. Prefer paths under airflow/."
    )
    ctx = build_repo_context(repo_path)
    user = (
        f"GitHub issue #{issue_number}\n"
        f"Title: {issue_title}\n\n"
        f"Body:\n{issue_body[:12000]}\n\n"
        f"--- Repository context ---\n{ctx[:80000]}\n"
    )
    raw = chat_fn(system, user)
    out = parse_plan_response(raw, backend)
    return {
        "issue_number": issue_number,
        "repo_path": repo_path,
        "plan": out["plan"],
        "code_spans": out["code_spans"],
        "note": out["note"],
        "raw_model_text": out["raw_model_text"],
    }


def llm_patch_and_critic(
    chat_fn: Callable[[str, str], str],
    issue_title: str,
    issue_body: str,
    plan: Any,
    code_spans: Any,
    backend: str,
    max_iterations: int = 3,
) -> dict[str, Any]:
    plan_s = json.dumps(plan, indent=2) if not isinstance(plan, str) else plan
    spans_s = json.dumps(code_spans, indent=2) if not isinstance(code_spans, str) else code_spans

    def _strip_fences(text: str) -> str:
        text = text.strip()
        if not text.startswith("```"):
            return text
        lines = text.splitlines()
        inner: list[str] = []
        first = True
        for line in lines:
            if first:
                first = False
                continue
            if line.strip() == "```":
                break
            inner.append(line)
        return "\n".join(inner).strip()

    def _validate_diff(diff_text: str) -> str:
        lines = [ln for ln in diff_text.splitlines() if ln.strip()]
        if not lines:
            return "ERROR: diff is empty"
        if not any(ln.startswith("--- ") for ln in lines):
            return "ERROR: missing '--- ' header"
        if not any(ln.startswith("+++ ") for ln in lines):
            return "ERROR: missing '+++ ' header"
        if not any(ln.startswith("@@ ") for ln in lines):
            return "ERROR: missing '@@ ' hunk header"
        return "VALID"

    def _extract_diff_payload(raw: str) -> str:
        text = _strip_fences(raw)
        idx = text.find("diff --git ")
        if idx != -1:
            return text[idx:].strip()

        # Fallback: many models emit unified hunks without diff --git header.
        # If we can find ---/+++ headers, normalize to full git diff.
        lines = text.splitlines()
        start = -1
        for i, ln in enumerate(lines):
            if ln.startswith("--- "):
                start = i
                break
        if start == -1:
            return text.strip()

        chunk = lines[start:]
        minus = ""
        plus = ""
        for ln in chunk:
            if ln.startswith("--- ") and not minus:
                minus = ln[4:].strip()
            elif ln.startswith("+++ ") and not plus:
                plus = ln[4:].strip()
            if minus and plus:
                break

        if not minus or not plus:
            return "\n".join(chunk).strip()

        def _clean(path: str) -> str:
            p = path
            if p.startswith("a/") or p.startswith("b/"):
                p = p[2:]
            return p

        a_path = _clean(minus)
        b_path = _clean(plus)
        if a_path == "/dev/null":
            a_path = b_path
        if b_path == "/dev/null":
            b_path = a_path

        return (
            f"diff --git a/{a_path} b/{b_path}\n" + "\n".join(chunk).strip()
        )

    def _touched_files(diff_text: str) -> list[str]:
        files: list[str] = []
        for line in diff_text.splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                files.append(path)
        return files

    def _parse_critic(raw: str) -> tuple[str, str]:
        response_text = _strip_fences(raw)
        verdict = "REVISE"
        feedback = ""
        try:
            parsed = json.loads(response_text)
            verdict = str(parsed.get("verdict", "REVISE")).upper()
            feedback = str(parsed.get("feedback", ""))
        except Exception:
            match = re.search(r'"verdict"\s*:\s*"(ACCEPT|REVISE|REJECT)"', response_text)
            if match:
                verdict = match.group(1)
            fb_match = re.search(r'"feedback"\s*:\s*"([^"]*)"', response_text)
            feedback = fb_match.group(1) if fb_match else response_text[:800]
        if verdict not in ("ACCEPT", "REVISE", "REJECT"):
            verdict = "REVISE"
        return verdict, feedback

    last_diff = ""
    last_verdict = "REVISE"
    last_feedback = ""
    progress: list[str] = []
    plan_files = []
    if isinstance(plan, dict):
        plan_files = [str(p) for p in (plan.get("files") or [])]

    for iteration in range(max_iterations):
        feedback_section = f"\nCRITIC FEEDBACK TO ADDRESS:\n{last_feedback}\n" if last_feedback else ""

        patcher_sys = (
            "You are a code Patcher. Generate a unified diff implementing this plan. "
            "Output ONLY a unified diff in standard git format. No explanation."
        )
        patcher_user = (
            f"PLAN:\n{plan_s}\n\n"
            f"CODE SPANS:\n{spans_s}\n"
            f"{feedback_section}\n"
            "Each file must include proper diff --git, --- / +++ headers and @@ hunks. "
            "Touch planned files first; do not return README placeholder text. "
            "Return ONLY the diff text, no prose and no markdown fences."
        )
        raw_diff = chat_fn(patcher_sys, patcher_user)
        diff_text = _extract_diff_payload(raw_diff)
        validation = _validate_diff(diff_text)
        touched = _touched_files(diff_text)
        if validation == "VALID" and plan_files:
            if not any(tf in plan_files for tf in touched):
                validation = (
                    "ERROR: diff does not modify planned files; "
                    f"planned={plan_files} touched={touched}"
                )
        progress.append(f"Patcher (iter {iteration}): {validation}")

        if validation != "VALID":
            last_diff = diff_text
            last_verdict = "REVISE"
            last_feedback = f"Generate a valid unified diff. {validation}"
            progress.append(f"Skipping critic (iter {iteration}) due to invalid patch")
            continue

        critic_sys = (
            "You are a code Critic. Evaluate this diff against the issue and plan. "
            "Respond with JSON only: "
            '{"verdict":"ACCEPT|REVISE|REJECT","feedback":"..."}'
        )
        critic_user = (
            f"ISSUE_TITLE: {issue_title}\n"
            f"ISSUE_BODY: {issue_body[:12000]}\n\n"
            f"PLAN:\n{plan_s}\n\n"
            f"DIFF:\n{diff_text[:12000]}\n\n"
            "ACCEPT if diff is correct and review-ready. "
            "REVISE if fixable issues remain. "
            "REJECT if approach is fundamentally wrong."
        )
        raw_critic = chat_fn(critic_sys, critic_user)
        verdict, feedback = _parse_critic(raw_critic)
        progress.append(f"Critic (iter {iteration}): verdict={verdict}")

        last_diff = diff_text
        last_verdict = verdict
        last_feedback = feedback

        if verdict in ("ACCEPT", "REJECT"):
            return {
                "diff": last_diff,
                "verdict": last_verdict,
                "reasoning": last_feedback,
                "plan_echo": plan,
                "iterations_used": iteration + 1,
                "note": f"patcher+critic via {backend}",
                "progress": progress,
            }

    return {
        "diff": "",
        "verdict": "REJECT",
        "reasoning": (
            "Patcher could not produce a valid unified diff touching planned files "
            f"after {max_iterations} iterations. Last feedback: {last_feedback}"
        ),
        "plan_echo": plan,
        "iterations_used": max_iterations,
        "note": f"patcher+critic via {backend}",
        "progress": progress + [f"Loop exhausted after {max_iterations} iterations"],
    }


def _issue_title_body(issue_number: int) -> tuple[str, str]:
    gh = fetch_github_issue(issue_number)
    if gh:
        return str(gh.get("title") or ""), str(gh.get("body") or "")
    return (
        f"[STUB] Issue #{issue_number}",
        "No GitHub token — using stub title/body. Set GITHUB_TOKEN for real issue text.",
    )


@app.post("/api/orchestrate")
def orchestrate():
    data = request.get_json(silent=True) or {}
    command = data.get("command")
    if not command:
        return jsonify({"error": "missing command"}), 400

    mode = AUTOBOT_MODE
    use_ollama = mode == "ollama" and ollama_available()
    use_google_ai = mode == "google_ai" and bool(GOOGLE_API_KEY)
    use_vertex = mode == "vertex" and bool(GCP_PROJECT_ID)

    if command == "ask_issue":
        n = int(data.get("issue_number") or 0)
        return jsonify(stub_ask_issue(n))

    if command == "plan_patch":
        n = data.get("issue_number")
        repo = str(data.get("repo_path") or "").strip()
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid issue_number"}), 400
        if not repo:
            return jsonify({"error": "repo_path is required (set local Airflow clone path)"}), 400
        if not Path(repo).expanduser().is_dir():
            return jsonify({"error": f"repo_path is not a directory: {repo}"}), 400

        title, body = _issue_title_body(n_int)

        if use_google_ai:
            try:
                return jsonify(
                    llm_plan(
                        google_ai_chat,
                        n_int,
                        repo,
                        title,
                        body,
                        f"google_ai:{GEMINI_MODEL}",
                    )
                )
            except Exception as e:
                return jsonify({"error": f"google_ai planner failed: {e}"}), 502

        if use_vertex:
            try:
                return jsonify(
                    llm_plan(vertex_chat, n_int, repo, title, body, f"vertex:{VERTEX_MODEL}")
                )
            except Exception as e:
                return jsonify({"error": f"vertex planner failed: {e}"}), 502

        if use_ollama:
            try:
                return jsonify(
                    llm_plan(ollama_chat, n_int, repo, title, body, f"ollama:{OLLAMA_MODEL}")
                )
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as e:
                return jsonify({"error": f"ollama planner failed: {e}"}), 502

        return jsonify(stub_plan(n_int, repo))

    if command == "accept_plan":
        plan = data.get("plan")
        code_spans = data.get("code_spans")
        issue_number = data.get("issue_number")
        issue_title = "Unknown issue title"
        issue_body = ""
        try:
            if issue_number is not None:
                issue_title, issue_body = _issue_title_body(int(issue_number))
        except (TypeError, ValueError):
            pass
        if use_google_ai:
            try:
                return jsonify(
                    llm_patch_and_critic(
                        google_ai_chat,
                        issue_title,
                        issue_body,
                        plan,
                        code_spans,
                        f"google_ai:{GEMINI_MODEL}",
                    )
                )
            except Exception as e:
                return jsonify({"error": f"google_ai patch/critic failed: {e}"}), 502

        if use_vertex:
            try:
                return jsonify(
                    llm_patch_and_critic(
                        vertex_chat,
                        issue_title,
                        issue_body,
                        plan,
                        code_spans,
                        f"vertex:{VERTEX_MODEL}",
                    )
                )
            except Exception as e:
                return jsonify({"error": f"vertex patch/critic failed: {e}"}), 502

        if use_ollama:
            try:
                return jsonify(
                    llm_patch_and_critic(
                        ollama_chat,
                        issue_title,
                        issue_body,
                        plan,
                        code_spans,
                        f"ollama:{OLLAMA_MODEL}",
                    )
                )
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as e:
                return jsonify({"error": f"ollama patch/critic failed: {e}"}), 502

        return (
            jsonify(
                {
                    "error": (
                        "No active LLM mode for accept_plan. Set AUTOBOT_MODE to "
                        "google_ai, vertex, or ollama and restart app.py."
                    )
                }
            ),
            400,
        )

    if command == "open_pr":
        diff = str(data.get("diff") or "")
        return jsonify(
            {
                "status": "ok",
                "title": "[local] AutoBot draft PR",
                "body": f"Local test PR.\n\n```diff\n{diff[:2000]}\n```",
                "html_url": f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/compare/local-test",
                "note": "No GitHub API call",
            }
        )

    return jsonify({"error": f"unknown command: {command}"}), 400


@app.get("/health")
def health():
    ollama_ok = ollama_available()
    return jsonify(
        {
            "status": "ok",
            "service": "local_autobot_orchestrator",
            "mode": AUTOBOT_MODE,
            "gcp_project": GCP_PROJECT_ID or None,
            "gcp_location": GCP_LOCATION,
            "vertex_model": VERTEX_MODEL,
            "vertex_ready": bool(GCP_PROJECT_ID),
            "vertex_llm_active": AUTOBOT_MODE == "vertex" and bool(GCP_PROJECT_ID),
            "gemini_model": GEMINI_MODEL,
            "google_api_key_set": bool(GOOGLE_API_KEY),
            "google_ai_llm_active": AUTOBOT_MODE == "google_ai" and bool(GOOGLE_API_KEY),
            "github_repo": f"{GITHUB_OWNER}/{GITHUB_REPO}",
            "github_token_set": bool(GITHUB_TOKEN),
            "ollama_host": OLLAMA_HOST,
            "ollama_model": OLLAMA_MODEL,
            "ollama_reachable": ollama_ok,
            "ollama_llm_active": AUTOBOT_MODE == "ollama" and ollama_ok,
        }
    )


@app.get("/api/orchestrate")
def orchestrate_get():
    return jsonify(
        {
            "message": "POST JSON with command ask_issue | plan_patch | accept_plan | open_pr",
            "autobot_mode": AUTOBOT_MODE,
            "google_ai": f"model={GEMINI_MODEL} (GOOGLE_API_KEY in .env)",
            "vertex": f"{VERTEX_MODEL} project={GCP_PROJECT_ID or '?'} location={GCP_LOCATION}",
            "ollama": f"{OLLAMA_HOST} model={OLLAMA_MODEL}",
        }
    )


def main() -> None:
    port = int(os.environ.get("PORT", "5000"))
    print(f"AUTOBOT_MODE={AUTOBOT_MODE} GCP_PROJECT_ID={GCP_PROJECT_ID or '(unset)'}")
    print(
        f"VERTEX_MODEL={VERTEX_MODEL} GEMINI_MODEL={GEMINI_MODEL} "
        f"GOOGLE_API_KEY={'set' if GOOGLE_API_KEY else 'unset'} "
        f"OLLAMA reachable={ollama_available()}"
    )
    print(f"GitHub repo={GITHUB_OWNER}/{GITHUB_REPO} token={'set' if GITHUB_TOKEN else 'unset'}")
    print(f"Listening on http://127.0.0.1:{port}  POST /api/orchestrate")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
