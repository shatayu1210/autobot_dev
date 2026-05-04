# AutoBot VS Code Orchestrator — Complete Implementation Specification

You are implementing the VS Code Orchestrator for AutoBot, a 3-model agentic patch pipeline. This document is your complete behavioral specification. Follow it exactly.

---

## System overview

The orchestrator coordinates three atomic, stateless fine-tuned models (Planner, Patcher, Critic) served on a single HuggingFace endpoint (Qwen 2.5 Coder 7B with LoRA adapters). The orchestrator owns all retrieval, context assembly, control flow, validation, and retry logic. The models are pure functions: they receive a prompt and produce structured output. They have no memory, no tool access, and no awareness of each other.

The orchestrator also manages a Neo4j GraphRAG database (issue/PR/file/symbol graph with vector similarity), an MCP server (GitHub API access, live file reads), and a local Docker sandbox for patch validation.

---

## Core pipeline sequence

Execute these steps in order for every incoming issue. Do not skip steps. Do not reorder.

### Step 1 — Receive issue

Accept an issue number from the VS Code plugin. Extract the issue's GitHub URL or number.

### Step 2 — Fetch issue context via MCP

Call the MCP server to retrieve from GitHub:
- `issue.title` (string)
- `issue.body` (string, full markdown body)
- `issue.labels` (list of label name strings)
- `issue.comments` (list of comment bodies, optional, for context)

Store these as the `ISSUE_CONTEXT` object. This is the primary input to the entire pipeline.

### Step 3 — Run GraphRAG Level 0 retrieval

This is the fast path. It replaces the monolithic repo-map approach with a targeted graph query.

Execute the following against Neo4j:

```
1. Embed ISSUE_CONTEXT.title + ISSUE_CONTEXT.body using the sentence-transformer model (all-MiniLM-L6-v2).
2. Run ANN vector search on the issue_embedding_index to find top-K=6 most similar historical issues.
3. For each similar issue, traverse: Issue -[RESOLVED_BY]-> PR -[TOUCHES]-> File
4. Aggregate files by frequency across all traversals.
5. Return ranked list of candidate files (top 10 by frequency).
6. For each candidate file, query: File -[DEFINES]-> Symbol to get symbol names and types (class/function).
7. For the top-5 similar issues, also return: issue.title, issue.number, and the resolving PR number + files touched (for the SIMILAR_RESOLUTIONS context block).
```

Store results as:
- `CANDIDATE_FILES`: list of `{path, frequency, source_issues}` ranked by frequency
- `SYMBOL_CONTEXT`: dict of `{file_path: [{name, type, line}]}` for each candidate file
- `SIMILAR_RESOLUTIONS`: list of `{issue_number, issue_title, pr_number, files_touched}` for top-5 similar issues
- `GRAPHRAG_SCORES`: the raw similarity scores from the ANN search (used for confidence scoring in step 5)

Expected latency: 25–50ms.

### Step 4 — Assemble Planner context bundle and call Planner

Build the Planner prompt from these components, in this order:

```
ISSUE_TITLE: {ISSUE_CONTEXT.title}
ISSUE_BODY: {ISSUE_CONTEXT.body}
ISSUE_LABELS: {comma-separated ISSUE_CONTEXT.labels}

CANDIDATE_FILES (from GraphRAG, ranked by historical frequency):
  1. {path} (freq: {n}, from {m} similar issues)
  2. ...
  [up to K=6 files]

SYMBOL_CONTEXT (tree-sitter headers for top candidates):
  {file_path}:
    - {symbol_type} {symbol_name} (line {line_number})
    ...
  [for each candidate file]

SIMILAR_RESOLUTIONS (from GraphRAG):
  - Issue #{number} ("{title}") → resolved by PR #{pr_number}
    touching: {comma-separated files}
  [for top 5 similar issues]
```

Call the Planner model (Qwen 2.5 Coder 7B + Planner LoRA adapter).

The Planner returns structured output:

```
REQUIRES_CODE_CHANGE: YES | NO
REASON: {short natural language justification grounded in evidence}
PLAN:
  - file: {file_path}
    what_to_change: {concise intent, max ~50 words per file}
  [one entry per target file]
```

