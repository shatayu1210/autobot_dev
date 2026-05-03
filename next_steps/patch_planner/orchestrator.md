# Orchestrator Spec — Planner Refinement Loop & Adhoc Queries

> **Scope of this document**: What the orchestrator does *around* the
> Planner model — evidence gathering, plan quality scoring, iterative
> refinement, and handling live user queries. The patcher-orchestrator is
> acknowledged at the boundary (what it receives from the planner side)
> but is not implemented here.

---

## 1. Role Split at a Glance

```
User query / issue number
        │
        ▼
┌─────────────────────┐
│  Planner-Orchestrator│  ← THIS DOCUMENT
│  - fetch issue       │
│  - retrieve context  │
│  - score plan        │
│  - research loop     │
│  - refine plan       │
└────────┬────────────┘
         │ finalized plan (JSON contract below)
         ▼
┌─────────────────────┐
│ Patcher-Orchestrator │  ← separate concern (not implemented here)
│ - assemble code ctx  │
│ - build diff prompt  │
│ - retry/hop on fail  │
└────────┬────────────┘
         │ patcher input pack
         ▼
   Patcher model → unified diff → Critic → ACCEPT/REVISE/REJECT
```

**Short version**: Planner decides *what* to fix and *where*.
Patcher-orchestrator figures out *which exact code* to include.
Patcher writes the diff.

---

## 2. Planner Model — Contract

### What planner receives (input)
- Issue title + body (≤ 4k tokens)
- GraphRAG candidate files (historically linked to similar issues)
- Tree-sitter top-level symbols per candidate file
- Repo file listing (paths only, no content)
- *(On refinement pass)*: delta evidence pack from orchestrator research

### What planner outputs (output schema — run5 compatible, no changes needed)
```json
{
  "requires_code_change": true,
  "files": ["airflow/serialization/serialized_objects.py"],
  "summary": "XCom backend deserialization breaks on custom types",
  "steps": ["Add 'custom_xcom_backend' to ALLOWED_TYPES", "..."],
  "code_spans": [
    { "file": "airflow/serialization/serialized_objects.py",
      "symbol": "BaseSerialization.ALLOWED_TYPES",
      "start_line": 87, "end_line": 95 }
  ]
}
```

