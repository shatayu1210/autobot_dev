# Critic Orchestrator Implementation Plan

## Objective
Implement the final Critic evaluation node. Once the Patcher has generated a diff that ideally passes the Sandbox tests (or exhausts its retry limit), the generic Qwen2.5 Coder Instruct base model is prompted to perform a holistic code review. It ensures the proposed patch actually solves the user's initial Issue and aligns with the Planner's directive.

## 1. Critic Adapter Definition
**File Target**: `autobot_vscode/local_orchestrator/app.py`
- **Action**: Ensure the critic chat function correctly routes to the base model.
- **Action**: In `hf_tgi_chat()`, if `adapter_id` is empty (or we explicitly pass `""` for the critic), TGI natively defaults to the base model weights (`qwen2.5-coder:7b`). This requires no new endpoints.

## 2. Critic Prompt Construction
**File Target**: `autobot_vscode/local_orchestrator/critic_orchestrator.py` (New File)
- **Action**: Create `evaluate_patch(issue: Issue, plan: PlannerPlan, diff: str, sandbox_result: dict)`.
- **Action**: Build the prompt. Include:
  - Original Issue Title & Body.
  - The Planner's Summary & Steps.
  - The final generated Unified Diff.
  - The Sandbox test output (Did it pass? If it failed, what is the traceback?).
- **Action**: Instruct the base model to evaluate:
  1. Does this diff structurally address the core complaint in the issue?
  2. Are there any obvious logical flaws or missing edge cases?
  3. If tests failed, is the failure a result of the patch or an unrelated environment issue?

## 3. Critic Output Parsing
**File Target**: `autobot_vscode/local_orchestrator/critic_orchestrator.py`
- **Action**: Force the model to output a structured JSON verdict, or parse a specific format:
  ```json
  {
    "verdict": "APPROVE" | "REJECT",
    "feedback": "Detailed explanation of why."
  }
  ```

## 4. LangGraph State Machine Integration (The Core Execution Loop)
**File Target**: `autobot_vscode/local_orchestrator/graph.py` (New File)
- **Action**: Implement the cyclic Patcher-Sandbox-Critic execution loop using LangGraph. This replaces the manual synchronous `for` loops in `app.py`, providing a robust, checkpointable, LLM-driven state machine.
- **State Definition (`PatchState`)**:
  ```python
  from typing import TypedDict, Annotated
  import operator

  class PatchState(TypedDict):
      issue_context: dict
      planner_directive: dict
      current_diff: str
      sandbox_output: dict
      critic_feedback: str
      iterations: int
      verdict: str
  ```
- **Nodes Definition**:
  1. `generate_patch_node(state)`: Formats the Planner context + `critic_feedback` (if any), invokes the **Patcher LLM**, extracts the diff, and increments the `iterations` counter.
  2. `run_sandbox_node(state)`: Applies the diff via the `/run_tests` Sandbox API. Captures `stdout`/`stderr` and the pass/fail boolean into `sandbox_output`.
  3. `critic_review_node(state)`: Packages the Issue, Plan, Diff, and Sandbox Output. Invokes the **Critic Base LLM** to evaluate holistic correctness. Returns a structured verdict (`ACCEPT` or `REVISE`) and `feedback`.
- **Conditional Edges (The Router)**:
  - `START` -> `generate_patch_node` -> `run_sandbox_node` -> `critic_review_node`.
  - Create a router function `should_continue(state)` attached to the output of `critic_review_node`:
    - If `state["verdict"] == "ACCEPT"`: Route to `END`.
    - If `state["verdict"] == "REVISE"` and `state["iterations"] < 3`: Route back to `generate_patch_node`.
    - If `state["iterations"] >= 3`: Route to `END` (exhausted retries).
- **Why LangGraph Here?**: 
  Unlike the deterministic pure-Python planner refinement loop, this execution loop relies on **LLM-driven routing**. The Critic model actively decides the execution path. LangGraph natively handles this non-deterministic cyclical routing, making the complex LLM-to-LLM conversation (Patcher arguing with Critic via Sandbox results) clean, observable, and easy to debug via LangSmith/Langfuse.
