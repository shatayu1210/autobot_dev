# Planner Orchestrator & Context Enrichment Plan

## Objective
Implement the deterministic Planner Refinement loop to improve initial plans (GraphRAG matching + LLM) by gathering deeper codebase context (cross-file hops, test file discovery, symbol resolution). Then, implement the Chain of Debate (CoD) where a Critic model verifies the Planner's output against the gathered evidence. Finally, assemble a strict, token-budgeted prompt for the Patcher.

## 1. Planner Refinement Loop (Phase 1)
**File Target**: `autobot_vscode/local_orchestrator/planner_orchestrator.py`

When the initial planner output returns a confidence score below `0.75` (CONFIDENCE_THRESHOLD), the orchestrator evaluates the plan against strict rule-based conditions (Triggers). If any triggers fire, it halts LLM execution and launches a deterministic research loop.

**Trigger Conditions:**
- `sparse_files`: Fired if no files are predicted, or only `__init__.py` files are found.
- `path_not_found`: Fired if a predicted file path does not physically exist in the local repo tree.
- `no_code_change_flagged`: Fired if the planner hallucinates that no code change is required for a bug.
- `novel_issue`: Fired if GraphRAG returns 0 candidate files (meaning no historically similar issues exist).

**Research Loop Sequence (Maximum 15 Steps Budget):**
- **Step 1: Keyword Grep Searches**: (Condition: `sparse_files`, `path_not_found`, or `novel_issue`) Fallback to `keyword_search()` over the codebase using regex extracted from the issue body.
- **Step 2: Tree-sitter Extraction**: Extract structural symbols (`get_symbols()`) for identified candidate files.
- **Step 3: Cross-File Dependency Hops**:
  - `get_imports()`: (Condition: `sparse_files` or `novel_issue`) Follow import statements in candidate files to discover upstream dependencies.
- **Step 4: GraphRAG Neighbors**: Query GraphRAG to find historically co-modified neighbors of the target files.
- **Step 5: Wider GraphRAG Fallback**: (Condition: `novel_issue`) Pivot GraphRAG. Instead of comparing issue semantic embeddings, query Neo4j for files historically co-modified alongside the grep hits from Step 1.

*Note on Scoring:* The internal deterministic scorer produces a float 0.0–1.0 used only to guide loop decisions. The UI exposes this as **Evidence Strength: Low / Medium / High**.
- Minimum baseline is `0.60` (model answered, no files found). `+0.15` for all files verified on disk. `+0.20` for 1–6 files cited (signals specificity, not correctness). `+0.15` for GraphRAG overlap. The YES/NO flag adds no scoring bonus since there is no ground truth on open issues.
- Thresholds: `< 0.65` = Low, `0.65–0.74` = Medium, `>= 0.75` = High.

*Note on Iterations:* This loop repeats until Evidence Strength reaches `High (>= 0.75)`, the score plateaus (`delta < 0.05`), or it hits `MAX_REFINEMENT_ITERATIONS = 5`.

## 2. Evidence Compression & Intersection Scoring (Phase 2)
**File Target**: `autobot_vscode/local_orchestrator/planner_orchestrator.py`
- **Action**: Implement a Multi-Keyword Intersection Scorer to replace naive `grep`.
  - Pass 1: Gather candidate files containing at least one issue keyword.
  - Pass 2: Score them locally in Python. `+10` for each unique keyword match, `+5` for path relevance based on the issue domain.
  - Pass 3: Sort by score and take the top `DELTA_PACK_SNIPPETS` (default 10).
- **Token Budgeting (6,000 char max)**: 
  - To prevent "Lost in the Middle" syndrome on 7B models, we strictly cap at 10 snippets.
  - Each snippet is allotted `540` characters of code.
  - `60` characters are reserved for a Tree-sitter spatial header (e.g., `[Snippet from function: X in file: Y]`).
  - Total: `600` chars * 10 snippets = 6,000 chars (~1,500 tokens).