Store this as `PLANNER_OUTPUT_PASS1`.

### Step 5 — Score confidence and evaluate refinement triggers

Compute a confidence assessment from these signals. This is deterministic logic, not a model call.

**Confidence signals to check (all boolean):**

| Signal | How to check |
|---|---|
| `strong_graphrag` | At least 2 candidate files have frequency ≥ 3, AND top ANN similarity score ≥ 0.75 |
| `multiple_candidates` | Planner cited ≥ 2 target files in its PLAN |
| `symbols_found` | SYMBOL_CONTEXT is non-empty for at least 1 planned file |
| `plan_grounded` | Every file in PLAN appears in CANDIDATE_FILES or SYMBOL_CONTEXT (no hallucinated paths) |
| `decision_consistent` | If REQUIRES_CODE_CHANGE=YES, at least 1 file is listed in PLAN. If NO, PLAN is empty. |

**Confidence classification:**
- HIGH: all 5 signals are true
- MEDIUM: 3–4 signals are true
- LOW: ≤ 2 signals are true

**Refinement trigger rules (check all 5, trigger if ANY is true):**

1. Planner outputs NO, but `strong_graphrag` is false (weak/no concrete file evidence supporting the NO).
2. Planner cites only 1 candidate file AND `strong_graphrag` is false.
3. CANDIDATE_FILES has ≤ 2 entries AND SYMBOL_CONTEXT is empty or near-empty for those files.
4. Issue title + body contains strong code-change language (mentions "bug", "fix", "error", "crash", "regression", "broken", specific function/class names) but Planner said NO.
5. Planner's cited files do NOT exist in CANDIDATE_FILES (path mismatch / hallucination indicator).

**Decision routing:**

- If confidence = HIGH and no triggers fired → go to Step 7 (HITL).
- If confidence = MEDIUM or LOW, OR any trigger fired → go to Step 6 (refinement).
- If REQUIRES_CODE_CHANGE = NO and confidence = HIGH and no triggers → run a single cheap guard check: verify that none of the top-3 GraphRAG candidate files have suspiciously high similarity scores (≥ 0.85). If they do, trigger refinement anyway. Otherwise, accept the NO and go to Step 7 with the NO decision.

### Step 6 — Run refinement (single retry, bounded)

Refinement is NOT open-ended exploration. It is a bounded, deterministic escalation through retrieval levels. Execute levels in order. Stop as soon as you have sufficient evidence.

**Level 1 — Cheap expansion (always run if refinement triggered)**

Execute:
1. Widen GraphRAG query: increase top-K from 6 to 12. Include second-hop neighbors (files touched by PRs that also touched the Level 0 candidate files).
2. Run keyword/symbol search against Neo4j: extract the top 5 distinctive nouns/identifiers from ISSUE_CONTEXT.body, query them against File.path and Symbol.name.
3. If the VS Code workspace is available, list files in directories matching the top candidate file paths (sibling files in the same package/module).

Budget caps: max 20 additional candidate files from Level 1 total.

Store new findings as `LEVEL1_NEW_FILES` and `LEVEL1_NEW_SYMBOLS`.

**Level 2 — Precision verification (run only if Level 1 found plausible new targets)**

Execute:
1. For the top 3–8 candidate files (merged from Level 0 + Level 1, re-ranked by combined frequency + keyword match score), fetch actual file contents via MCP / VS Code file reads.
2. Run tree-sitter on each fetched file. Extract function/class bodies around matched symbols. Bound to: max 50 lines per symbol, max 3 symbols per file, max 8 files total.
3. If a suspected symbol is imported from another file, trace one hop to find the definition site. Add that file to candidates if not already present.

Budget caps: max 8 files read, max 400 lines of code total across all files, max 24 symbols extracted.

Store results as `LEVEL2_CODE_SPANS` (list of `{file_path, symbol_name, start_line, end_line, code_text}`).

**Level 3 — Deep fallback (run ONLY if Level 1+2 produced near-zero useful results AND issue appears high-value)**

