# Patch Planner → Orchestrator Refinement — Implementation Spec (Post Run5)

## Status

- **Planner adapter**: trained and evaluated (run5). Ready for integration.
- **Patcher adapter**: training in progress (see `training/patch_patcher/`).
- **Critic adapter**: training planned (see `docs/autobot_agentic_plan.md` §6).
- **VS Code plugin**: scaffold exists (`autobot-vscode/`), webview panel working with 4-step button flow.
- **Local orchestrator**: Flask backend at `autobot-vscode/local_orchestrator/app.py` handles `ask_issue`, `plan_patch`, `accept_plan`, `open_pr`.
- **This document**: defines everything remaining to wire the planner adapter + orchestrator research loop + approve gate + patcher handoff into a working demo.

---

## Architecture overview

```
┌──────────────────────────────────────────────────────────┐
│  VS Code Plugin  (autobot-vscode/)                       │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Webview Chat Panel (Conversational UI)             │  │
│  │  User <> Agent dialogue + Inline action buttons    │  │
│  └──────────────┬─────────────────────────────────────┘  │
│                 │ POST /api/orchestrate                   │
└─────────────────┼────────────────────────────────────────┘
                  ▼
┌──────────────────────────────────────────────────────────┐
│  Local Orchestrator  (local_orchestrator/app.py)         │
│  Flask server on localhost:5000                          │
│                                                          │
│  Commands:                                               │
│    ask_issue   → GitHub API fetch                        │
│    plan_patch  → Planner pass-1 → [Refinement loop]     │
│                  → return plan for approval              │
│    approve_plan → user approved → hand off to patcher    │
│    accept_plan → Patcher + Critic loop → return diff     │
│    open_pr     → create PR draft                         │
│                                                          │
│  Tools available (runs on user's machine):               │
│    • VS Code workspace file system (read/search)         │
│    • Tree-sitter index (treesitter_index.json)           │
│    • GraphRAG / Neo4j queries                            │
│    • GitHub REST API (issue, PR, comments)               │
│    • MCP server (slackbot/mcp_server.py) for adhoc       │
│                                                          │
│  LLM Backends:                                           │
│    • Orchestrator LLM (Gemini Flash / Local Ollama)      │
│    • LoRA Adapters via HuggingFace Inference Endpoint    │
└──────────────────────────────────────────────────────────┘
```

### Where things live

| Component | Path | Role |
|---|---|---|
| VS Code extension entry | `autobot-vscode/src/extension.ts` | Registers webview panel + commands |
| Webview panel provider | `autobot-vscode/src/plannerPanelProvider.ts` | HTML/JS webview, message routing |
| Orchestrator HTTP client | `autobot-vscode/src/orchestratorClient.ts` | POST to Flask, auth, timeout |
| Webview UI + buttons | `autobot-vscode/media/webview.js` | Button handlers, state management |
| Local orchestrator (Flask) | `autobot-vscode/local_orchestrator/app.py` | All backend logic, LLM calls |
| Mock orchestrator | `autobot-vscode/mock_orchestrator/app.py` | Stub responses for UI dev |
| Code pipeline (GCP deploy) | `code_pipeline/agents/` | Google ADK agents for cloud deploy |
| LangGraph pipeline | `langgraph_autobot/pipeline/` | Alternative graph-based pipeline |
| Slack MCP server | `slackbot/mcp_server.py` | MCP tools for adhoc GitHub queries |
| Planner training | `training/patch_planner/` | Run5 notebook + data builder |
| Patcher training | `training/patch_patcher/` | Data builder + handover doc |
| Airflow repo checkout | `tree_sitter/airflow/` | Local clone for index + demo |
| Tree-sitter index | `tree_sitter/treesitter_index.json` | Pre-built symbol index |

---

## Run5 conclusions that drive this plan

Reference: `training/patch_planner/run5_results.md`

1. Model grounding is substantially improved over prior runs.
2. Residual failures are concentrated in **recall** (false NO) not hallucination.
3. Many misses are retrieval/context-shaping gaps (path drift, sparse context, indirect evidence).
4. A deterministic orchestrator research loop is the highest-ROI next step.

---

## What needs to be implemented

### 1. New orchestrator command: `plan_patch` with refinement loop

Currently `plan_patch` in `local_orchestrator/app.py` does a single LLM call with shallow repo context (file listing + README snippet). This must be replaced with:

```
plan_patch flow:
  1. Fetch issue from GitHub API
  2. Query GraphRAG for candidate files (narrow top-k)
  3. Fetch tree-sitter spans for candidates
  4. Build planner prompt (system prompt + issue + candidates + spans)
  5. Call planner adapter (pass-1) → get structured output
  6. Run confidence/trigger check on pass-1 output
  7. IF triggers fire:
       a. Orchestrator autonomous research loop (see §3 below)
       b. Build delta evidence pack
       c. Call planner adapter (pass-2) with delta
  8. Return final plan to VS Code for user approval
```

### 2. New orchestrator command: `approve_plan`

A new command that the webview sends when user clicks the Approve button. This separates the plan review from patcher execution.

```
approve_plan flow:
  1. Receive approved plan JSON from webview
  2. For each target file in plan:
     a. Read full file content from workspace (repo_path)
     b. Extract relevant function/class spans via tree-sitter
  3. Build patcher prompt with plan + file contents + spans
  4. Call patcher adapter → get unified diff
  5. Call critic adapter → get verdict
  6. If REVISE: loop patcher↔critic (max 3 iterations)
  7. Return {diff, verdict, feedback} to webview
```

### 3. Orchestrator autonomous research loop (the core new work)

When refinement triggers fire after planner pass-1, the orchestrator must autonomously research the codebase to build better evidence for a pass-2 replan.

#### Research tools the orchestrator uses

Since the orchestrator runs on the user's machine with the Airflow repo checked out in the VS Code workspace, it has direct filesystem access. Implement these as Python functions in `local_orchestrator/app.py`:

| Tool | Implementation | What it does |
|---|---|---|
| `keyword_search(repo_path, keywords, max_results)` | `subprocess.run(["grep", "-rnl", ...])` or `ripgrep` | Search entire repo for keywords extracted from issue + planner reason |
| `find_file(repo_path, filename_pattern)` | `subprocess.run(["find", ...])` or `Path.rglob()` | Locate a file by name/glob anywhere in repo |
| `read_file_lines(file_path, start, end)` | Direct `open()` + slice | Read specific line range of a file |
| `read_file_full(file_path)` | Direct `open()` | Read entire file (capped at 500 lines) |
| `get_treesitter_symbols(file_path)` | Lookup in `treesitter_index.json` | Get all functions/classes defined in a file with line ranges |
| `get_imports(file_path)` | Parse import lines from file or use tree-sitter AST | Find what a file imports and from where |
| `get_callers(symbol_name, repo_path)` | `grep` for `symbol_name(` across repo | Find files that call a specific function/class |
| `summarize_file(file_path, llm_fn)` | Read file → short LLM summary | Produce a 2-3 sentence summary of what a file does |

#### Research loop algorithm

```python
def orchestrator_research(
    pass1_output: dict,
    issue_data: dict,
    repo_path: str,
    treesitter_index: dict,
    llm_fn: Callable,
    max_files_to_read: int = 8,
    max_research_steps: int = 15,
) -> dict:
    """
    Autonomous research loop. Orchestrator decides what to explore
    based on planner pass-1 output and issue context.

    Returns a delta evidence pack for planner pass-2.
    """
    evidence_store = []       # full raw findings
    file_summaries = {}       # path -> summary (orchestrator's scratchpad)
    explored_files = set()
    steps_taken = 0

    # Step 1: Extract search seeds from issue + pass-1 output
    seeds = extract_search_seeds(issue_data, pass1_output)
    # seeds = { keywords: [...], file_hints: [...], symbol_hints: [...] }

    # Step 2: Keyword search across repo
    for kw in seeds["keywords"][:5]:
        hits = keyword_search(repo_path, kw, max_results=10)
        for hit in hits:
            if hit["file"] not in explored_files:
                evidence_store.append({
                    "source": "keyword_search",
                    "keyword": kw,
                    "file": hit["file"],
                    "line": hit["line"],
                    "snippet": hit["context"],
                })
        steps_taken += 1

    # Step 3: Find files by name pattern if issue mentions specific files
    for hint in seeds["file_hints"][:3]:
        matches = find_file(repo_path, hint)
        for m in matches:
            if m not in explored_files:
                evidence_store.append({
                    "source": "find_file",
                    "pattern": hint,
                    "file": m,
                })
        steps_taken += 1

    # Step 4: Deep read top candidate files
    #   Rank evidence_store entries by relevance, pick top files
    ranked_files = rank_candidate_files(evidence_store, seeds)

    for file_path in ranked_files[:max_files_to_read]:
        if steps_taken >= max_research_steps:
            break
        if file_path in explored_files:
            continue
        explored_files.add(file_path)

        # Read file content
        content = read_file_full(os.path.join(repo_path, file_path))

        # Get tree-sitter symbols for this file
        symbols = treesitter_index.get(file_path, [])

        # Summarize what this file does (store for orchestrator reference)
        summary = summarize_file_content(content, llm_fn)
        file_summaries[file_path] = summary

        # Extract relevant spans based on keyword overlap
        relevant_spans = extract_relevant_spans(content, symbols, seeds)

        evidence_store.append({
            "source": "deep_read",
            "file": file_path,
            "summary": summary,
            "symbols": symbols,
            "relevant_spans": relevant_spans,
        })
        steps_taken += 1

    # Step 5: Trace imports/callers for key symbols
    for sym in seeds["symbol_hints"][:3]:
        if steps_taken >= max_research_steps:
            break
        callers = get_callers(sym, repo_path)
        for caller_file in callers[:3]:
            if caller_file not in explored_files:
                evidence_store.append({
                    "source": "caller_trace",
                    "symbol": sym,
                    "caller_file": caller_file,
                })
        steps_taken += 1

    # Step 6: Compress into delta evidence pack
    delta_pack = compress_evidence(
        evidence_store,
        file_summaries,
        max_snippets=12,
        max_excerpt_chars=400,
    )

    return delta_pack
```

