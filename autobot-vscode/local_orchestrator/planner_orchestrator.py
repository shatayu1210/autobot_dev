"""
Planner-Orchestrator: Refinement loop for the AutoBot Planner model.

Sits between the Planner model call and the Patcher-Orchestrator handoff.
Detects weak/incomplete plans, runs deterministic research tools (no LLM),
and re-prompts the planner with enriched evidence until confidence passes
the threshold or a plateau is detected.

See next_steps/patch_planner/orchestrator.md §3 & §10 for full spec.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ── Data structures ────────────────────────────────────────────────────────


@dataclass
class Issue:
    number: int
    title: str
    body: str


@dataclass
class PlannerPlan:
    requires_code_change: bool
    files: list[str]
    summary: str
    steps: list[str]
    code_spans: list[dict]  # {file, symbol, start_line, end_line}
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict) -> "PlannerPlan":
        return cls(
            requires_code_change=bool(raw.get("requires_code_change", True)),
            files=list(raw.get("files") or []),
            summary=str(raw.get("summary", "")),
            steps=list(raw.get("steps") or []),
            code_spans=list(raw.get("code_spans") or []),
            raw=raw,
        )


@dataclass
class OrchestratorTrace:
    issue_number: int
    backend: str
    iterations: int = 0
    final_confidence: float = 0.0
    triggers_detected: list[str] = field(default_factory=list)
    research_steps_used: int = 0
    delta_snippets: int = 0
    passes: list[dict] = field(default_factory=list)  # [{files, confidence}]


# ── Constants ──────────────────────────────────────────────────────────────

MAX_REFINEMENT_ITERATIONS = 5
CONFIDENCE_THRESHOLD = 0.75
PLATEAU_DELTA = 0.05
MAX_RESEARCH_STEPS = 15
MAX_DEEP_READS_PER_ITER = 8
DELTA_PACK_SNIPPETS = 12


# ── Research tools (deterministic — no LLM) ────────────────────────────────


def _extract_keywords(body: str) -> list[str]:
    """Pull error names, CamelCase class names, and airflow module names from issue body."""
    words = re.findall(
        r"[A-Z][a-zA-Z0-9]{3,}|airflow\.\S+|Error\w*|Exception\w*", body
    )
    return list(dict.fromkeys(words))[:12]


def keyword_search(
    repo_root: str, keywords: list[str], top_k: int = 10
) -> list[dict]:
    """grep -rn keywords across .py files; returns [{file, line, snippet}]."""
    if not keywords:
        return []
    pattern = "|".join(re.escape(k) for k in keywords[:8])
    try:
        out = subprocess.run(
            [
                "grep",
                "-rn",
                "--include=*.py",
                "-m",
                "3",
                "-E",
                pattern,
                repo_root,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except Exception:
        return []
    results: list[dict] = []
    for line in out.splitlines()[: top_k * 3]:
        parts = line.split(":", 2)
        if len(parts) >= 3:
            results.append(
                {
                    "file": parts[0].replace(repo_root, "").lstrip("/"),
                    "line": parts[1],
                    "snippet": parts[2][:120],
                }
            )
    # deduplicate by file, keep top_k
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        if r["file"] not in seen:
            seen.add(r["file"])
            deduped.append(r)
    return deduped[:top_k]


def find_file(repo_root: str, pattern: str) -> list[str]:
    """Glob match across repo tree."""
    root = Path(repo_root)
    return [
        str(p.relative_to(root))
        for p in root.rglob(f"*{pattern}*")
        if p.is_file() and ".venv" not in str(p)
    ][:10]


def get_symbols(ts_index: dict, file_path: str) -> list[dict]:
    """Return top-level symbols for file_path from pre-built tree-sitter index."""
    return ts_index.get(file_path, [])


def read_file_window(
    repo_root: str,
    file_path: str,
    start_line: int,
    end_line: int,
    context: int = 20,
) -> str:
    """Read ±context lines around [start_line, end_line] in a file."""
    p = Path(repo_root) / file_path
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(errors="replace").splitlines()
        lo = max(0, start_line - context - 1)
        hi = min(len(lines), end_line + context)
        numbered = [f"{i + lo + 1}: {l}" for i, l in enumerate(lines[lo:hi])]
        return "\n".join(numbered)
    except OSError:
        return ""


def get_imports(repo_root: str, file_path: str) -> list[str]:
    """Parse import statements from a .py file."""
    p = Path(repo_root) / file_path
    if not p.is_file():
        return []
    imports: list[str] = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("from ") or line.startswith("import "):
            imports.append(line)
    return imports[:30]


def compress_delta(
    snippets: list[str], max_count: int = DELTA_PACK_SNIPPETS
) -> str:
    """Truncate and join evidence snippets into a single string for the LLM context."""
    selected = snippets[:max_count]
    return "\n\n---EVIDENCE---\n".join(s[:600] for s in selected)


# ── Core functions ─────────────────────────────────────────────────────────


def detect_triggers(plan: PlannerPlan, repo_path: str) -> list[str]:
    """
    Detect weaknesses in the plan that should trigger a research + refinement pass.
    Returns list of trigger names (empty = no refinement needed).
    """
    triggers: list[str] = []
    root = Path(repo_path)

    # No files listed
    if not plan.files:
        triggers.append("sparse_files")
    elif all(f.endswith("__init__.py") for f in plan.files):
        triggers.append("sparse_files")

    # Any listed path doesn't exist
    for f in plan.files:
        if not (root / f).exists():
            triggers.append("path_not_found")
            break

    # Planner said no code change needed (suspicious for bug issues)
    if not plan.requires_code_change:
        triggers.append("no_code_change_flagged")

    # No code spans (planner couldn't identify specific locations)
    if not plan.code_spans:
        triggers.append("no_code_spans")

    return triggers


def score_plan(
    plan: PlannerPlan,
    repo_path: str,
    graphrag_candidates: list[str],
) -> float:
    """
    Deterministic confidence score for a plan. No LLM involved.
    Returns 0.0 – 1.0.
    """
    score = 0.5
    root = Path(repo_path)

    if plan.requires_code_change is not None:
        score += 0.10

    valid_files = [f for f in plan.files if (root / f).exists()]
    if plan.files and len(valid_files) == len(plan.files):
        score += 0.15
    elif valid_files:
        score += 0.07  # partial credit

    if 1 <= len(plan.files) <= 6:
        score += 0.10

    if any(s.get("start_line", 0) > 0 for s in plan.code_spans):
        score += 0.10

    overlap = len(set(plan.files) & set(graphrag_candidates))
    score += min(overlap * 0.05, 0.15)

    return min(round(score, 3), 1.0)


def research_loop(
    triggers: list[str],
    issue: Issue,
    plan: PlannerPlan,
    repo_path: str,
    ts_index: dict,
    max_steps: int = MAX_RESEARCH_STEPS,
    max_reads: int = MAX_DEEP_READS_PER_ITER,
) -> list[str]:
    """
    Run deterministic research tools to build a delta evidence pack.
    All calls are pure Python — no LLM involved.
    Returns list of evidence snippet strings.
    """
    snippets: list[str] = []
    steps = 0
    reads = 0

    # Extract keywords from issue body
    keywords = _extract_keywords(issue.body)

    # 1. Keyword grep search across repo
    matches: list[dict] = []
    if "sparse_files" in triggers or "path_not_found" in triggers:
        matches = keyword_search(repo_path, keywords, top_k=10)
        for m in matches:
            snippets.append(f"KEYWORD_MATCH {m['file']}:{m['line']}\n{m['snippet']}")
        steps += len(matches)

    if steps >= max_steps:
        return snippets

    # 2. Get tree-sitter symbols for each candidate file
    candidate_files = list(
        dict.fromkeys(plan.files + [m["file"] for m in matches])
    )[:8]
    for f in candidate_files:
        syms = get_symbols(ts_index, f)
        if syms:
            snippets.append(f"SYMBOLS {f}: {[s['name'] for s in syms]}")
        steps += 1
        if steps >= max_steps:
            break

    # 3. Read windows around planner's code_spans
    for span in plan.code_spans:
        if reads >= max_reads or steps >= max_steps:
            break
        window = read_file_window(
            repo_path, span["file"], span.get("start_line", 1), span.get("end_line", 20)
        )
        if window:
            snippets.append(
                f"FILE_WINDOW {span['file']}:{span.get('start_line', 1)}-{span.get('end_line', 20)}\n{window}"
            )
        reads += 1
        steps += 1

    # 4. GraphRAG neighbors of planner's files (Neo4j call)
    try:
        from graphrag_client import get_neighbor_files

        for f in plan.files[:3]:
            if steps >= max_steps:
                break
            neighbors = get_neighbor_files(f, top_k=3)
            if neighbors:
                snippets.append(f"GRAPHRAG_NEIGHBORS of {f}: {neighbors}")
            steps += 1
    except ImportError:
        pass  # Neo4j driver not installed — skip

    return snippets


def log_trace(trace: OrchestratorTrace, logs_dir: str = "logs") -> None:
    """Persist a JSON trace for offline analysis."""
    Path(logs_dir).mkdir(exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    path = Path(logs_dir) / f"planner_trace_{trace.issue_number}_{ts}.json"
    path.write_text(json.dumps(vars(trace), indent=2, default=str))


# ── Main entry point ──────────────────────────────────────────────────────


def _parse_planner_json(raw: str) -> dict:
    """Extract JSON from planner model output (handles markdown fences)."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*\n([\s\S]*?)\n```\s*$", text)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