Execute:
1. Deeper GraphRAG traversal: add PR-review evidence hops (File ← TOUCHED_BY ← PR ← has review with CHANGES_REQUESTED), related file clusters.
2. Cross-check with historical patch idioms for the issue's label category.
3. Bounded directory-level file scan: list all files in the top 3 suspect directories, filtered to relevant language extensions.

Budget caps: max 3 additional directory scans, max 50 files listed, max 5 new files read.

This level is rare. Most issues resolve at Level 0–1.

**Build the refinement bundle**

Assemble a structured delta block for the Planner's second pass:

```
INITIAL_DECISION_SUMMARY:
  Decision: {PLANNER_OUTPUT_PASS1.REQUIRES_CODE_CHANGE}
  Files cited: {list}
  Confidence: {HIGH|MEDIUM|LOW}
  Triggers fired: {list of trigger descriptions}

NEW_EVIDENCE_FOUND:
  New candidate files from expanded search:
    - {file_path} (source: {keyword_match|second_hop|directory_scan})
  New symbols discovered:
    - {symbol_name} in {file_path} (type: {class|function})
  [only include genuinely new findings, not duplicates of Level 0]

CANDIDATE_FILES_RERANKED:
  1. {file_path} (combined score: {n}, sources: graphrag + keyword + code_read)
  2. ...
  [merged and re-ranked list of all candidate files across all levels]

CODE_EVIDENCE (if Level 2 ran):
  {file_path}, lines {start}-{end}:
    {code_text excerpt}
  [only the most relevant spans, max 3]

CONFLICTS_OR_UNCERTAINTIES:
  {any contradictory evidence between Level 0 and expanded search}

REVISION_INSTRUCTION:
  You previously decided {YES|NO}. New evidence has been gathered.
  Revise your decision ONLY if the new evidence provides stronger
  grounding than your initial assessment. Do not change your decision
  simply because more files were found — change it only if the evidence
  clearly supports a different conclusion.
```

**Call Planner pass-2**

Prepend the original Planner prompt (from Step 4) with the refinement bundle above. Call the Planner model again (same adapter).

Store result as `PLANNER_OUTPUT_PASS2`.

Validate pass-2 output:
- Schema check: does it contain REQUIRES_CODE_CHANGE, REASON, and PLAN fields?
- Grounding check: are all cited files present in the merged candidate list?
- If validation fails, use pass-1 output instead and flag for HITL with a warning.

Use `PLANNER_OUTPUT_PASS2` as the final plan going forward (or pass-1 if pass-2 failed validation).

**Do not run more than one refinement round.** If pass-2 is still ambiguous, present it to the human as-is with confidence metadata.

### Step 7 — Present plan for HITL approval

Send the following to the VS Code plugin for developer review:

```
DECISION: {YES|NO}
CONFIDENCE: {HIGH|MEDIUM|LOW}
REFINEMENT_APPLIED: {true|false}
REFINEMENT_CHANGED_DECISION: {true|false|n/a}

TARGET_FILES:
  - {file_path}: {what_to_change}
  [for each file in PLAN]

EVIDENCE_SUMMARY:
  Similar issues: {list of issue numbers + titles}
  Historical files touched: {top candidate files with frequencies}
  Confidence signals: {which passed, which failed}

REASON: {Planner's natural language justification}
```

Wait for developer response:
- **Approve**: proceed to Step 8 with the plan as-is.
- **Modify**: developer edits the file list or intents. Use their modified plan.
- **Reject**: stop the pipeline. Log the rejection reason.

### Step 8 — Fetch file contents and prepare Patcher context

For each file in the approved plan, execute:

1. **Fetch file content** via MCP (GitHub API or local workspace file read).
2. **Truncate to relevant span**: use tree-sitter to parse the fetched file. Find the function/class that matches the Planner's intent (by symbol name or line proximity). Extract that symbol's body plus 20 lines before and 20 lines after. If tree-sitter parsing fails or no matching symbol is found, use a window of ±50 lines around the first occurrence of the most relevant keyword from the intent text.
3. **Fetch historical idioms** from Neo4j: query `File ← TOUCHED_BY ← PR` for the target file. Return the top 3–5 most recent merged PRs' body text and commit messages. These show how past developers idiomatically modified this file.
4. **Fetch GraphRAG PR context**: query past CI outcomes and review patterns for this file. Return `{ci_pass_rate, common_review_feedback}`.