#### Evidence compression rules

The orchestrator keeps a full evidence store internally but only sends a compressed delta pack to the planner for pass-2:

- **Max 12 snippets** in the delta pack
- **Each snippet** includes: `file_path`, `symbol` (or "unknown"), `excerpt` (max 400 chars), `reason_tag`, `relevance_score`
- **Reason tags**: `keyword_match`, `import_trace`, `caller_trace`, `structural_match`, `historical_fix`, `novel_finding`
- **Deduplicate** near-identical snippets (same file + overlapping line ranges)
- **Prioritize diversity** across modules over many snippets from one file
- **File summaries** are included as compact 1-liner annotations, not full text

---

## Planner output contract

Based on run5 training, the planner adapter outputs:

```
REQUIRES_CODE_CHANGE: YES | NO
REASON: <1-3 sentence rationale>
PLAN: (only when YES)
  - FILE: <path>
    CHANGE: <what to modify>
  - FILE: <path>
    CHANGE: <what to modify>
```

The orchestrator must parse this into a structured JSON:

```json
{
  "requires_code_change": true,
  "reason": "The DAG serialization logic doesn't handle the new XCom backend...",
  "confidence": "high",
  "files": [
    {
      "path": "airflow/serialization/serialized_objects.py",
      "change": "Add XCom backend type to the serialization allowlist"
    }
  ],
  "refinement_used": false,
  "pass": 1
}
```

### Confidence scoring (computed by orchestrator, not planner)

The orchestrator computes confidence based on deterministic signals from pass-1:

| Signal | Weight | Measurement |
|---|---|---|
| File count in plan | 0.2 | 1-3 files = high, 0 or >5 = low |
| Reason length | 0.15 | >50 chars = high, <20 chars = low |
| File paths exist in repo | 0.3 | All exist = high, any missing = low |
| Tree-sitter symbols found for listed files | 0.2 | Symbols found = high, empty = low |
| GraphRAG candidate overlap with plan files | 0.15 | High overlap = high confidence |

Confidence bands:
- **High** (≥ 0.70): multiple aligned file/symbol matches + non-empty tree-sitter context
- **Medium** (0.40–0.69): partial alignment, some inferred paths
- **Low** (< 0.40): weak/no concrete code evidence

---

## Deterministic refinement triggers

Trigger the research loop when **any** of these are true:

1. **Weak NO**: planner says NO but reason is ≤30 chars or mentions uncertainty words ("might", "unclear", "possibly")
2. **Sparse evidence**: tree-sitter spans for top candidates < 2 useful symbols
3. **Path mismatch**: top GraphRAG historical files and planner target files have < 20% overlap
4. **Missing files**: any file in plan doesn't exist at `repo_path`
5. **Low confidence score**: computed confidence < 0.40
6. **Empty plan on YES**: planner says YES but file list is empty

**Do not always refine.** If none fire, return pass-1 directly. Fast path is the default.

---

## Refinement payload to planner (pass-2 prompt)