### What planner does NOT do
- Does not read full file content (that is patcher-orchestrator's job)
- Does not resolve imports or cross-file hops
- Does not produce a unified diff
- Stays at file-level + intent-level scope

### What patcher-orchestrator receives from planner (the handoff contract)
This is the JSON that flows downstream. Planner populates `planner_directive`;
patcher-orchestrator builds `file_contexts` and `allowed_edit_files` from it.

```json
{
  "planner_directive": {
    "requires_code_change": true,
    "files": ["airflow/serialization/serialized_objects.py"],
    "summary": "...",
    "steps": ["..."],
    "code_spans": [{ "file": "...", "symbol": "...", "start_line": 87, "end_line": 95 }]
  },
  "issue_context": {
    "issue_number": 45123,
    "title": "...",
    "body": "..."
  },
  "file_contexts": {
    "primary": [],      "supporting": [],    "tests": []
  },
  "allowed_edit_files": ["airflow/serialization/serialized_objects.py"]
}
```
> `file_contexts` is empty when it leaves the planner side.
> Patcher-orchestrator fills it using the tiered context policy
> (full file if < 300 lines, else bounded windows; cross-file hops from
> imports/callers/GraphRAG neighbors; relevant test snippets).
> Token budget: primary 6k, supporting 3k, tests 1.5k, total cap 12k.

---

## 3. Planner-Orchestrator — Refinement Loop

This is the core of what we're building. It sits between the planner model
call and the handoff to patcher-orchestrator.

### Trigger conditions (any one → trigger research + second pass)

| Trigger | Detection |
|---|---|
| `requires_code_change = false` on a code-related issue | Issue body contains stack trace / error keyword |
| Sparse or missing file list | `len(files) == 0` OR all files are `__init__.py` / non-Python |
| Path does not exist in repo | Any listed file path not in the actual repo tree |
| Low confidence score | `score_plan()` returns < 0.75 |
| Novel / OOD issue | Zero GraphRAG matches for this issue |
| Malformed JSON from planner | Parse error on first pass |

### Research tools (deterministic — no LLM involved)

```python
keyword_search(repo_root, keywords, top_k=10)
  # Grep for error keywords from issue body across all .py files
  # Returns: list[{file, line, snippet}]

find_file(repo_root, pattern)
  # Glob/partial-name match across repo tree
  # Returns: list[str]  (matching paths)

get_symbols(ts_index, file_path)
  # Top-level classes + functions from pre-built tree-sitter index
  # Returns: list[{name, kind, start_line, end_line}]

read_file_window(repo_root, file_path, start_line, end_line, context=20)
  # Read ±context lines around [start_line, end_line]
  # Returns: str (file excerpt)

get_imports(repo_root, file_path)
  # Parse import statements from a .py file
  # Returns: list[str]  (resolved module paths)

get_callers(repo_root, symbol_name)
  # grep -r "symbol_name(" across repo
  # Returns: list[{file, line, snippet}]

graphrag_neighbors(file_path, top_k=5)
  # Neo4j: MATCH (f:File {path: $path})-[:CO_MODIFIED]->(n) RETURN n.path
  # Returns: list[str]  (co-modified file paths)
```

### Loop configuration

```python
MAX_REFINEMENT_ITERATIONS = 5
CONFIDENCE_THRESHOLD      = 0.75   # stop when plan reaches this
PLATEAU_DELTA             = 0.05   # stop if improvement < this across iterations
MAX_RESEARCH_STEPS        = 15     # total tool calls per iteration
MAX_DEEP_READS_PER_ITER   = 8      # read_file_window calls per iteration
DELTA_PACK_SNIPPETS       = 12     # max evidence snippets fed back to planner
```

### Loop pseudocode

```python
def planner_orchestrator(issue, repo, ts_index, graphrag):
    base_ctx = build_base_context(issue, repo, ts_index, graphrag)
    plan = planner_model(issue, base_ctx)
    confidence = score_plan(plan, repo, graphrag)

    for iteration in range(MAX_REFINEMENT_ITERATIONS):
        if confidence >= CONFIDENCE_THRESHOLD:
            break

        triggers = detect_triggers(plan, repo)
        if not triggers:
            break

        delta = research_loop(
            triggers, issue, repo, ts_index,
            MAX_RESEARCH_STEPS, MAX_DEEP_READS_PER_ITER
        )
        enriched_ctx = base_ctx + compress(delta, DELTA_PACK_SNIPPETS)

        prev_confidence = confidence
        plan = planner_model(issue, enriched_ctx)
        confidence = score_plan(plan, repo, graphrag)

        if abs(confidence - prev_confidence) < PLATEAU_DELTA:
            break   # plateau — further calls won't help

    log_trace(issue.number, plan, confidence, iteration, triggers, delta)
    return plan     # handed to patcher-orchestrator
```

### Confidence scoring (deterministic, no LLM)

```python
def score_plan(plan, repo, graphrag_candidates) -> float:
    score = 0.5
    if plan.get("requires_code_change") is not None:
        score += 0.10
    if plan["files"] and all(file_exists(repo, f) for f in plan["files"]):
        score += 0.15
    if 1 <= len(plan["files"]) <= 6:
        score += 0.10
    if any(s["start_line"] > 0 for s in plan.get("code_spans", [])):
        score += 0.10
    overlap = len(set(plan["files"]) & set(graphrag_candidates))
    score += min(overlap * 0.05, 0.15)
    return min(score, 1.0)
```

### Research loop — how evidence is collected

```python
def research_loop(triggers, issue, repo, ts_index, max_steps, max_reads):
    snippets = []
    steps = 0
    reads = 0

    keywords = extract_keywords(issue.body)  # error names, class names, stack frames

    # Step 1: keyword search for files not in planner's list
    if "sparse_files" in triggers or "path_not_found" in triggers:
        matches = keyword_search(repo, keywords, top_k=10)
        snippets += [f"KEYWORD_MATCH: {m.file}:{m.line}\n{m.snippet}" for m in matches]
        steps += len(matches)

    # Step 2: symbol lookup on candidate files
    for f in (plan["files"] + [m.file for m in matches])[:8]:
        symbols = get_symbols(ts_index, f)
        snippets += [f"SYMBOLS in {f}: {[s.name for s in symbols]}"]
        steps += 1

    # Step 3: read windows around planner's code_spans
    for span in plan.get("code_spans", [])[:max_reads]:
        if reads >= max_reads: break
        window = read_file_window(repo, span["file"], span["start_line"], span["end_line"])
        snippets.append(f"FILE_WINDOW {span['file']}:{span['start_line']}-{span['end_line']}\n{window}")
        reads += 1
        steps += 1

    # Step 4: GraphRAG neighbors of planner's files
    for f in plan["files"][:3]:
        neighbors = graphrag_neighbors(f, top_k=3)
        snippets += [f"GRAPHRAG_NEIGHBOR of {f}: {n}" for n in neighbors]
        steps += 1

    return snippets  # compressed to DELTA_PACK_SNIPPETS before feeding back
```

---

## 4. Logging

Every planner run writes a trace file to:
```
autobot-vscode/local_orchestrator/logs/planner_trace_{issue_number}_{ts}.json
```

```json
{
  "issue_number": 45123,
  "timestamp": "2026-05-01T15:00:00Z",
  "backend": "ollama:qwen2.5-coder:7b",
  "iterations": 2,
  "final_confidence": 0.82,
  "triggers_detected": ["sparse_files", "path_not_found"],
  "research_steps_used": 7,
  "delta_pack_snippets": 4,
  "planner_pass1": { "files": ["airflow/__init__.py"], "confidence": 0.47 },
  "planner_pass2": { "files": ["airflow/serialization/serialized_objects.py"], "confidence": 0.82 }
}
```

Use these to offline-tune `CONFIDENCE_THRESHOLD` and trigger conditions.

---

## 5. Run5 Gap Mapping

| Run5 Gap | Orchestrator Fix |
|---|---|
| File misses on multi-module bugs | Keyword search + graphrag_neighbors adds the missing files to delta pack |
| Novel / OOD issue (zero RAG matches) | Zero-match detection → forces keyword search as primary evidence |
| Ambiguous `NO` on code-related issue | Confidence < 0.6 → refinement pass with stack-trace keyword extraction |
| Wrong file (e.g. `__init__.py`) | path-not-found trigger + get_symbols finds the actual implementation file |

---

## 6. Adhoc User Queries

The orchestrator also serves live user questions typed into the VS Code chat
that are **not** fix/plan requests. These fall into two tiers.

### 6.1 Simple queries — direct GitHub REST API

These are answered with a single (or at most two) GitHub API calls.
No LLM reasoning needed to decide *what to call* — the orchestrator
detects the intent from the text and routes directly.

**Examples and their API calls:**

| User query | GitHub endpoint(s) used |
|---|---|
| "What's the status of issue #66158?" | `GET /issues/66158` |
| "When was issue #45000 created?" | `GET /issues/45000` → `created_at` |
| "Who is assigned to issue #45000?" | `GET /issues/45000` → `assignees` |
| "Who raised issue #45000?" | `GET /issues/45000` → `user.login` |
| "What labels does issue #45000 have?" | `GET /issues/45000` → `labels` |
| "Who closed the PR for issue #45000?" | `GET /issues/45000/timeline` → cross-ref PR → `GET /pulls/{n}` → `merged_by` |
| "Show comments on issue #45000" | `GET /issues/45000/comments` |
| "What files did PR #60000 touch?" | `GET /pulls/60000/files` |
| "What is the CI status for PR #60000?" | `GET /pulls/60000` → head SHA → `GET /commits/{sha}/check-runs` |
| "Show reviews on PR #60000" | `GET /pulls/60000/reviews` |
| "What commits are in PR #60000?" | `GET /pulls/60000/commits` |

All issue and PR numbers in responses are hyperlinked:
```
Issue #45000 → <a href="https://github.com/apache/airflow/issues/45000">#45000</a>
PR #60000    → <a href="https://github.com/apache/airflow/pull/60000">#60000</a>
```

**Implementation note**: The `query` command in `app.py` handles these via
the `GITHUB_TOOLS` registry + LLM tool-call routing (3-pass: plan → execute
→ summarize). The LLM sees the tool list and picks the right one(s). Since
the tool list is fixed and the tools are deterministic Python functions, there
is no hallucination surface beyond intent parsing.

---

### 6.2 Complex queries — GraphRAG multi-hop

These require combining vector similarity search on the ingested Neo4j graph
with one or more GitHub REST calls. The orchestrator executes a fixed Cypher
query pipeline (deterministic), then optionally formats the result with an LLM.

**Query type A: Find similar issues**

> "Are there any issues similar to #45000?"
> "Find top 5 issues similar to #45000"

```
Pipeline:
1. Fetch issue #N body from GitHub (or Neo4j if cached)
2. Neo4j vector similarity search:
   CALL db.index.vector.queryNodes('issue_embeddings', $k, $embedding)
   YIELD node AS issue, score
   WHERE issue.number <> $n
   RETURN issue.number, issue.title, issue.state, score
   ORDER BY score DESC LIMIT $k
3. Format results with similarity score + hyperlinked issue numbers
```

Default k = 5. Response example:
```
Similar to #45000 ("XCom backend deserialization"):
1. #38291 — "Custom XCom serializer breaks on restart" (score: 0.94) → [#38291]
2. #41055 — "Pickling error in custom XCom backend" (score: 0.87) → [#41055]
...
```

---

**Query type B: Who fixed similar issues, what files were involved?**

> "Who fixed issues similar to #45000 and what files did they touch?"

```
Pipeline:
1. Similarity search (same as type A, top-5 issues)
2. For each similar issue: find linked PRs via Neo4j
   MATCH (i:Issue {number: $n})-[:LINKED_PR]->(pr:PR)
   RETURN pr.number, pr.author, pr.merged_at, pr.changed_files
3. Collect unique authors + file paths
4. Format: author → list of files touched → hyperlinked PRs
```

Response example:
```
Issues similar to #45000 were fixed by:
• @dstandish → airflow/serialization/serialized_objects.py (PR #38300)
• @XD-DENG → airflow/models/xcom.py, airflow/utils/xcom_backend.py (PR #41060)
```

---

**Query type C: Who raised similar issues and how long did they take to resolve?**

> "Who raised issues like #45000 in the past and how long did they take to resolve?"

```
Pipeline:
1. Similarity search (top-5)
2. For each similar issue, fetch from Neo4j (or GitHub):
   - user.login (reporter)
   - created_at, closed_at
   - days_to_resolve = (closed_at - created_at).days  [None if still open]
3. Aggregate by reporter → average resolution time
4. Sort by resolution time ascending
```

Response example (with hyperlinks):
```
Similar issues and their resolution times:
• #38291 raised by @bbovenzi — resolved in 12 days → [#38291]
• #41055 raised by @dstandish — resolved in 4 days → [#41055]
• #39100 raised by @XD-DENG — still open → [#39100]

Fastest resolver for similar issues: @dstandish (avg 4 days)
```

---

### 6.3 Implementation notes for complex queries

```python
# Neo4j driver (already available via graphrag setup)
from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))

def graphrag_similar_issues(issue_number, k=5):
    with driver.session() as s:
        # Requires 'issue_embeddings' vector index from vectorize_issues.py
        result = s.run("""
            MATCH (seed:Issue {number: $n})
            CALL db.index.vector.queryNodes('issue_embeddings', $k, seed.embedding)
            YIELD node AS issue, score
            WHERE issue.number <> $n
            RETURN issue.number AS number,
                   issue.title AS title,
                   issue.state AS state,
                   issue.html_url AS url,
                   issue.created_at AS created_at,
                   issue.closed_at AS closed_at,
                   issue.user_login AS reporter,
                   score
            ORDER BY score DESC
        """, n=issue_number, k=k)
        return [dict(r) for r in result]

def graphrag_linked_prs(issue_number):
    with driver.session() as s:
        result = s.run("""
            MATCH (i:Issue {number: $n})-[:LINKED_PR]->(pr:PR)
            RETURN pr.number AS pr_number,
                   pr.author AS author,
                   pr.merged_at AS merged_at,
                   pr.html_url AS url
        """, n=issue_number)
        return [dict(r) for r in result]
```

> **Prerequisite**: `vectorize_issues.py` must have been run to build the
> `issue_embeddings` vector index in Neo4j. Without it, similarity search
> falls back to a keyword-based Cypher full-text search.

```cypher
-- Fallback (no embeddings): full-text search
CALL db.index.fulltext.queryNodes('issue_text', $query)
YIELD node, score
RETURN node.number, node.title, score LIMIT $k
```

---

## 7. Why Direct API Calls Instead of MCP

MCP (Model Context Protocol) is a protocol for *exposing* tools to LLM
clients so they can discover and call them without coupling. It is valuable
when you are building a tool server that will be consumed by **multiple
different LLM clients** (Claude Desktop, Cursor, custom agents) without
code changes.

Our system does not need MCP because:

1. **Single consumer**: The AutoBot orchestrator is the only client. There
   is no need for a discovery protocol — we know exactly what tools exist.

2. **Tools are plain Python functions**: `gh_get_issue(n)`,
   `graphrag_similar_issues(n)` etc. are already in the same process as
   the orchestrator. Adding MCP transport (stdio/SSE to a separate process)
   would introduce an extra network hop, a new process to manage, and
   a new failure surface — for zero benefit.

3. **Tool selection is LLM function-calling, not MCP**: The LLM reads a
   tool registry string in its system prompt and outputs a JSON plan
   (`{"calls": [{"tool": "get_pr_ci_status", "args": {...}}]}`). Your
   Python code then calls the actual function. This is identical to
   OpenAI function calling and Google ADK tool use — none of them require
   MCP either.

4. **Determinism over hallucination surface**: MCP exposes tool schemas
   that the LLM must reason about at runtime. Our approach bakes the tool
   list into the system prompt, caps tool calls at 5 per query, and executes
   them as pure Python — no ambiguity in how the tool is invoked.

5. **MCP adds value when**: you want third-party tools to plug in without
   code changes, or you want Claude Desktop / another agent to use your
   tools. The Slack MCP server (`slackbot/mcp_server.py`) does exactly this
   for the Slack integration — a legitimate use case.

**One-line answer for the professor**: MCP is a tool-*discovery* protocol
for multi-client ecosystems; our orchestrator is a single-consumer system
where tools are Python functions in the same process, making MCP an
unnecessary indirection layer — the same intelligence (LLM JSON function
calling) is achieved with less complexity and more determinism.

---

## 8. Framework Decision — Why Pure Python (not NeMo / LangGraph)

**NeMo (NVIDIA Neural Modules)** is NVIDIA's framework for training and
fine-tuning large models on GPU clusters (Megatron-LM sharding, mixed
precision, multi-node). It's the right tool for model training, not for
building an inference-time orchestration loop. **NeMo Guardrails** is a
sub-project that adds programmable safety rails (written in Colang DSL) to
LLM chat apps — useful for topic fencing and compliance, but not designed
for a bounded code-research loop with deterministic tools.

**For the research loop specifically:**

| What we need | NeMo Guardrails | LangGraph | Pure Python |
|---|---|---|---|
| Bounded iteration (max N steps) | ❌ not its model | ⚠️ works but heavy | ✅ `for i in range(N)` |
| Deterministic tool calls | ❌ LLM-driven | ⚠️ tool nodes | ✅ direct function call |
| Score-gated early exit | ❌ | ⚠️ conditional edge | ✅ `if score >= T: break` |
| Plateau detection | ❌ | ❌ | ✅ `if delta < 0.05: break` |
| Works with Ollama + Gemini | ⚠️ adapter needed | ✅ | ✅ already wired |
| Per-step JSON trace | ❌ | ⚠️ LangSmith | ✅ `json.dump(trace)` |
| Zero new dependencies | ❌ heavy | ❌ heavy | ✅ |

**Decision**: pure Python. The loop is deterministic, bounded, and has no
need for a framework. Every tool is already a Python function in the same
process. We keep NeMo Guardrails as a future option only if we need topic
fencing on the adhoc query path (a tight system prompt achieves the same
effect for now).

---

## 9. File Layout

```
autobot-vscode/local_orchestrator/
  app.py                        ← FastAPI server (existing)
  planner_orchestrator.py       ← NEW: refinement loop (Section 10)
  graphrag_client.py            ← NEW: Neo4j queries for complex adhoc (Section 11)
  logs/                         ← auto-created at startup
    planner_trace_{n}_{ts}.json
  requirements.txt              ← add: neo4j (for graphrag_client)
```

---

## 10. Implementation Spec — `planner_orchestrator.py`

### Data structures

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    code_spans: list[dict]   # {file, symbol, start_line, end_line}
    raw: dict = field(default_factory=dict)

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
```

### Constants

```python
MAX_REFINEMENT_ITERATIONS = 5
CONFIDENCE_THRESHOLD      = 0.75
PLATEAU_DELTA             = 0.05
MAX_RESEARCH_STEPS        = 15
MAX_DEEP_READS_PER_ITER   = 8
DELTA_PACK_SNIPPETS       = 12
```

### Public entry point (called from `app.py`)

```python
def run_planner_with_refinement(
    chat_fn: Callable[[str, str], str],
    issue: Issue,
    repo_path: str,
    ts_index_path: str,
    graphrag_candidates: list[str],   # from graphrag_client.get_candidate_files()
    backend: str,
) -> tuple[PlannerPlan, OrchestratorTrace]:
    """
    Run planner model, detect weaknesses, optionally research + retry.
    Returns finalized plan and a trace for logging.
    """
```

### `detect_triggers(plan, repo_path) -> list[str]`

```python
# Returns list of trigger names (empty = no refinement needed)
def detect_triggers(plan: PlannerPlan, repo_path: str) -> list[str]:
    triggers = []
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

    if not plan.code_spans:
        triggers.append("no_code_spans")

    # Caller sets this based on graphrag_candidates being empty
    return triggers
```

### `score_plan(plan, repo_path, graphrag_candidates) -> float`

```python
def score_plan(
    plan: PlannerPlan,
    repo_path: str,
    graphrag_candidates: list[str],
) -> float:
    score = 0.5
    root = Path(repo_path)

    if plan.requires_code_change is not None:
        score += 0.10

    valid_files = [f for f in plan.files if (root / f).exists()]
    if plan.files and len(valid_files) == len(plan.files):
        score += 0.15
    elif valid_files:
        score += 0.07   # partial credit

    if 1 <= len(plan.files) <= 6:
        score += 0.10

    if any(s.get("start_line", 0) > 0 for s in plan.code_spans):
        score += 0.10

    overlap = len(set(plan.files) & set(graphrag_candidates))
    score += min(overlap * 0.05, 0.15)

    return min(round(score, 3), 1.0)
```

### `research_loop(triggers, issue, plan, repo_path, ts_index) -> list[str]`

```python
def research_loop(
    triggers: list[str],
    issue: Issue,
    plan: PlannerPlan,
    repo_path: str,
    ts_index: dict,          # loaded from treesitter_index.json
    max_steps: int = MAX_RESEARCH_STEPS,
    max_reads: int = MAX_DEEP_READS_PER_ITER,
) -> list[str]:
    """
    Runs deterministic research tools to build a delta evidence pack.
    All calls are pure Python — no LLM involved in this function.
    Returns list of evidence snippet strings.
    """
    snippets: list[str] = []
    steps = 0
    reads = 0

    # Extract keywords from issue body (error names, module names, class names)
    keywords = _extract_keywords(issue.body)

    # 1. Keyword grep search across repo
    if "sparse_files" in triggers or "path_not_found" in triggers:
        matches = keyword_search(repo_path, keywords, top_k=10)
        for m in matches:
            snippets.append(f"KEYWORD_MATCH {m['file']}:{m['line']}\n{m['snippet']}")
        steps += len(matches)

    if steps >= max_steps:
        return snippets

    # 2. Get tree-sitter symbols for each candidate file
    candidate_files = list(dict.fromkeys(
        plan.files + [m['file'] for m in (matches if "sparse_files" in triggers else [])]
    ))[:8]
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
            repo_path, span["file"], span["start_line"], span["end_line"]
        )
        if window:
            snippets.append(
                f"FILE_WINDOW {span['file']}:{span['start_line']}-{span['end_line']}\n{window}"
            )
        reads += 1
        steps += 1

    # 4. GraphRAG neighbors of planner's files (Neo4j call)
    from graphrag_client import get_neighbor_files
    for f in plan.files[:3]:
        if steps >= max_steps:
            break
        neighbors = get_neighbor_files(f, top_k=3)
        snippets.append(f"GRAPHRAG_NEIGHBORS of {f}: {neighbors}")
        steps += 1

    return snippets