Assemble the Patcher context bundle for this file:

```
ISSUE_BODY: {ISSUE_CONTEXT.body}

PLANNER_OUTPUT:
  file: {file_path}
  intent: {what_to_change from the plan}

FILE_CONTENT ({file_path}, lines {start}-{end}):
  {truncated file content with line numbers}

HISTORICAL_IDIOMS (past PRs that modified this file):
  - PR #{number}: "{pr_body_excerpt}" (merged {date})
  - PR #{number}: "{commit_message}" (merged {date})
  [top 3-5 by recency]

GRAPHRAG_PR_CONTEXT:
  CI pass rate for this file: {percentage}
  Common review feedback: "{most frequent review comment pattern}"
```

### Step 9 — Call Patcher (per file)

For each file in the approved plan, call the Patcher model (Qwen 2.5 Coder 7B + Patcher LoRA adapter) with the context bundle from Step 8.

The Patcher returns a unified diff:

```diff
--- a/{file_path}
+++ b/{file_path}
@@ -{start},{count} +{start},{count} @@
 context line
-removed line
+added line
 context line
```

Store each file's diff as `PATCHER_OUTPUT[file_path]`.

Validate each diff:
- Must be valid unified diff format (starts with `---` and `+++` headers, has `@@` hunk markers).
- Line numbers in the diff must be plausible (within the range of the file content provided).
- If validation fails, retry the Patcher once with the same input. If it fails again, flag this file and continue with other files.

After all files are processed, assemble the full patch by concatenating all per-file diffs.

### Step 10 — Run sandbox validation

Execute the following Docker sandbox workflow:

1. **Create** a disposable Docker container from the pre-built base image (project dependencies pre-installed).
2. **Copy** the current repository snapshot into the container.
3. **Apply** the assembled patch using `git apply --check` first (dry run), then `git apply` if the check passes.
4. **Run** the validation command: `pytest` (or the project's configured test command).
5. **Capture** stdout, stderr, exit code, and runtime.
6. **Destroy** the container regardless of outcome.

Resource limits: 60-second timeout, 2GB memory cap, no network access.

**If the sandbox passes (exit code 0):** proceed to Step 11 with the patch and sandbox results.

**If the sandbox fails (exit code != 0):** execute the Patcher retry loop:

1. Parse the sandbox failure output. Strip noise: remove stack traces from unrelated tests, dependency installation warnings, and pytest collection output. Keep only the actual failure messages, assertion errors, and import errors.
2. Format the failure as a `SANDBOX_FAILURE_LOG` block:
   ```
   SANDBOX_FAILURE_LOG:
     Exit code: {code}
     Failed tests: {list of failed test names}
     Error summary: {cleaned error messages, max 500 tokens}
     Suggested focus: {which part of the patch likely caused the failure}
   ```
3. Append this block to the original Patcher prompt and call the Patcher again.
4. Apply the new diff and re-run the sandbox.
5. **Max 3 total Patcher attempts per file** (1 initial + 2 retries). If all 3 fail, flag this file as unresolved and proceed to the Critic with the best-effort patch and failure logs.

### Step 11 — Fetch Critic context and call Critic

Assemble the Critic context bundle:

1. **Generated patch**: the full assembled patch (all file diffs).
2. **Sandbox result**: `{exit_code, test_summary, runtime}`.
3. **Historical review patterns** from Neo4j: for each file in the patch, query `File → REVIEWED_IN → PR Reviews where state='CHANGES_REQUESTED'`. Return the review body text. These are the team's known "gotchas" for these files.
4. **Inline code friction** from Neo4j: query `pr_review_comments[].body` mapped to the `diff_hunk` for each file. These are localized, line-level engineering feedback from past reviewers.
5. **Historical CI patterns**: query `check_runs[].conclusion` for past PRs touching these files. Return `{failure_rate, common_failure_modes}`.

Build the Critic prompt:

```
GENERATED_PATCH:
  {full unified diff}

SANDBOX_RESULT:
  exit_code: {code}
  stdout: "{test summary}"
  stderr: "{error output if any}"
  runtime: {seconds}

HISTORICAL_REVIEW_PATTERNS ({file_path}):
  - "CHANGES_REQUESTED: {review body}" (PR #{number})
  [for each file, top 3 most relevant reviews]

INLINE_CODE_FRICTION ({file_path}):
  - Line {n}: "{inline comment}" (PR #{number}, reviewer: @{username})
  [only for files/lines relevant to the current patch]

HISTORICAL_CI_PATTERNS:
  Files: {list of patched files}
  Recent CI failure rate: {percentage}
  Common failure modes: {list}
```

Call the Critic model (Qwen 2.5 Coder 7B + Critic LoRA adapter).

The Critic returns:

```
DECISION: APPROVE | REQUEST_CHANGES
FEEDBACK: {natural language review body}
LINE_COMMENTS:
  - {file_path}, line {n}: "{comment}"
  [optional inline comments]
```

### Step 12 — Route Critic decision

**If APPROVE:** go to Step 13.

**If REQUEST_CHANGES:** classify the feedback:

- **Implementation-level feedback** (wrong variable name, missing import, logic error in the patch, style issue): retry the Patcher with the Critic's feedback appended as a `CRITIC_FEEDBACK` block. Then re-run sandbox and Critic. Max 3 total Patcher↔️Critic iterations across all retry sources (sandbox failures + critic rejections combined).

- **Strategy-level feedback** (wrong file targeted, wrong subsystem, architectural mismatch, missing a required file): this is a replan signal. Go to the Replan decision (below).

To classify: check if the Critic's feedback references files NOT in the current plan, mentions "wrong file", "wrong approach", "should modify X instead", or explicitly states the plan's assumptions are incorrect. If any of these are true, it is strategy-level. Everything else is implementation-level.

**Replan decision (rare, strategy-level only):**

Re-enter Step 4 with enriched context. Add to the Planner prompt:
```
PREVIOUS_ATTEMPT_SUMMARY:
  Files targeted: {list}
  Critic feedback: "{strategy-level feedback}"
  Failure reason: {why the previous plan was rejected at strategy level}

INSTRUCTION: The previous plan was rejected because the targeted
files/approach were incorrect. Use the Critic's feedback and the
original issue context to produce a revised plan targeting the
correct files/subsystem.
```

Max 1 replan. If the second plan also gets strategy-level rejection from the Critic, flag the issue as requiring manual intervention and return all artifacts to the developer.

### Step 13 — Return results to VS Code plugin

Send the final output to the VS Code plugin:

```json
{
  "status": "complete",
  "decision": "APPROVE",
  "patch": "{full unified diff text}",
  "critique": {
    "decision": "APPROVE",
    "feedback": "{Critic's review body}",
    "line_comments": [{"file": "...", "line": 42, "comment": "..."}]
  },
  "metadata": {
    "confidence": "HIGH",
    "refinement_applied": false,
    "sandbox_attempts": 1,
    "critic_attempts": 1,
    "total_latency_ms": 12340,
    "total_tokens_used": 8500
  }
}
```

The VS Code plugin renders the patch using the `vscode.diff` command and displays Critic feedback as inline diagnostics.

---

## Replan policy

Do NOT re-enter the Planner for these situations (keep them in the Patcher↔️Sandbox↔️Critic retry loop):
- Syntax errors in generated code
- Missing imports
- Wrong variable names
- Test failures due to implementation bugs
- Style or formatting issues
- Off-by-one errors or logic mistakes within the correct function

DO re-enter the Planner ONLY for these situations:
- Critic says the wrong file or subsystem was targeted
- Repeated Patcher failures (3 attempts) all fail on the same fundamental assumption about file structure or API
- New issue evidence appears mid-run (new comments on the issue, scope changes detected via MCP polling)
- The Patcher's diff targets a function/class that doesn't exist in the file (indicates the Planner's symbol context was wrong)

Max 1 replan per issue. After that, return to human.

---

## Novel-issue fallback

When GraphRAG returns sparse or empty results (fewer than 2 candidate files with frequency ≥ 2, or top ANN similarity score < 0.5):

1. Skip directly to refinement (Step 6) with Level 1 + Level 2.
2. In Level 1, emphasize keyword/symbol search over graph traversal (the graph won't help if there's no historical precedent).
3. In Level 2, read more files (up to the 8-file budget) to compensate for missing graph signal.
4. In the Planner prompt, add an explicit flag:
   ```
   NOTE: This issue has low historical precedent in the codebase graph.
   Candidate files are based on keyword/symbol matching rather than
   historical resolution patterns. Apply extra caution in file selection.
   ```
5. Set confidence to LOW regardless of other signals.
6. Always present to HITL with the low-confidence flag.

---

## Context budgeting rules

These are hard limits. Do not exceed them.

| Context component | Max tokens (approximate) | Applies to |
|---|---|---|
| ISSUE_BODY | 1,500 | All models |
| CANDIDATE_FILES list | 500 | Planner |
| SYMBOL_CONTEXT | 800 | Planner |
| SIMILAR_RESOLUTIONS | 600 | Planner |
| Refinement bundle (if used) | 1,500 | Planner pass-2 |
| FILE_CONTENT (per file) | 2,000 | Patcher |
| HISTORICAL_IDIOMS (per file) | 500 | Patcher |
| GENERATED_PATCH | 3,000 | Critic |
| HISTORICAL_REVIEW_PATTERNS | 800 | Critic |
| SANDBOX_FAILURE_LOG (if used) | 500 | Patcher retry |
| CRITIC_FEEDBACK (if used) | 400 | Patcher retry |

**Total Planner context budget:** ~4,000 tokens (pass-1), ~5,500 tokens (pass-2 with refinement).
**Total Patcher context budget per file:** ~4,500 tokens.
**Total Critic context budget:** ~5,000 tokens.

If any component exceeds its budget, truncate it. Truncation priority (what to cut first):
1. HISTORICAL_IDIOMS: keep only top 2 instead of 5.
2. SIMILAR_RESOLUTIONS: keep only top 3 instead of 5.
3. FILE_CONTENT: narrow the ±20 line window to ±10 lines.
4. ISSUE_BODY: truncate to first 800 tokens (usually the most important context is at the top).

Never truncate the PLAN, the GENERATED_PATCH, or the CRITIC_FEEDBACK — these are primary artifacts.

---

## Budget caps for retrieval operations

These prevent the orchestrator from degenerating into an expensive repo crawler.

| Operation | Hard cap |
|---|---|
| GraphRAG top-K (Level 0) | K=6 issues, return max 10 files |
| GraphRAG expanded (Level 1) | K=12 issues, return max 20 additional files |
| Keyword/symbol search (Level 1) | Max 5 search queries, max 20 results total |
| File reads via MCP (Level 2) | Max 8 files |
| Tree-sitter span extraction (Level 2) | Max 3 symbols per file, max 50 lines per symbol |
| Import tracing (Level 2) | Max 1 hop, max 3 traced imports |
| Deep GraphRAG traversal (Level 3) | Max 3 additional hops |
| Directory listing (Level 3) | Max 3 directories, max 50 files listed |
| Planner calls per issue | Max 2 (pass-1 + pass-2) |
| Patcher calls per file | Max 3 (1 initial + 2 retries) |
| Critic calls per issue | Max 3 |
| Sandbox runs per issue | Max 6 (3 per Patcher attempt × 2 if replan) |
| Replans per issue | Max 1 |
| Total refinement rounds | Max 1 |

---

## Adapter routing

The Coder Hub endpoint hosts three LoRA adapters on a single Qwen 2.5 Coder 7B base:

| Step | Adapter name | When to activate |
|---|---|---|
| Step 4, Step 6 (pass-2) | `planner` | When calling the Planner model |
| Step 9 | `patcher` | When calling the Patcher model |
| Step 11 | `critic` | When calling the Critic model |

Specify the adapter name in the HuggingFace API call. The endpoint handles adapter switching internally.

Before every model call:
1. Check endpoint health (`GET /health`). If endpoint is paused (auto-pause), send a wake request and wait up to 120 seconds for it to become ready.
2. If the endpoint returns a 5xx error or times out (>30s), retry once after 10 seconds. If it fails again, log the error and surface it to the developer as an infrastructure failure (not a model failure).

---

## Logging contract

For every issue processed, log the following as a structured JSON trace:

```json
{
  "issue_number": 12345,
  "timestamp": "2025-01-15T10:30:00Z",
  "pipeline_trace": {
    "retrieval": {
      "level_0": {"files_found": 8, "top_score": 0.82, "latency_ms": 35},
      "level_1": {"triggered": false},
      "level_2": {"triggered": false},
      "level_3": {"triggered": false}
    },
    "planner": {
      "pass_1": {"decision": "YES", "confidence": "HIGH", "files": ["..."], "tokens_in": 3200, "tokens_out": 450},
      "pass_2": null,
      "refinement_triggered": false,
      "triggers_fired": []
    },
    "hitl": {"decision": "approve", "modifications": null},
    "patcher": {
      "files_processed": 2,
      "attempts_per_file": {"file1.py": 1, "file2.py": 2},
      "total_tokens": 6800
    },
    "sandbox": {
      "runs": 2,
      "final_exit_code": 0,
      "total_runtime_s": 18.5
    },
    "critic": {
      "attempts": 1,
      "final_decision": "APPROVE",
      "tokens_in": 4200,
      "tokens_out": 320
    },
    "replan": false,
    "total_latency_ms": 12340,
    "total_tokens": 15020,
    "final_status": "complete"
  },
  "evidence_bundle": {
    "planner_input": "{serialized context bundle}",
    "patcher_inputs": ["{per-file context bundles}"],
    "critic_input": "{serialized context bundle}"
  }
}
```

The `evidence_bundle` field is for offline analysis and eval dataset construction. It allows you to replay any pipeline run with different model versions.

---

## Error handling

| Error condition | Action |
|---|---|
| MCP server unreachable | Retry 3 times with 5s backoff. If still down, abort and notify developer. |
| Neo4j unreachable | Retry 3 times with 5s backoff. If still down, skip GraphRAG and go directly to Level 2 keyword search + file reads. Set confidence to LOW. |
| HuggingFace endpoint paused | Send wake request, wait up to 120s. If still not ready, notify developer of cold-start delay. |
| HuggingFace endpoint 5xx | Retry once after 10s. If still failing, abort and notify developer. |
| Planner returns invalid schema | Use best-effort parse. If completely unparseable, retry once. If still invalid, flag for manual review. |
| Patcher returns invalid diff | Retry once with same input. If still invalid, skip this file and continue. |
| Sandbox times out (>60s) | Kill container, treat as failure, retry Patcher. |
| Docker daemon not running | Abort sandbox step, skip to Critic without sandbox results. Flag in Critic prompt that sandbox was not run. |
| File not found during MCP fetch | Remove file from plan, continue with remaining files. If no files remain, abort. |
| All Patcher retries exhausted for a file | Include best-effort diff in final patch with a warning flag. Let Critic evaluate it. |

---

## What the orchestrator does NOT do

- It does NOT generate code. That is the Patcher's job.
- It does NOT evaluate code quality. That is the Critic's job.
- It does NOT decide which files to modify. That is the Planner's job.
- It does NOT silently override any model's output. If the Planner says NO, the orchestrator does not flip it to YES — it runs refinement to give the Planner better evidence, then lets the Planner decide again.
- It does NOT have unbounded loops. Every retry path has a hard cap.
- It does NOT stream raw file dumps to models. It always compresses, ranks, and truncates context before passing it.
- It does NOT access the codebase directly. All file reads go through MCP or VS Code workspace APIs.