When refinement triggers fire and research completes, send a structured delta block to the planner:

```
--- INITIAL DECISION ---
REQUIRES_CODE_CHANGE: {pass1_decision}
REASON: {pass1_reason}
FILES: {pass1_files}
CONFIDENCE: {computed_confidence}

--- NEW EVIDENCE FOUND ---
{compressed delta snippets, max 12}

--- CANDIDATE FILES RERANKED ---
{top 8 candidate files with scores and reason tags}

--- CONFLICTS OR UNCERTAINTIES ---
{any contradictory signals between pass-1 and new evidence}

--- REVISION INSTRUCTION ---
Review the new evidence above. If it provides stronger grounding
for a different decision or different file targets, revise your
output. If the original decision is still best supported, keep it.
Maintain the same output format (REQUIRES_CODE_CHANGE / REASON / PLAN).
```

### Dynamic Refinement Loop
Instead of a strict 2-pass limit, the orchestrator should loop the Planner until one of the following stop conditions is met:
1. **High Confidence Reached**: Computed confidence >= 0.75.
2. **Confidence Plateau**: Confidence score hasn't improved by more than 0.05 over the last 2 iterations.
3. **Hard Cap**: Reached maximum of 5 planner iterations (to prevent infinite loops and manage latency).

---

## VS Code Webview UX (Cursor-like Conversational Interface)

To closely resemble the feel of Cursor or Antigravity, we will migrate from the current static "button-flow" Webview to a **conversational chat interface** within the VS Code sidebar.

### VS Code Extension Capabilities
The VS Code Extension API provides a `WebviewViewProvider` (which you are already using). Inside this Webview, you can render a full React, Vue, or Vanilla JS chat interface. This allows you to:
- Render chat bubbles for the User and the Agent.
- Stream text responses.
- Render interactive UI components (like an "Approve Plan" or "Apply Diff" button) directly inside a chat bubble.
- Show "Thinking..." or "Searching files..." loading states.

### Conversational Flow

Instead of a rigid 4-step button process, the UX becomes a fluid conversation:

**User:**
> "Please fix issue #45123"

**Agent (AutoBot):**
> *[UI Spinner]* Fetching issue details...
> *[UI Spinner]* Planning patch...
> *[UI Spinner]* Researching codebase (Iteration 1)...
>
> I have analyzed issue #45123. Here is the proposed plan:
>
> **Files to modify:**
> 1. `airflow/serialization/serialized_objects.py`
>    → Add XCom backend type to serialization allowlist
>
> **Reason:** The XCom backend serialization needs to handle the new custom backend type added in AIP-72.
>
> [✓ Approve Plan] [✗ Reject & Modify]

**User clicks [✓ Approve Plan]**

**Agent (AutoBot):**
> *[UI Spinner]* Generating unified diff...
> *[UI Spinner]* Critic reviewing diff...
>
> Here is the generated patch. The critic has approved it.
>
> ```diff
> --- a/airflow/serialization/serialized_objects.py
> +++ b/airflow/serialization/serialized_objects.py
> @@ -87,6 +87,7 @@ class BaseSerialization:
>      ALLOWED_TYPES = {
>          "json",
>          "pickle",
> +        "custom_xcom_backend",
>      }
> ```
>
> [Apply to Workspace] [Create PR Draft]

### Implementation Changes Needed in `autobot-vscode`
1. **Frontend Rewrite**: Replace `webview.js` and `webview.html` with a chat UI. You can use standard HTML/CSS or bundle a React app. It needs to maintain an array of "messages" (role: user/assistant) and render them.
2. **Streaming & Tool Indicators**: The Flask orchestrator should stream back Server-Sent Events (SSE) or chunked JSON so the Webview can display real-time actions (e.g., "> Searched repo for 'xcom'").
3. **Interactive Actions**: The `approve_plan` and `open_pr` actions become events triggered by clicking action buttons inside the assistant's chat bubbles.

---

## Patcher handoff contract

When the user approves the plan, the orchestrator builds a patcher prompt. Based on the patcher training format (see `training/patch_patcher/patcher_handover.md`), the handoff includes:

### Patcher input schema