```

### Tool helpers (same file)

```python
import re, subprocess
from pathlib import Path

def keyword_search(repo_root: str, keywords: list[str], top_k: int = 10) -> list[dict]:
    """grep -rn keywords across .py files; returns [{file, line, snippet}]."""
    if not keywords:
        return []
    pattern = "|".join(re.escape(k) for k in keywords[:8])
    try:
        out = subprocess.run(
            ["grep", "-rn", "--include=*.py", "-m", "3", "-E", pattern, repo_root],
            capture_output=True, text=True, timeout=15
        ).stdout
    except Exception:
        return []
    results = []
    for line in out.splitlines()[:top_k * 3]:
        parts = line.split(":", 2)
        if len(parts) >= 3:
            results.append({"file": parts[0].replace(repo_root, "").lstrip("/"),
                            "line": parts[1], "snippet": parts[2][:120]})
    # deduplicate by file, keep top_k
    seen, deduped = set(), []
    for r in results:
        if r["file"] not in seen:
            seen.add(r["file"]); deduped.append(r)
    return deduped[:top_k]

def find_file(repo_root: str, pattern: str) -> list[str]:
    """Glob match across repo tree."""
    root = Path(repo_root)
    return [str(p.relative_to(root)) for p in root.rglob(f"*{pattern}*")
            if p.is_file() and ".venv" not in str(p)][:10]

