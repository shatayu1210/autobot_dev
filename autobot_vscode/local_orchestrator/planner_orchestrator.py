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
    """
    Multi-Keyword Intersection Scorer.
    Phase 1: grep -l to gather candidate files (up to 60).
    Phase 2: score each candidate by counting unique keyword hits + path boost.
    Phase 3: sort by score, take top_k, return [{file, line, snippet, score}].
    """
    if not keywords:
        return []

    # ── Phase 1: collect candidate files ──────────────────────────────────────
    pattern = "|".join(re.escape(k) for k in keywords[:8])
    try:
        candidate_output = subprocess.run(
            ["grep", "-rl", "--include=*.py", "--include=*.ts",
             "--include=*.css", "--include=*.tsx", "-E", pattern, repo_root],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return []
    candidates = [
        p for p in candidate_output.splitlines()
        if ".venv" not in p and "__pycache__" not in p
    ][:60]

    # ── Phase 2: score each candidate ─────────────────────────────────────────
    scored: list[dict] = []
    for filepath in candidates:
        rel = filepath.replace(repo_root, "").lstrip("/")
        try:
            content = Path(filepath).read_text(errors="replace").lower()
        except OSError:
            continue

        score = 0
        hits_found: list[str] = []
        for kw in keywords[:8]:
            if kw.lower() in content:
                score += 10
                hits_found.append(kw)

        # Path domain boost: reward paths matching issue domain hints
        path_lower = rel.lower()
        for domain_hint in ["ui", "www", "css", "theme", "trigger", "scheduler",
                             "task", "executor", "dag", "model", "listener"]:
            if domain_hint in path_lower:
                score += 5
                break

        if score > 0:
            scored.append({"file": rel, "path": filepath, "score": score, "hits": hits_found})

    scored.sort(key=lambda x: x["score"], reverse=True)

    # ── Phase 3: extract snippets from top_k files ────────────────────────────
    results: list[dict] = []
    for entry in scored[:top_k]:
        try:
            out = subprocess.run(
                ["grep", "-n", "-E", pattern, entry["path"]],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            continue
        lines = out.splitlines()[:3]  # max 3 lines per file
        for raw_line in lines:
            parts = raw_line.split(":", 2)
            if len(parts) >= 3:
                results.append({
                    "file": entry["file"],
                    "line": parts[1],
                    "snippet": parts[2][:120],
                    "score": entry["score"],
                })
    return results[:top_k]



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
    """
    Compress evidence pack to max_count snippets.
    Enforces diversity: max 3 snippets per unique file to prevent flooding
    the planner context with redundant evidence from one large file.
    """
    file_counts: dict[str, int] = {}
    selected: list[str] = []
    for s in snippets:
        first_line = s.split("\n")[0]
        # Extract file hint from lines like "KEYWORD_MATCH airflow/foo.py:42"
        file_key = first_line.split(":")[0].split(" ")[-1]
        if file_counts.get(file_key, 0) >= 3:
            continue
        file_counts[file_key] = file_counts.get(file_key, 0) + 1
        selected.append(s[:600])
        if len(selected) >= max_count:
            break
    return "\n\n---EVIDENCE---\n".join(selected)


# ── Core functions ─────────────────────────────────────────────────────────


def get_callers(repo_root: str, symbol_name: str, top_k: int = 8) -> list[dict]:
    """Find files that call symbol_name() across the repo."""
    try:
        out = subprocess.run(
            ["grep", "-rn", "--include=*.py", "-m", "2", f"{symbol_name}(", repo_root],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return []
    results: list[dict] = []
    for line in out.splitlines()[: top_k * 3]:
        parts = line.split(":", 2)
        if len(parts) >= 3:
            results.append({
                "file": parts[0].replace(repo_root, "").lstrip("/"),
                "line": parts[1],
                "snippet": parts[2][:120],
            })
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        if r["file"] not in seen:
            seen.add(r["file"]); deduped.append(r)
    return deduped[:top_k]


def find_test_files(repo_root: str, target_files: list[str]) -> list[str]:
    """
    Discover test files matching each target file by basename.
    Tries test_{stem}.py, then any *{stem}*test*.py under tests/.
    """
    root = Path(repo_root)
    test_files: list[str] = []
    for f in target_files:
        stem = Path(f).stem
        matches = list(root.rglob(f"test_{stem}.py"))
        matches += list(root.rglob(f"*{stem}*test*.py"))
        test_files += [
            str(p.relative_to(root)) for p in matches if ".venv" not in str(p)
        ]
    return list(dict.fromkeys(test_files))[:6]


def detect_triggers(
    plan: PlannerPlan, repo_path: str, graphrag_candidates: list[str] | None = None
) -> list[str]:
    """
    Detect weaknesses in the plan that should trigger a research + refinement pass.
    Returns list of trigger names (empty = no refinement needed).
    """
    triggers: list[str] = []
    root = Path(repo_path)

    if not plan.files:
        triggers.append("sparse_files")
    elif all(f.endswith("__init__.py") for f in plan.files):
        triggers.append("sparse_files")

    for f in plan.files:
        if not (root / f).exists():
            triggers.append("path_not_found")
            break

    if not plan.requires_code_change:
        triggers.append("no_code_change_flagged")

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

    if 1 <= len(valid_files) <= 6:
        score += 0.20

    overlap = len(set(plan.files) & set(graphrag_candidates))
    score += min(overlap * 0.05, 0.15)

    return min(round(score, 3), 1.0)


def research_loop(
    triggers: list[str],
    issue: Issue,
    plan: PlannerPlan,
    repo_path: str,
    ts_index: dict,
    keywords: list[str],
    is_novel: bool = False,
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

    if not keywords:
        keywords = _extract_keywords(issue.body)

    # Step 1: Keyword grep — fires on sparse/missing files, critic override, or novel issue
    matches: list[dict] = []
    if "sparse_files" in triggers or "path_not_found" in triggers or "critic_override" in triggers or is_novel:
        matches = keyword_search(repo_path, keywords, top_k=10)
        for m in matches:
            snippets.append(f"KEYWORD_MATCH {m['file']}:{m['line']}\n{m['snippet']}")
        steps += len(matches)

    if steps >= max_steps:
        return snippets

    # Step 2: Tree-sitter symbols for candidate files
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

    # Step 2b: Import tracing — follow what candidate files import
    if steps < max_steps and ("sparse_files" in triggers or "novel_issue" in triggers):
        for f in candidate_files[:4]:
            if steps >= max_steps:
                break
            imports = get_imports(repo_path, f)
            if imports:
                snippets.append(f"IMPORTS {f}:\n" + "\n".join(imports[:10]))
            steps += 1

    # Step 2c: Caller tracing — find who calls key symbols from code_spans
    if steps < max_steps and plan.code_spans:
        key_symbols = list(dict.fromkeys(
            s.get("symbol", "") for s in plan.code_spans if s.get("symbol")
        ))[:4]
        for sym in key_symbols:
            if steps >= max_steps:
                break
            callers = get_callers(repo_path, sym, top_k=5)
            for c in callers:
                snippets.append(f"CALLER of {sym}: {c['file']}:{c['line']}\n{c['snippet']}")
            steps += 1

    # Step 3: Read file windows around planner's code_spans
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

    # Step 4: GraphRAG neighbors of planner's files
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
        pass

    # Step 5: Novel-issue wider GraphRAG — traverse co-mod edges from grep hits
    # Anchors on code-side rather than issue-embedding when no vector matches exist
    if is_novel and steps < max_steps:
        try:
            from graphrag_client import get_neighbor_files
            grep_files = list(dict.fromkeys(m["file"] for m in matches))[:5]
            for gf in grep_files:
                if steps >= max_steps:
                    break
                neighbors = get_neighbor_files(gf, top_k=3)
                if neighbors:
                    snippets.append(
                        f"GRAPHRAG_NEIGHBORS (novel-issue path) of {gf}: {neighbors}"
                    )
                steps += 1
        except ImportError:
            pass

    return snippets


def log_trace(trace: OrchestratorTrace, logs_dir: str = "logs") -> None:
    """Persist a JSON trace for offline analysis."""
    Path(logs_dir).mkdir(exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    path = Path(logs_dir) / f"planner_trace_{trace.issue_number}_{ts}.json"
    path.write_text(json.dumps(vars(trace), indent=2, default=str))


# ── Main entry point ──────────────────────────────────────────────────────


def _parse_planner_text(raw: str) -> dict:
    """Extract fields from the strict text format (v7 training spec)."""
    text = raw.strip()
    fence = re.match(r"^```(?:text)?\s*\n([\s\S]*?)\n```\s*$", text)
    if fence:
        text = fence.group(1).strip()

    result = {
        "requires_code_change": False,
        "summary": "",
        "files": [],
        "steps": [],
        "code_spans": []
    }

    lines = text.split('\n')
    current_section = None

    for line in lines:
        line_clean = line.strip()
        if line_clean.startswith("REQUIRES_CODE_CHANGE:"):
            val = line_clean.split(":", 1)[1].strip().upper()
            result["requires_code_change"] = val == "YES"
        elif line_clean.startswith("REASON:"):
            result["summary"] = line_clean.split(":", 1)[1].strip()
        elif line_clean.startswith("PLAN:"):
            current_section = "PLAN"
        elif current_section == "PLAN":
            if line_clean.startswith("- What to change:"):
                result["steps"].append(line_clean.split(":", 1)[1].strip())
            elif line_clean.startswith("- Target files:"):
                current_section = "FILES"
            elif line_clean.startswith("- Test strategy:"):
                result["steps"].append(line_clean)
        elif current_section == "FILES":
            if line_clean.startswith("- "):
                file_path = line_clean[2:].strip()
                if file_path.lower().startswith("test strategy:") or " " in file_path:
                    result["steps"].append(file_path)
                    current_section = "PLAN"
                elif file_path and "<repo/path.py>" not in file_path:
                    result["files"].append(file_path)
            elif line_clean and not line_clean.startswith("-"):
                if line_clean.lower().startswith("test strategy:"):
                    result["steps"].append(line_clean)
                current_section = "PLAN"

    return result


def run_critic(
    chat_fn: Callable[[str, str], str],
    plan: "PlannerPlan",
    issue: "Issue",
    evidence_str: str,
    prev_search_terms: list[str],
) -> dict:
    """
    Invoke the Critic persona on any drafted plan (YES or NO).
    Returns a dict with:
      - decision: 'APPROVED' | 'REJECTED'
      - feedback: str  (reason for rejection, or empty if approved)
      - new_search_terms: list[str]  (optional, if snippets are irrelevant)
    """
    critic_system = (
        "You are AutoBot Critic. Your job is to verify a Planner's draft plan for a GitHub issue.\n"
        "You are NOT the Planner. Do NOT rewrite the plan. Only verify and give feedback.\n"
        "You are an objective verifier. Do not automatically disagree with the plan.\n"
        "Check ONLY these things:\n"
        "1. If REQUIRES_CODE_CHANGE=YES: Are the listed Target Files found in the provided evidence? "
        "If files are hallucinated (not in evidence), reject and name the error.\n"
        "2. If REQUIRES_CODE_CHANGE=NO: Does the research evidence clearly show a bug or UI issue "
        "that requires a code fix? If yes, the Planner is wrong — reject it.\n"
        "3. Are the provided search evidence snippets relevant to the issue? "
        "If 8+ out of 10 snippets are clearly from wrong domain files, output new search terms.\n"
        "Output STRICT plain text only. Format:\n"
        "CRITIC_DECISION: APPROVED\n"
        "or:\n"
        "CRITIC_DECISION: REJECTED\n"
        "CRITIC_FEEDBACK: <one concise sentence explaining the error>\n"
        "NEW_SEARCH_TERMS: term1, term2, term3  # optional, only if snippets are domain-wrong"
    )

    plan_text = (
        f"REQUIRES_CODE_CHANGE: {'YES' if plan.requires_code_change else 'NO'}\n"
        f"REASON: {plan.summary}\n"
        f"Target files: {', '.join(plan.files) if plan.files else '(none)'}\n"
    )

    critic_user = (
        f"GitHub issue #{issue.number}\n"
        f"Title: {issue.title}\n\n"
        f"--- Planner Draft ---\n{plan_text}\n"
        f"--- Previous Search Terms Used ---\n{', '.join(prev_search_terms) if prev_search_terms else '(default keywords)'}\n"
        f"--- Research Evidence (top snippets) ---\n{evidence_str[:4000]}\n"
        f"Now verify the plan and output your decision."
    )

    raw = chat_fn(critic_system, critic_user)
    result = {"decision": "APPROVED", "feedback": "", "new_search_terms": []}

    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("CRITIC_DECISION:"):
            val = line.split(":", 1)[1].strip().upper()
            result["decision"] = "APPROVED" if "APPROVED" in val else "REJECTED"
        elif line.startswith("CRITIC_FEEDBACK:"):
            result["feedback"] = line.split(":", 1)[1].strip()
        elif line.startswith("NEW_SEARCH_TERMS:"):
            raw_terms = line.split(":", 1)[1].strip()
            # strip comment if present
            raw_terms = raw_terms.split("#")[0].strip()
            result["new_search_terms"] = [
                t.strip() for t in raw_terms.split(",") if t.strip()
            ]

    return result



def run_planner_with_refinement(
    chat_fn: Callable[[str, str], str],
    issue: Issue,
    repo_path: str,
    repo_context: str,
    ts_index: dict,
    graphrag_candidates: list[str],
    backend: str,
    on_step: Callable[[str], None] | None = None,
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
        "You are AutoBot Planner. Decide if code change is required and output "
        "STRICT plain text only. Never use markdown, bold, bullets with **, or prose outside format.\n"
        "STRICT GROUNDING: You are an engineering tool. You must only select target files from the "
        "provided context blocks. Do not hallucinate paths or add file extensions not present in the "
        "retrieval.\n"
        "DECISION POLICY: If issue evidence indicates a real code change is needed, output YES and anchor to the best "
        "supported module or directory from the provided context.\n"
        "If code change is needed, output:\n"
        "REQUIRES_CODE_CHANGE: YES\n"
        "REASON: <one sentence>\n"
        "PLAN:\n"
        "- What to change: <one concise paragraph>\n"
        "- Target files:\n"
        "  - <repo/path.py>\n"
        "- Test strategy: <one sentence>\n"
        "If code change is not needed, output:\n"
        "REQUIRES_CODE_CHANGE: NO\n"
        "REASON: <one sentence>"
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

    def _log_step(msg: str):
        if on_step:
            on_step(msg)

    # Pass 1: initial planner call
    _log_step("Calling Planner (pass-1)...")
    user_prompt = _build_user_prompt()
    raw_response = chat_fn(system_prompt, user_prompt)

    try:
        parsed = _parse_planner_text(raw_response)
        plan = PlannerPlan.from_raw(parsed)
    except Exception as e:
        # Malformed output on first pass — treat as a trigger
        plan = PlannerPlan(
            requires_code_change=True,
            files=[],
            summary="Planner returned malformed output.",
            steps=[],
            code_spans=[],
            raw={"raw_text": raw_response[:3000]},
        )

    confidence = score_plan(plan, repo_path, graphrag_candidates)
    trace.passes.append({"files": plan.files, "confidence": confidence})

    # Refinement loop — runs up to MAX_REFINEMENT_ITERATIONS
    current_keywords: list[str] = _extract_keywords(issue.body)
    last_evidence_str: str = ""
    critic_feedback: str = ""

    for iteration in range(MAX_REFINEMENT_ITERATIONS):
        triggers = detect_triggers(plan, repo_path, graphrag_candidates)

        # ── Critic evaluation (runs on EVERY plan regardless of score) ──────
        _log_step("Critic is reviewing the plan...")
        critic_result = run_critic(
            chat_fn, plan, issue, last_evidence_str or "(no evidence yet)", current_keywords
        )
        critic_decision = critic_result["decision"]
        critic_feedback = critic_result["feedback"]
        critic_new_terms = critic_result["new_search_terms"]

        if critic_decision == "APPROVED" and confidence >= CONFIDENCE_THRESHOLD:
            _log_step("Critic approved the plan. Done.")
            break

        if critic_decision == "APPROVED" and confidence < CONFIDENCE_THRESHOLD:
            # Critic approves but structural score is low — trust the Critic, exit
            _log_step("Critic approved. Accepting plan despite low evidence strength.")
            break

        # Critic rejected — update search terms if Critic steered us
        if critic_new_terms:
            current_keywords = critic_new_terms
            _log_step(f"Critic steered search → new terms: {', '.join(current_keywords)}")

        if not triggers and critic_decision == "REJECTED":
            # Triggers didn't fire but Critic caught a semantic error — force research
            triggers = ["critic_override"]

        if not triggers:
            _log_step("Plan looks solid! No refinement needed.")
            break

        _log_step(f"Weakness detected ({', '.join(triggers)}). Running research loop...")
        trace.triggers_detected = list(set(trace.triggers_detected + triggers))

        is_novel = graphrag_candidates is not None and len(graphrag_candidates) == 0
        delta = research_loop(
            triggers, issue, plan, repo_path, ts_index, current_keywords,
            is_novel=is_novel,
            max_steps=MAX_RESEARCH_STEPS, max_reads=MAX_DEEP_READS_PER_ITER,
        )
        trace.research_steps_used += len(delta)

        evidence_str = compress_delta(delta, DELTA_PACK_SNIPPETS)
        last_evidence_str = evidence_str
        trace.delta_snippets += min(len(delta), DELTA_PACK_SNIPPETS)

        # Inject Critic feedback into the re-plan prompt
        combined_evidence = evidence_str
        if critic_feedback:
            combined_evidence = f"CRITIC FEEDBACK: {critic_feedback}\n\n" + evidence_str

        _log_step(f"Re-planning with more evidence (iter {iteration + 1}/{MAX_REFINEMENT_ITERATIONS})...")
        user_prompt = _build_user_prompt(extra_evidence=combined_evidence)

        prev_confidence = confidence
        raw_response = chat_fn(system_prompt, user_prompt)

        try:
            parsed = _parse_planner_text(raw_response)
            plan = PlannerPlan.from_raw(parsed)
        except Exception:
            # If parsing is still broken, keep the previous plan
            pass

        confidence = score_plan(plan, repo_path, graphrag_candidates)
        trace.passes.append({"files": plan.files, "confidence": confidence})
        trace.iterations = iteration + 1

        if abs(confidence - prev_confidence) < PLATEAU_DELTA and critic_decision == "APPROVED":
            break  # plateau + Critic approved — further calls won't help


    trace.final_confidence = confidence
    return plan, trace


# ── Patcher context assembly ───────────────────────────────────────────────


def assemble_patcher_input(
    plan: "PlannerPlan",
    issue: "Issue",
    repo_path: str,
    ts_index: dict,
    test_files: list[str] | None = None,
    token_budget: dict | None = None,
) -> dict:
    """
    Build the full patcher input pack from an approved planner plan.

    Tiered context policy (approximate char budgets):
      primary:    6000  full file if <= 300 lines, else span windows ±40 lines
      supporting: 3000  import-resolved dependent modules (top symbols)
      tests:      1500  relevant test file snippets
      total cap: 12000

    Returns a dict matching the patcher input schema (orchestrator.md §2).
    """
    budget = token_budget or {
        "primary": 6000, "supporting": 3000, "tests": 1500, "total_cap": 12000
    }
    root = Path(repo_path)
    file_contexts: dict[str, list[dict]] = {"primary": [], "supporting": [], "tests": []}
    used_chars = 0

    # ── Primary files ─────────────────────────────────────────────────────────
    for file_path in plan.files:
        if used_chars >= budget["total_cap"]:
            break
        p = root / file_path
        if not p.is_file():
            continue
        lines = p.read_text(errors="replace").splitlines()
        file_len = len(lines)

        if file_len <= 300:
            content = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines))
        else:
            spans = [s for s in plan.code_spans if s.get("file") == file_path]
            if spans:
                windows: list[tuple[int, int]] = []
                for span in spans:
                    lo = max(0, span.get("start_line", 1) - 41)
                    hi = min(file_len, span.get("end_line", lo + 40) + 40)
                    windows.append((lo, hi))
                windows.sort()
                merged: list[tuple[int, int]] = [windows[0]]
                for lo, hi in windows[1:]:
                    if lo <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                    else:
                        merged.append((lo, hi))
                excerpts = []
                for lo, hi in merged:
                    chunk = lines[lo:hi]
                    excerpts.append(
                        f"# Lines {lo+1}–{hi}\n"
                        + "\n".join(f"{lo+i+1}: {l}" for i, l in enumerate(chunk))
                    )
                content = "\n\n...\n\n".join(excerpts)
            else:
                content = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[:150]))

        syms = get_symbols(ts_index, file_path)
        remaining = budget["primary"] - sum(len(c["excerpt"]) for c in file_contexts["primary"])
        entry = {
            "file": file_path,
            "symbols": [s["name"] for s in syms],
            "excerpt": content[:max(0, remaining)],
            "line_count": file_len,
        }
        file_contexts["primary"].append(entry)
        used_chars += len(entry["excerpt"])

    # ── Supporting files (import-resolved dependencies) ────────────────────────
    supporting_seen: set[str] = set(plan.files)
    for file_path in plan.files:
        if used_chars >= budget["total_cap"]:
            break
        imports = get_imports(repo_path, file_path)
        for imp in imports[:6]:
            match = re.match(r"from ([\w.]+) import", imp)
            if not match:
                continue
            module_path = match.group(1).replace(".", "/") + ".py"
            if module_path in supporting_seen:
                continue
            sup_p = root / module_path
            if not sup_p.is_file():
                continue
            supporting_seen.add(module_path)
            sup_lines = sup_p.read_text(errors="replace").splitlines()
            syms = get_symbols(ts_index, module_path)
            remaining = budget["supporting"] - sum(
                len(c["excerpt"]) for c in file_contexts["supporting"]
            )
            if remaining <= 0:
                break
            excerpt = "\n".join(f"{i+1}: {l}" for i, l in enumerate(sup_lines[:100]))
            file_contexts["supporting"].append({
                "file": module_path,
                "symbols": [s["name"] for s in syms],
                "excerpt": excerpt[:remaining],
                "reason": "import_of_primary",
            })
            used_chars += len(file_contexts["supporting"][-1]["excerpt"])

    # ── Test files ────────────────────────────────────────────────────────────
    discovered = test_files or find_test_files(repo_path, plan.files)
    for test_path in discovered[:3]:
        if used_chars >= budget["total_cap"]:
            break
        tp = root / test_path
        if not tp.is_file():
            continue
        t_lines = tp.read_text(errors="replace").splitlines()
        remaining = budget["tests"] - sum(len(c["excerpt"]) for c in file_contexts["tests"])
        if remaining <= 0:
            break
        excerpt = "\n".join(f"{i+1}: {l}" for i, l in enumerate(t_lines[:80]))
        file_contexts["tests"].append({"file": test_path, "excerpt": excerpt[:remaining]})
        used_chars += len(file_contexts["tests"][-1]["excerpt"])

    return {
        "instruction": "Generate a unified diff implementing the planner directive below.",
        "planner_directive": {
            "requires_code_change": plan.requires_code_change,
            "summary": plan.summary,
            "files": plan.files,
            "steps": plan.steps,
            "code_spans": plan.code_spans,
        },
        "issue_context": {
            "issue_number": issue.number,
            "title": issue.title,
            "body": issue.body[:4000],
        },
        "file_contexts": file_contexts,
        "allowed_edit_files": plan.files,
        "constraints": {
            "output_format": "unified_diff",
            "rules": [
                "No edits outside allowed_edit_files",
                "No unrelated refactors",
                "Unified diff must parse correctly (--- a/, +++ b/, @@)",
            ],
        },
    }