```json
{
  "instruction": "Generate a unified diff implementing this plan.",
  "planner_directive": {
    "requires_code_change": true,
    "reason": "...",
    "files": [
      {"path": "airflow/serialization/serialized_objects.py", "change": "..."}
    ]
  },
  "issue_context": {
    "title": "...",
    "body": "...(truncated to 4000 chars)...",
    "labels": ["bug", "area:serialization"]
  },
  "treesitter_context": {
    "language": "python",
    "spans": [
      {
        "file": "airflow/serialization/serialized_objects.py",
        "symbol": "BaseSerialization",
        "symbol_type": "class",
        "start_line": 45,
        "end_line": 120,
        "excerpt": "... relevant code ..."
      }
    ]
  },
  "constraints": {
    "allowed_files": ["airflow/serialization/serialized_objects.py", "tests/..."],
    "output_format": "unified_diff",
    "rules": [
      "No edits outside allowed files",
      "No unrelated refactors",
      "Unified diff must parse (--- a/, +++ b/, @@)"
    ]
  }
}
```

### Patcher output

The patcher returns **only** a unified diff:

```diff
diff --git a/airflow/serialization/serialized_objects.py b/airflow/serialization/serialized_objects.py
--- a/airflow/serialization/serialized_objects.py
+++ b/airflow/serialization/serialized_objects.py
@@ -87,6 +87,7 @@ class BaseSerialization:
     ALLOWED_TYPES = {
         "json",
         "pickle",
+        "custom_xcom_backend",
     }
```

### Patcher → Critic → Patcher loop

After patcher generates a diff:

1. Critic evaluates: `{verdict: "ACCEPT" | "REVISE" | "REJECT", feedback: "..."}`
2. If `REVISE`: feed feedback back to patcher, regenerate diff (max 3 iterations)
3. If `ACCEPT`: return diff to webview for user review
4. If `REJECT`: return rejection with feedback, suggest re-planning

This loop already exists in `local_orchestrator/app.py` (`llm_patch_and_critic`). The change is to use the fine-tuned patcher adapter instead of generic Gemini/Ollama.

---

## End-to-end flow (complete sequence)

```
User opens VS Code with Airflow repo in workspace
User opens AutoBot panel (sidebar)
User enters issue number (e.g., #45123)

[1 · Load Issue]
  → orchestrator calls GitHub API
  → returns issue title, body, labels, comments
  → displayed in webview contextOut panel

[2 · Plan]
  → orchestrator:
      1. Queries GraphRAG for candidate files
      2. Fetches tree-sitter spans for candidates
      3. Builds planner prompt
      4. Calls planner adapter (pass-1)
      5. Parses output, computes confidence
      6. Checks refinement triggers
      7. IF triggers fire:
           a. Runs autonomous research loop
              - keyword search across repo
              - find files by pattern
              - read candidate files line by line
              - trace imports and callers
              - summarize findings
           b. Compresses evidence into delta pack
           c. Calls planner adapter (pass-2) with delta
      8. Returns structured plan + confidence + metadata
  → displayed in webview with readable formatting

[Approve ✓]  ← user reviews plan and approves
  → webview sends "approve_plan" with plan JSON

  → orchestrator:
      1. Reads target file contents from workspace
      2. Gets tree-sitter spans for target functions
      3. Builds patcher input (plan + file content + spans)
      4. Calls patcher adapter → unified diff
      5. Calls critic adapter → verdict
      6. If REVISE: loop (max 3x)
      7. Returns final diff + verdict
  → diff displayed in webview patchOut panel

[Reject ✗]  ← user decides plan is wrong
  → flow terminates
  → status: "Plan rejected. Re-run planner or modify issue."
  → user can re-enter issue number or modify and re-plan

[3 · Open PR draft]  (only if diff was accepted)
  → orchestrator creates PR via GitHub API
  → returns PR URL
```

---

## Logging and observability

Every planner invocation must persist a trace for debugging and threshold tuning:

```json
{
  "timestamp": "2026-05-01T10:30:00Z",
  "issue_number": 45123,
  "pass1_output": { "decision": "NO", "reason": "...", "files": [] },
  "confidence_score": 0.35,
  "triggers_fired": ["weak_no", "sparse_evidence"],
  "refinement_used": true,
  "research_steps": 8,
  "max_escalation_level": 2,
  "evidence_snippets_selected": 7,
  "pass2_output": { "decision": "YES", "reason": "...", "files": ["..."] },
  "user_approved": true,
  "patcher_iterations": 2,
  "critic_verdict": "ACCEPT",
  "total_latency_seconds": 45.2
}
```

Store logs to: `autobot-vscode/local_orchestrator/logs/` (one JSON per invocation).