def get_symbols(ts_index: dict, file_path: str) -> list[dict]:
    """Return top-level symbols for file_path from pre-built tree-sitter index."""
    return ts_index.get(file_path, [])

def read_file_window(repo_root: str, file_path: str,
                     start_line: int, end_line: int, context: int = 20) -> str:
    p = Path(repo_root) / file_path
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(errors="replace").splitlines()
        lo = max(0, start_line - context - 1)
        hi = min(len(lines), end_line + context)
        numbered = [f"{i+lo+1}: {l}" for i, l in enumerate(lines[lo:hi])]
        return "\n".join(numbered)
    except OSError:
        return ""

def get_imports(repo_root: str, file_path: str) -> list[str]:
    p = Path(repo_root) / file_path
    if not p.is_file():
        return []
    imports = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("from ") or line.startswith("import "):
            imports.append(line)
    return imports[:30]

def _extract_keywords(body: str) -> list[str]:
    """Pull error names, CamelCase class names, and airflow module names from issue body."""
    words = re.findall(r'[A-Z][a-zA-Z0-9]{3,}|airflow\.\S+|Error\w*|Exception\w*', body)
    return list(dict.fromkeys(words))[:12]

def compress_delta(snippets: list[str], max_count: int = DELTA_PACK_SNIPPETS) -> str:
    """Truncate and join evidence snippets into a single string for the LLM context."""
    selected = snippets[:max_count]
    return "\n\n---EVIDENCE---\n".join(s[:600] for s in selected)