def run_planner_with_refinement(
    chat_fn: Callable[[str, str], str],
    issue: Issue,
    repo_path: str,
    repo_context: str,
    ts_index: dict,
    graphrag_candidates: list[str],
    backend: str,
) -> tuple[PlannerPlan, OrchestratorTrace]:
    """
    Run the planner model, detect weaknesses, optionally research + retry.
    Returns (finalized plan, trace for logging).

    Args:
        chat_fn: LLM call function with signature (system, user) -> str
        issue: Issue dataclass with number, title, body
        repo_path: Absolute path to the repo checkout
        repo_context: Pre-built repo context string (file listing + README)
        ts_index: Loaded tree-sitter index dict {file_path: [symbols]}
        graphrag_candidates: List of file paths from GraphRAG
        backend: String label like "ollama:qwen2.5-coder:7b"
    """
    trace = OrchestratorTrace(issue_number=issue.number, backend=backend)

    system_prompt = (
        "You are the Planner for the Apache Airflow codebase. "
        "Output a single JSON object only — no markdown fences, no commentary. "
        "Schema: {\n"
        '  "requires_code_change": boolean,\n'
        '  "summary": string,\n'
        '  "files": string[],\n'
        '  "steps": string[],\n'
        '  "code_spans": [{ "file": string, "symbol": string, "start_line": number, "end_line": number }]\n'
        "}\n"
        "Use file paths that appear in the repository listing. Prefer paths under airflow/."
    )

    def _build_user_prompt(extra_evidence: str = "") -> str:
        parts = [
            f"GitHub issue #{issue.number}\n",
            f"Title: {issue.title}\n\n",
            f"Body:\n{issue.body[:12000]}\n\n",
            f"--- Repository context ---\n{repo_context[:80000]}\n",
        ]
        if graphrag_candidates:
            parts.append(
                f"\n--- GraphRAG candidate files (historically relevant) ---\n"
                + "\n".join(graphrag_candidates[:15])
                + "\n"
            )
        if extra_evidence:
            parts.append(
                f"\n--- Additional research evidence (use to refine your plan) ---\n"
                + extra_evidence
                + "\n"
            )
        return "".join(parts)

    # Pass 1: initial planner call
    user_prompt = _build_user_prompt()
    raw_response = chat_fn(system_prompt, user_prompt)

    try:
        parsed = _parse_planner_json(raw_response)
        plan = PlannerPlan.from_raw(parsed)
    except (json.JSONDecodeError, KeyError, TypeError):
        # Malformed JSON on first pass — treat as a trigger
        plan = PlannerPlan(
            requires_code_change=True,
            files=[],
            summary="Planner returned malformed JSON",
            steps=[],
            code_spans=[],
            raw={"raw_text": raw_response[:3000]},
        )

    confidence = score_plan(plan, repo_path, graphrag_candidates)
    trace.passes.append({"files": plan.files, "confidence": confidence})

    # Refinement loop
    for iteration in range(MAX_REFINEMENT_ITERATIONS):
        if confidence >= CONFIDENCE_THRESHOLD:
            break

        triggers = detect_triggers(plan, repo_path)
        # Also check for zero GraphRAG matches (OOD signal)
        if not graphrag_candidates:
            triggers.append("zero_graphrag_matches")

        if not triggers:
            break

        trace.triggers_detected = list(set(trace.triggers_detected + triggers))

        delta = research_loop(
            triggers, issue, plan, repo_path, ts_index,
            MAX_RESEARCH_STEPS, MAX_DEEP_READS_PER_ITER,
        )
        trace.research_steps_used += len(delta)

        evidence_str = compress_delta(delta, DELTA_PACK_SNIPPETS)
        trace.delta_snippets += min(len(delta), DELTA_PACK_SNIPPETS)

        # Re-prompt planner with enriched evidence
        user_prompt = _build_user_prompt(extra_evidence=evidence_str)

        prev_confidence = confidence
        raw_response = chat_fn(system_prompt, user_prompt)

        try:
            parsed = _parse_planner_json(raw_response)
            plan = PlannerPlan.from_raw(parsed)
        except (json.JSONDecodeError, KeyError, TypeError):
            # If JSON is still broken, keep the previous plan
            pass

        confidence = score_plan(plan, repo_path, graphrag_candidates)
        trace.passes.append({"files": plan.files, "confidence": confidence})
        trace.iterations = iteration + 1

        if abs(confidence - prev_confidence) < PLATEAU_DELTA:
            break  # plateau — further calls won't help

    trace.final_confidence = confidence
    return plan, trace