---

## Metrics to validate orchestrator value

Track pass-1 vs pass-2 on the run5 test set:

| Metric | What it measures |
|---|---|
| NO→YES correction rate | How often refinement flips a false NO to correct YES |
| Precision delta | Guard against false-positive inflation from refinement |
| Recall lift | Primary target: catch previously missed positives |
| File overlap with gold | Do refined plans target the right files? |
| Latency overhead (p50/p95) | Cost of research loop in seconds |
| Refinement trigger rate | What % of issues trigger refinement |

**Minimum acceptance**: measurable recall gain with precision drop ≤ 5%, latency ≤ 1.5x p50.

---

## Implementation phases

### Phase 1 (now — core demo)

- [ ] Wire planner adapter into `local_orchestrator/app.py` (replace Gemini placeholder)
- [ ] Implement confidence scoring on pass-1 output
- [ ] Implement 5 deterministic trigger rules
- [ ] Implement research tools: `keyword_search`, `find_file`, `read_file_lines`, `get_treesitter_symbols`
- [ ] Implement research loop with `max_research_steps=15`
- [ ] Implement evidence compression (12 snippet cap)
- [ ] Implement pass-2 delta prompt construction
- [ ] Add `approve_plan` command to Flask orchestrator
- [ ] Add Approve/Reject buttons to webview
- [ ] Add plan display formatting in webview
- [ ] Wire patcher adapter (when ready) into `accept_plan` flow
- [ ] Add per-invocation JSON logging

### Phase 2 (post-demo polish)

- [ ] Implement `get_imports`, `get_callers`, `summarize_file` research tools
- [ ] Add deeper GraphRAG traversal (Level 3) for high-ambiguity cases
- [ ] Implement deterministic reranking with composite score
- [ ] Add conflict detection between pass-1 evidence and new findings
- [ ] Extend eval scripts to report pass-1 vs pass-2 deltas on test set
- [ ] Add streaming progress updates to webview during research loop

### Phase 3 (learned optimization)

- [ ] Learn trigger thresholds from Phase 1/2 logs
- [ ] Tune research depth by issue class/severity
- [ ] Add budget-aware dynamic retrieval stopping
- [ ] Add MCP integration for live PR status/CI checks during planning

---

## Non-negotiable constraints

1. Orchestrator enriches planner context; it never overrides planner logic directly
2. **Dynamic Planner Loop** (replaces max 2 calls)
3. Pass-2+ prompt is delta-evidence-only (don't replay pass-1 context)
4. Research loop is bounded: max 15 steps, max 8 files deep-read per iteration
5. User must explicitly approve plan before patcher runs (Approve button inside chat)
6. Every refinement decision is traceable via logs
7. Patcher only touches files listed in the approved plan (constraint enforcement)
8. MCP server is for adhoc user queries only (issue status, assignee, etc.), not for the planner research loop

---

## Open design decisions

1. **Should the orchestrator use the same LLM for file summarization during research?**
   - Recommended: yes, use same Gemini/Ollama backend configured in `AUTOBOT_MODE` for summaries
   - Alternative: use a cheaper/faster model for summarization only

2. **Should Reject offer a "re-plan with feedback" option?**
   - Recommended: yes, let user type a short note that gets appended to the planner prompt on retry

3. **Max latency budget per issue?**
   - Recommended: 60s total for plan (including research), 120s for patcher+critic loop

4. **Should we show research progress in real-time?**
   - Phase 1: no, just show a spinner with "Researching codebase..."
   - Phase 2: stream step-by-step updates ("Searching for 'xcom_backend'...", "Reading serialized_objects.py...")

---

## Deployment architecture (HuggingFace Hub)

To deploy the 3 models (Planner, Patcher, Critic) efficiently, the recommended architecture is **HuggingFace Inference Endpoints** running vLLM or TGI (Text Generation Inference). 

Because all three models share the exact same base model (`Qwen2.5-Coder-7B-Instruct`), you can deploy a **single** endpoint and load the three LoRA adapters dynamically at request time. 

When the Orchestrator makes an HTTP request to the HF endpoint, it simply specifies which adapter to use in the payload:
- `{"model": "autobot-planner-lora", "prompt": "..."}`
- `{"model": "autobot-patcher-lora", "prompt": "..."}`
- `{"model": "autobot-critic-lora", "prompt": "..."}`

This saves massive amounts of VRAM and deployment costs compared to hosting 3 separate base models.