def log_trace(trace: OrchestratorTrace, logs_dir: str = "logs") -> None:
    import json, datetime
    Path(logs_dir).mkdir(exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    path = Path(logs_dir) / f"planner_trace_{trace.issue_number}_{ts}.json"
    path.write_text(json.dumps(vars(trace), indent=2, default=str))
```

---

## 11. Implementation Spec — `graphrag_client.py`

```python
"""
Neo4j client for GraphRAG queries used by:
  - planner_orchestrator (candidate files, neighbor files)
  - app.py query command (similar issues, linked PRs)

Requires: pip install neo4j
Neo4j must be running: cd graphrag && docker compose up -d
"""
from __future__ import annotations
import os
from neo4j import GraphDatabase

NEO4J_URI  = os.environ.get("NEO4J_URI",  "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("NEO4J_PASS", "password")

_driver = None

def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    return _driver

def neo4j_available() -> bool:
    try:
        _get_driver().verify_connectivity()
        return True
    except Exception:
        return False

# ── Planner-orchestrator tools ─────────────────────────────────────────────

def get_candidate_files(issue_number: int, top_k: int = 10) -> list[str]:
    """Files historically touched by PRs linked to issues similar to this one."""
    with _get_driver().session() as s:
        result = s.run("""
            MATCH (i:Issue {number: $n})-[:LINKED_PR]->(pr:PR)-[:TOUCHES]->(f:File)
            RETURN f.path AS path, count(*) AS freq
            ORDER BY freq DESC LIMIT $k
        """, n=issue_number, k=top_k)
        return [r["path"] for r in result]

def get_neighbor_files(file_path: str, top_k: int = 5) -> list[str]:
    """Files historically co-modified with this file."""
    with _get_driver().session() as s:
        result = s.run("""
            MATCH (:File {path: $p})<-[:TOUCHES]-(pr:PR)-[:TOUCHES]->(other:File)
            WHERE other.path <> $p
            RETURN other.path AS path, count(*) AS freq
            ORDER BY freq DESC LIMIT $k
        """, p=file_path, k=top_k)
        return [r["path"] for r in result]

# ── Adhoc query tools ──────────────────────────────────────────────────────

def similar_issues(issue_number: int, k: int = 5) -> list[dict]:
    """
    Vector similarity search. Requires 'issue_embeddings' index from vectorize_issues.py.
    Falls back to fulltext search if index missing.
    """
    with _get_driver().session() as s:
        try:
            result = s.run("""
                MATCH (seed:Issue {number: $n})
                CALL db.index.vector.queryNodes('issue_embeddings', $k, seed.embedding)
                YIELD node AS issue, score
                WHERE issue.number <> $n
                RETURN issue.number AS number, issue.title AS title,
                       issue.state AS state, issue.html_url AS url,
                       issue.created_at AS created_at, issue.closed_at AS closed_at,
                       issue.user_login AS reporter, score
                ORDER BY score DESC
            """, n=issue_number, k=k)
        except Exception:
            # Fallback: fulltext (no embeddings)
            result = s.run("""
                MATCH (seed:Issue {number: $n})
                WITH seed.title + ' ' + coalesce(seed.body, '') AS query
                CALL db.index.fulltext.queryNodes('issue_text', query)
                YIELD node AS issue, score
                WHERE issue.number <> $n
                RETURN issue.number AS number, issue.title AS title,
                       issue.state AS state, issue.html_url AS url,
                       issue.created_at AS created_at, issue.closed_at AS closed_at,
                       issue.user_login AS reporter, score
                ORDER BY score DESC LIMIT $k
            """, n=issue_number, k=k)
        rows = [dict(r) for r in result]
        # Compute days_to_resolve
        for r in rows:
            if r.get("created_at") and r.get("closed_at"):
                from datetime import datetime
                try:
                    c = datetime.fromisoformat(r["created_at"].replace("Z",""))
                    cl = datetime.fromisoformat(r["closed_at"].replace("Z",""))
                    r["days_to_resolve"] = (cl - c).days
                except Exception:
                    r["days_to_resolve"] = None
            else:
                r["days_to_resolve"] = None
        return rows

def linked_prs_for_issues(issue_numbers: list[int]) -> list[dict]:
    """Return PRs linked to any of the given issue numbers."""
    with _get_driver().session() as s:
        result = s.run("""
            MATCH (i:Issue)-[:LINKED_PR]->(pr:PR)
            WHERE i.number IN $nums
            RETURN i.number AS issue_number, pr.number AS pr_number,
                   pr.author AS author, pr.merged_at AS merged_at,
                   pr.html_url AS pr_url, pr.changed_files AS changed_files
        """, nums=issue_numbers)
        return [dict(r) for r in result]
```

---

## 12. Wiring into `app.py` — `plan_patch` command

The existing `plan_patch` command currently calls `llm_plan()` directly.
Replace it with `run_planner_with_refinement()` from `planner_orchestrator`:

```python
# In app.py — plan_patch branch, after title/body fetch:

from planner_orchestrator import run_planner_with_refinement, Issue as OrcIssue
from graphrag_client import get_candidate_files, neo4j_available
import json

# Load tree-sitter index once at startup
TS_INDEX_PATH = os.environ.get("TS_INDEX_PATH", "")
_ts_index: dict = {}
if TS_INDEX_PATH and Path(TS_INDEX_PATH).is_file():
    _ts_index = json.loads(Path(TS_INDEX_PATH).read_text())

# In plan_patch handler:
issue_obj = OrcIssue(number=n_int, title=title, body=body)
graphrag_candidates = get_candidate_files(n_int) if neo4j_available() else []

plan, trace = run_planner_with_refinement(
    chat_fn=chat_fn,          # google_ai_chat / ollama_chat / vertex_chat
    issue=issue_obj,
    repo_path=repo,
    ts_index_path=TS_INDEX_PATH,
    graphrag_candidates=graphrag_candidates,
    backend=backend_label,
)
log_trace(trace)              # writes to logs/
return plan.raw               # same JSON shape as before — VS Code UI unchanged
```

Add `TS_INDEX_PATH` to `.env.example`:
```
TS_INDEX_PATH=/path/to/tree_sitter/treesitter_index.json
```

Add `neo4j` to `requirements.txt`.

---

## 13. Wiring Complex Adhoc Queries into `app.py`

Add `graphrag_similar_issues` and `graphrag_linked_prs` to `GITHUB_TOOLS`
in `app.py` so the LLM tool-calling loop can dispatch to them:

```python
# After existing GITHUB_TOOLS dict:
from graphrag_client import similar_issues, linked_prs_for_issues, neo4j_available

if neo4j_available():
    GITHUB_TOOLS["graphrag_similar_issues"] = (
        lambda issue_number, k=5: similar_issues(issue_number, k),
        "Find top-K GitHub issues similar to a given issue number using GraphRAG vector search. Args: issue_number (int), k (int, default 5)"
    )
    GITHUB_TOOLS["graphrag_linked_prs"] = (
        lambda issue_numbers: linked_prs_for_issues(issue_numbers),
        "Find PRs linked to a list of issue numbers in the graph. Args: issue_numbers (list[int])"
    )
```

**Hyperlink formatting** — add a post-processing step in `llm_adhoc_query()`
before returning the answer:

```python
import re as _re

def _hyperlink_refs(text: str, owner: str, repo: str) -> str:
    """Replace bare #N with HTML anchors for issues and PRs."""
    base = f"https://github.com/{owner}/{repo}"
    text = _re.sub(
        r'(?<!\w)#(\d{3,6})(?!\w)',
        lambda m: f'<a href="{base}/issues/{m.group(1)}">#{m.group(1)}</a>',
        text
    )
    text = _re.sub(
        r'\bPR #(\d{3,6})\b',
        lambda m: f'<a href="{base}/pull/{m.group(1)}">PR #{m.group(1)}</a>',
        text
    )
    return text

# At the end of llm_adhoc_query(), before return:
result["answer"] = _hyperlink_refs(answer, GITHUB_OWNER, GITHUB_REPO)
```

---

## 14. Implementation Order (when green signal given)

```
Step 1: graphrag_client.py        (15 min — Neo4j queries, no app changes)
Step 2: planner_orchestrator.py   (45 min — loop + tools + trace logging)
Step 3: app.py wiring             (20 min — plan_patch + GITHUB_TOOLS + hyperlinks)
Step 4: .env.example + requirements.txt  (5 min)
Step 5: smoke test:
  curl -X POST http://localhost:5000/api/orchestrate \
    -H 'Content-Type: application/json' \
    -d '{"command":"plan_patch","issue_number":45123,"repo_path":"/path/to/airflow"}'
```

Total estimated time: ~90 minutes of focused implementation.