## 3. Chain of Debate / Planner-Critic (Phase 3)
**File Target**: `autobot_vscode/local_orchestrator/planner_orchestrator.py`
- **Action**: Implement a dedicated Critic evaluation step *inside* the refinement loop. The Critic evaluates **all** drafted plans (both YES and NO predictions), regardless of Evidence Strength score. The structural score only controls the *research loop*; the Critic is the semantic gate that decides if the plan is actually correct.
- **Action**: The Critic acts as an objective Verifier. It does *not* blindly take the opposite stance. It receives the Planner's draft, the issue, the research snippets, and the `PREVIOUS_SEARCH_TERMS`. It explicitly checks for hallucinations (e.g., picking files not in context) or omissions (e.g., predicting NO CHANGE when the context clearly shows a bug).
- **False Positive Guard**: Even if a plan scores `High` (>= 0.75) because a file exists on disk, the Critic may still reject it if that file is semantically irrelevant to the issue.
- **Steering & Feedback Injection**: 
  - If the Critic identifies an error, its reasoning is appended to the Planner's prompt for the next iteration (`CRITIC FEEDBACK: ...`).
  - If the Critic determines the retrieved snippets are irrelevant, it outputs `NEW_SEARCH_TERMS: [...]`. The orchestrator intercepts this and uses it to drive the Intersection Scorer in the next iteration.
- **Termination**: The debate terminates when the Critic outputs `CRITIC_DECISION: APPROVED`, or the loop hits `MAX_REFINEMENT_ITERATIONS = 5`. The simple asynchronous feedback loop (Critic critiques -> Planner replans) serves as a robust CoD for 7B models without risking the "agreement spirals" common in single-prompt multi-turn debates.

## 4. Patcher Input Assembly (Phase 4)
**File Target**: `autobot_vscode/local_orchestrator/planner_orchestrator.py`
- **Action**: Implement `assemble_patcher_input()` to construct the exact contract handed to the Patcher model.
- **Budgeting Rules (12,000 char max)**:
  - **Primary Files (6,000)**: Target files to be edited.
  - **Supporting Files (3,000)**: Context-only files (upstream imports or downstream callers). Truncate to top 100 lines + list of Tree-sitter symbols.
  - **Test Files (1,500)**: Discover test equivalents using `find_test_files()` (matching `test_{stem}.py` heuristics). Provide snippets to demonstrate how tests are structured for the modified components.

## 5. API Integration (Phase 5)
**File Target**: `autobot_vscode/local_orchestrator/app.py`
- **Action**: Define the `approve_plan` API endpoint. 
- **Action**: When a user approves the initial plan in VS Code, execute `assemble_patcher_input()` and hand off the context payload to the HF TGI endpoint hosting the Patcher adapter.

## 6. UX & Real-Time Observability (SSE Streaming)
**File Targets**: `autobot_vscode/local_orchestrator/app.py`, `autobot_vscode/media/webview.js`
- **Action**: Use Server-Sent Events (SSE) to surface the backend orchestration loop's "train of thought" dynamically to the VS Code UI via the `/api/orchestrate_stream` endpoint.
- **Telemetry Event Flow**:
  1. `Fetching issue...` (GitHub API retrieval)
  2. `Querying GraphRAG candidates...` (Neo4j Vector similarity match)
  3. `Building planner context...` (Tree-sitter and repo-level mapping)
  4. `Calling Planner (pass-1)...` (Initial LLM inference)
  5. *If Triggers fire*: `Weakness detected ([trigger_name]). Running research loop...`
  6. *Post-Research*: `Re-planning with more evidence (iter [X]/5)...`
  7. *Upon Success/Plateau*: `Plan looks solid! No refinement needed.`
- **Action**: Implement early termination rendering. If `AUTOBOT_STOP_AT=planner` is configured in the `.env` file, the UI explicitly traps the returned `patcher_input` and renders the context within the VS Code chat bubble for inspection.
