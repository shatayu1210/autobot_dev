# Hybrid Autonomous DevOps Ecosystem - Deployment Plan

## Purpose
This document is a high-detail deployment plan for the **Hybrid Autonomous DevOps Ecosystem**. It is written so an AI IDE can follow it with minimal ambiguity and implement the system in a staged, production-like way.

This blueprint assumes the following core constraint:

- The **Reasoner** uses **Qwen 2.5 Instruct 7B**.
- The **Coder Agents** use **Qwen 2.5 Coder 7B**.
- Because these rely on different base models, the **7B inference layer must be split into two separate Hugging Face endpoints**.
- This slightly increases active infrastructure cost, but **Hugging Face Auto-Pause** keeps the monthly academic budget controlled.
- The coding loop must include a **local sandbox validation layer** so AI-generated patches are tested before they are reviewed or surfaced to the user.

---

## 1. Final Architecture Summary

The system is a **dual-base hybrid ecosystem** composed of:

- local orchestration,
- local graph memory,
- a local disposable sandbox runtime,
- cloud inference endpoints for the LLM layer.

### Architectural Goals
- Keep graph storage, orchestration, and patch validation local to eliminate unnecessary managed infrastructure costs.
- Use Hugging Face inference only where GPU-backed model execution is required.
- Separate analysis reasoning from coding behavior because the Reasoner and Coder agents do not share the same base model family.
- Validate generated patches before code review using an isolated execution environment.
- Support two major workflows:
  1. **Slack bottleneck detection and explanation loop**
  2. **VS Code autonomous patch planning, generation, validation, and review loop**

### High-Level Split
- **Local machine** hosts data, graph memory, ETL-connected tool access, orchestration, and sandbox validation.
- **Hugging Face** hosts model inference services.
- **ngrok** exposes the Slack orchestrator so Slack can send events to your local machine.

---

## 2. Model Inventory and Logic Distribution

The complete system uses **5 logical models/agents**.

| Model | Role | Base Model | Weight Type | Primary Interface |
|---|---|---|---|---|
| The Sentinel | Lightweight monitor and trigger agent | Qwen 2.0 1.5B | Base (Quantized) | Slack / polling loop |
| The Reasoner | Analytical explanation and Cypher generation | Qwen 2.5 Instruct 7B | Base or LoRA | Slack / analysis |
| The Planner | Fix strategy generation | Qwen 2.5 Coder 7B | LoRA adapter | VS Code |
| The Patcher | Code generation and patch creation | Qwen 2.5 Coder 7B | LoRA adapter | VS Code |
| The Critic | Code review and rejection/approval pass | Qwen 2.5 Coder 7B | LoRA adapter | VS Code |

### Functional Distribution

#### The Sentinel
- Runs lightweight recurring monitoring.
- Polls GitHub and Jira state through MCP-enabled tools.
- Scores issues or bottlenecks.
- Triggers the Reasoner only when a threshold condition is met.
- Exists to keep the expensive reasoning endpoint asleep unless needed.

#### The Reasoner
- Performs detailed analysis after a Sentinel trigger.
- Converts user or system prompts into graph-aware reasoning.
- Generates **Text-to-Cypher** queries against Neo4j.
- Drafts Slack notifications with citations and evidence.
- Must be guarded to prevent unsafe or destructive graph queries.

#### The Planner
- Determines how a requested fix should be approached.
- Produces a structured implementation strategy before code generation.
- Acts as the first coding-stage reasoning layer in the VS Code workflow.

#### The Patcher
- Generates the actual implementation diff or patch.
- Consumes context from MCP tools and the Planner output.
- Produces code modifications rather than broad prose.
- Must be able to revise its output using sandbox failure logs.
- Training pipeline uses strict filtered PR diff dataset with persisted constraints, Tree-sitter spans (Python), and GraphRAG file idioms for reproducible patch behavior.

#### The Critic
- Reviews generated code for correctness and quality.
- Rejects low-confidence or invalid outputs.
- Receives sandbox-passing patches, not raw unvalidated code.
- Forces retries when the patch does not satisfy internal review criteria.
- Caps iterative loops to prevent runaway generation.

---

## 3. Infrastructure Configuration

## 3.1 Local Environment

The local environment hosts the system's **brains**, **memory**, **control plane**, and **pre-flight validation layer**. This avoids cloud database charges and keeps the graph and patch validation loop under full local control.

### Components Hosted Locally
- Neo4j (Docker)
- MCP server
- Slack orchestrator
- VS Code orchestrator
- Sandbox runtime (Docker)
- Existing ETL-wrapped services
- ngrok tunnel for Slack event delivery

### Why Local Hosting Is Required
- Eliminates managed graph database costs.
- Maintains direct control over a large graph of approximately **50k nodes**.
- Supports custom ETL and tool wrapping without extra cloud complexity.
- Keeps graph lookup latency predictable for development.
- Lets you validate AI-generated patches without sending source code to a third-party execution system.
- Provides the cheapest possible execution sandbox because you are already running local Docker.

---

### A. Neo4j (Docker)

Neo4j is the persistent graph memory layer.

#### Planned Data Volume
- ~12k Issues
- ~38k PRs
- Total graph size target: ~50k nodes

#### Responsibilities
- Store issue-to-PR relationships.
- Support graph traversal for reasoning and historical pattern retrieval.
- Power vector-assisted retrieval if embeddings are precomputed and stored or indexed.

#### Deployment Requirements
- Use Docker image: `neo4j:latest`
- Mount a persistent volume for database durability.
- Ensure the graph can survive container restarts.

#### Required Configuration
- Expose Neo4j ports locally.
- Store credentials securely in environment variables or local secrets management.
- Pre-create schema and indexes needed for graph traversal and vector search.

#### Optimization Requirement
**Pre-calculate embeddings during ETL** instead of generating embeddings during inference-time queries.

This matters because it:
- reduces runtime latency,
- avoids repeated embedding cost,
- makes vector search immediately available inside Neo4j,
- keeps the reasoning loop focused on retrieval and analysis instead of preprocessing.

#### Expected Outcome
- Graph database is queryable locally.
- Vector index is available and active.
- The Reasoner can query both structural graph edges and semantic search features.

---

### B. MCP Server (Local)

The MCP server is a Python service that wraps existing ETL logic and exposes it to agents as standardized tool calls.

#### Responsibilities
- Provide live access to GitHub state.
- Provide live access to Jira state.
- Expose graph lookup tools.
- Normalize tool interfaces for both Slack and VS Code orchestrators.
- Serve as the bridge between agents and real project data.

#### Design Intent
The agents should **pull live operational state** through MCP rather than hardcoding integrations directly inside model prompts.

#### Minimum Tool Surface
The MCP server should expose tool endpoints for:
- fetch recent GitHub issues,
- fetch recent PR activity,
- fetch Jira tickets,
- fetch linked issue or PR relationships,
- run approved Neo4j queries,
- return relevant code context,
- return graph schema metadata for Text-to-Cypher prompting,
- fetch repository snapshot or selected file set for sandbox validation.

#### Implementation Constraint
Reuse the existing ETL logic rather than rewriting ingestion from scratch.

---

### C. Local Orchestrators

Two orchestrators are required.

#### 1. Slack Orchestrator
Purpose:
- manage Sentinel polling,
- trigger the Reasoner endpoint,
- receive Slack events,
- send Slack notifications back to channels.

#### 2. VS Code Orchestrator
Purpose:
- receive user-initiated fix requests,
- fetch code and project context,
- route work across Planner -> Patcher -> Sandbox -> Critic,
- return final `.diff` output to the IDE.

#### Recommended Stack
- **FastAPI** for service endpoints
- **LangGraph** for agent workflow and state orchestration

#### Responsibilities Shared by Both Orchestrators
- prompt assembly,
- tool routing,
- retry control,
- adapter selection,
- endpoint invocation,
- structured logging,
- failure handling.

---

### D. Local Sandbox Runtime (Docker)

The sandbox is an isolated, disposable execution environment used to validate AI-generated patches before they are reviewed or surfaced to the user.

#### What the Sandbox Is
Think of it as a temporary clone of the development environment used only for **pre-flight checks**.

#### Primary Goal
Run commands such as:
- `make build`
- `npm install`
- `pytest`
- `npm test`
- project-specific validation scripts

The output of those commands determines whether the generated patch is plausibly valid.

#### Success Signal
- **Exit code 0** = build/tests passed in the sandbox.
- **Non-zero exit code** = patch is broken, incomplete, hallucinated, or incompatible with the codebase.

#### Why It Is Required
Without a sandbox, the patch loop depends only on language-model self-assessment. With a sandbox, the loop gets a concrete execution signal from the real toolchain.

#### Required Properties
- **Isolation:** use Docker so generated code cannot affect the host machine.
- **Disposability:** create a fresh container for each validation run.
- **Clean state:** always apply the patch against a known repository snapshot.
- **No internet by default:** disable network access where practical to reduce exfiltration risk.
- **Log capture:** record stdout, stderr, exit code, runtime, and failed command.

#### Minimum Architecture
- Docker Engine running locally
- Docker SDK for Python or equivalent runtime integration
- Prebuilt base image per target repository or stack
- Validation entrypoint script such as `test.sh`, `validate.sh`, `pytest`, or `npm test`

#### Recommended Local Strategy
Because Neo4j and the orchestrators already run locally, the cheapest and most efficient design is to host the sandbox locally too.

#### Technical Configuration
- Platform: **Docker Engine (Local)**
- Control interface: **Docker SDK for Python**
- Base image: project-specific image with dependencies preinstalled
- Lifecycle:
  1. create disposable container,
  2. copy repository snapshot,
  3. apply generated patch,
  4. run validation script,
  5. collect logs,
  6. destroy container.

#### Suggested Docker SDK Flow
```python
container = client.containers.create(...)
container.put_archive(...)
container.start()
result = container.exec_run("pytest")
container.remove(force=True)
```

#### Best Practice
Build a **base validation image** with dependencies already installed. This avoids repeating slow dependency setup on every patch attempt and keeps retries fast and cheap.

---

### E. Networking

#### ngrok Usage
Use **ngrok** to expose the Slack Orchestrator's local port to the public internet so Slack can send events to the local machine.

#### Why This Is Needed
Slack event subscriptions require a publicly reachable callback URL. Since the orchestrator is local, ngrok acts as the tunnel.

#### Requirement
- Keep the ngrok URL synchronized with Slack app configuration whenever it changes.
- Make sure the local port used by FastAPI is stable and documented.

---

## 3.2 Cloud Environment (Hugging Face Inference)

The cloud environment contains **three separate inference stations**.

### Station 1: The Sentinel

#### Form Factor
- Hugging Face Space

#### Model
- Qwen 1.5B

#### Hardware
- CPU Upgrade or T4 Small

#### Availability Mode
- Always on

#### Job
- Perform continuous bottleneck scoring every 30 minutes.

#### Rationale
The Sentinel should be cheap, lightweight, and always available so it can continuously monitor state without paying for a heavy GPU endpoint.

---

### Station 2: The Reasoning Hub

#### Form Factor
- Hugging Face Inference Endpoint

#### Model
- Qwen 2.5 Instruct 7B

#### Hardware
- NVIDIA A10G

#### Required Settings
- Auto-Pause: 15 minutes

#### Job
- Generate detailed Slack notifications
- Perform Text-to-Cypher generation
- Support analytical reasoning over graph-backed context

#### Rationale
This endpoint is only needed when the Sentinel detects a meaningful bottleneck or when complex Slack-side analysis is required.

---

### Station 3: The Coder Hub

#### Form Factor
- Hugging Face Inference Endpoint

#### Model Stack
- Qwen 2.5 Coder 7B base
- Planner LoRA
- Patcher LoRA
- Critic LoRA

#### Hardware
- NVIDIA A10G

#### Required Settings
- Auto-Pause: 15 minutes
- Multi-LoRA enabled

#### Job
- Execute autonomous reactive patching workflows for VS Code
- Dynamically switch between planning, coding, and review behaviors through LoRA selection

#### Rationale
All three coding behaviors share the same coder base model, so they can live on one endpoint with multiple adapters enabled.

---

## 4. Why the 7B Layer Must Be Split into Two Endpoints

This is a core architectural decision.

### Constraint
- The Reasoner uses **Qwen 2.5 Instruct 7B**.
- The Planner, Patcher, and Critic use **Qwen 2.5 Coder 7B**.

These are **different base models**, so they should not be treated as a single interchangeable LoRA-backed service.

### Deployment Decision
Create:
1. one endpoint for **Qwen 2.5 Instruct 7B**, and
2. one endpoint for **Qwen 2.5 Coder 7B + multi-LoRA adapters**.

### Cost Impact
- Active cost is slightly higher because two GPU-backed services exist.
- Real monthly impact remains controlled due to **Auto-Pause** on both endpoints.

### Operational Benefit
- cleaner model separation,
- more predictable prompting,
- less risk of mixing reasoning and code-generation behavior,
- easier debugging and performance tuning per workflow.

---

## 5. Sequential Multi-Agent Workflows

## 5.1 Slack Workflow: Sentinel-to-Reasoner Loop

This loop is for bottleneck detection, analysis, and Slack notification.

### Trigger Cadence
- Every 30 minutes

### Step-by-Step Flow
1. **Sentinel-side orchestration begins locally.**
   - The Slack Orchestrator polls GitHub and Jira through MCP.
   - It fetches the current state required for bottleneck scoring.

2. **The Sentinel evaluates issue risk.**
   - The 1.5B model scores issues or bottlenecks.
   - Scoring is intended to be lightweight and frequent.

3. **Threshold gate is applied.**
   - If any issue score is **greater than 0.8**, the Reasoning Hub is triggered.
   - If not, the pipeline stops and waits for the next poll cycle.

4. **The Reasoner wakes up.**
   - The Qwen 2.5 Instruct 7B endpoint is invoked.
   - The Orchestrator provides the relevant graph schema and context.

5. **Text-to-Cypher generation occurs.**
   - The Reasoner generates a Cypher query to search local Neo4j for historical patterns.
   - This query is used to gather evidence related to similar prior issues, linked PRs, or recent high-severity patterns.

6. **Historical evidence is retrieved.**
   - Neo4j returns structural and or vector-backed results.
   - The Orchestrator compiles the evidence into a reasoning context.

7. **Final Slack message is drafted.**
   - The Reasoner generates a human-readable alert.
   - The message includes citations or evidence references.
   - The notification is pushed to the designated Slack channel.

### Slack Workflow Output
- a structured alert containing:
  - the detected bottleneck,
  - confidence or score,
  - relevant historical examples,
  - graph-backed evidence,
  - suggested next action.

---

## 5.2 VS Code Workflow: Planner-Patcher-Sandbox-Critic Loop

This loop is for reactive autonomous patching with validation before review.

### Trigger
- User initiates a fix request from VS Code.

### Updated Control Flow
**Planner -> Refinement -> HITL Approval -> Patcher -> Sandbox -> (Retry if Fail) -> Critic -> VS Code**

This is the correct sequence because the Critic should review a patch that already survives basic execution and test validation.

### Step-by-Step Flow
1. **User requests a fix in VS Code.**
   - The local VS Code Orchestrator receives the request.

2. **Context retrieval begins.**
   - The Orchestrator uses MCP to fetch relevant code and project state.
   - This may include issue context, file context, recent PR state, or related implementation history.

3. **Coder Hub is invoked.**
   - The Qwen 2.5 Coder 7B endpoint is activated.
   - The appropriate adapter is selected for each stage.

4. **Planner LoRA runs first.**
   - It maps the requested fix into a strategy.
   - It determines what should change and how the patch should be structured.
   - It should consume hybrid retrieval context: GraphRAG candidate files + Tree-sitter structural grounding.
   - Training-time note: planner `What to change` targets are cleaned to remove PR-template/license noise and keep concise implementation intent.

5. **Orchestrator refinement runs second (tool-driven, bounded).**
   - This is not only prompt text. The orchestrator actively:
     - probes top candidate files,
     - reads limited code spans for target symbols,
     - scores evidence and updates the plan.
   - If planner output is NO, run only a cheap guard check and skip full refinement unless contradiction is found.

6. **Human approval gate (HITL) on refined plan.**
   - User reviews the refined plan before patch generation.

7. **Patcher LoRA runs next.**
   - It generates the code modification or patch.
   - Output should be aligned to IDE-consumable diff behavior.

8. **Sandbox validation runs next.**
   - The Orchestrator applies the generated diff to a disposable Docker container.
   - It runs the project validation command such as `pytest`, `npm test`, or `make build`.
   - It captures stdout, stderr, exit code, and runtime.

9. **Retry loop is triggered on sandbox failure.**
   - If validation fails, the Orchestrator sends the terminal logs back to the Patcher.
   - The Patcher uses the error message as corrective feedback.
   - The system retries from the Patcher stage.
   - Recommended global cap: **3 total attempts**.

10. **Critic LoRA runs after sandbox success.**
   - The Critic reviews the now-validated patch for code quality, maintainability, and policy compliance.
   - It can still reject a patch even if the build passes.

11. **Planner re-entry decision (strategy-level failures only).**
   - Re-enter Planner only if Critic/sandbox evidence says the plan targeted the wrong subsystem/file or issue scope changed.
   - Do not replan for ordinary implementation bugs; keep those in the Patcher retry loop.

12. **Final output is returned to VS Code.**
   - If the patch passes sandbox validation and Critic review, the final `.diff` is delivered to the IDE.
   - If all attempts fail, return the best failure-safe output with reason, failed command, and last terminal logs.

### Why This Ordering Matters
- The sandbox provides a real execution signal.
- The Patcher can self-correct using concrete toolchain failures rather than only model-generated critique.
- The Critic focuses on quality and reasoning after basic correctness is established.

### VS Code Workflow Output
- final patch or diff,
- sandbox validation status,
- terminal logs on failure,
- review outcome,
- bounded retry behavior,
- controlled handoff to the user or editor.

---

## 6. Sandbox Design Details

This section formalizes the sandbox as a first-class system component.

## 6.1 What Industry Tools Commonly Do

### VS Code / GitHub Copilot
- Standard Copilot behavior does **not** provide a disposable validation sandbox by default.
- It typically relies on the user to run commands manually.
- GitHub Codespaces can function as a persistent remote containerized environment, but that is not the same as a per-patch disposable sandbox.

### Cursor
- Cursor commonly asks permission to run commands in the user's local terminal.
- That means it behaves more like an agent acting on your machine than a true isolated sandbox.

### Open-Source Autonomous Agents
Tools like OpenHands-style systems and autonomous coding agents usually rely on **Docker containers**.

Typical pattern:
1. spin up a clean container,
2. mount or copy the code,
3. run the patch,
4. execute tests,
5. capture logs,
6. destroy the environment.

This is the model your architecture should follow.

---

## 6.2 Development and Infrastructure Requirements

### Development Requirements
You need a validation script or command that can reliably answer: **did this patch work?**

Examples:
- `pytest`
- `npm test`
- `make build`
- `bash test.sh`
- `bash validate.sh`

The Orchestrator must capture:
- stdout,
- stderr,
- exit code,
- execution time,
- failed command,
- optional artifact paths such as coverage or lint reports.

### Infrastructure Requirements
- **Isolation:** use Docker to protect the host from harmful commands.
- **State management:** always start from a clean repository snapshot.
- **Determinism:** use a stable base image and pinned dependencies where possible.
- **Networking policy:** default to no internet access unless a repository genuinely requires it for tests.
- **Disposal:** delete containers after validation to avoid drift and hidden state.

---

## 6.3 Recommended Validation Lifecycle

1. Prepare a known-good repository snapshot.
2. Create a disposable container from the project's base validation image.
3. Copy the repository snapshot into the container.
4. Apply the generated diff.
5. Execute the validation script.
6. Collect logs and metadata.
7. If validation passes, forward patch to Critic.
8. If validation fails, return logs to Patcher for correction.
9. Remove the container.

### Minimal Metadata to Persist Per Attempt
- patch attempt number,
- adapter used,
- command executed,
- exit code,
- stdout,
- stderr,
- duration,
- timestamp,
- final disposition: pass, fail, rejected.

---

## 7. Technical Specifications

## 7.1 Coder Hub vLLM Startup Command

Use the following startup pattern for the **Coder Hub** endpoint.

```bash
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen2.5-Coder-7B-Instruct \
    --enable-lora \
    --lora-modules \
        planner=hub/user/planner-lora \
        patcher=hub/user/patcher-lora \
        critic=hub/user/critic-lora \
    --max-loras 3 \
    --auto-pause 15
```

### Notes on This Command
- Base model is the coder model.
- LoRA support is enabled.
- Three named adapters are registered.
- Concurrent adapter availability is capped at 3.
- Auto-pause is enabled to control cost.

### Required Validation
Before relying on this endpoint in production-like workflows, verify:
- the endpoint accepts requests successfully,
- each adapter can be selected independently,
- adapter switching behaves correctly,
- response format is stable for orchestration.

---

## 7.2 Text-to-Cypher Logic

The **Reasoner** acts as the database translator.

### Role
When a Slack-side analytical request or bottleneck event requires graph lookup, the Orchestrator provides the Reasoner with the graph schema and query objective.

### Example Prompt Pattern
> Based on the schema `(Issue)-[:FIXED_BY]->(PR)`, generate a Cypher query to find all PRs linked to critical bottlenecks in the last 48 hours.

### Responsibilities of the Orchestrator
- supply the schema,
- define the task clearly,
- restrict allowed query behavior,
- validate the generated Cypher before execution,
- reject unsafe or destructive queries.

### Critical Guardrail Requirement
The Reasoner must not be allowed to hallucinate unsafe write or delete Cypher statements.

Only approved query classes should be executable by default.

---

## 7.3 Sandbox Execution Interface

The VS Code Orchestrator should expose a dedicated validation function such as:

```text
validate_patch(repo_snapshot, diff, command, image_name) -> {
  exit_code,
  stdout,
  stderr,
  duration_ms,
  sandbox_status
}
```

### Expected Behavior
- `repo_snapshot` should represent a clean source state.
- `diff` should be the patch generated by the Patcher.
- `command` should map to project-specific validation logic.
- `image_name` should point to a prepared dependency image.

### Output Contract
The return payload should be machine-usable by the Orchestrator and prompt-usable by the Patcher.

---

## 8. Cost Analysis

The following cost model is the projected academic monthly setup.

| Component | Role | Hosting | Estimated Cost |
|---|---|---|---:|
| Sentinel | 1.5B scoring loop | HF Space (Cloud) | ~$0.05/hr |
| Reasoner | 7B Instruct / Text-to-Cypher | HF Endpoint (Cloud) | ~$1.05/hr active, auto-paused |
| Coder Hub | 7B Coder + 3 LoRAs | HF Endpoint (Cloud) | ~$1.05/hr active, auto-paused |
| Orchestrator | LangGraph loop logic | Local | $0.00 |
| Sandbox | Local Docker container | Local | $0.00 |
| Neo4j | GraphRAG store | Local Docker | $0.00 |

### Monthly Academic Estimate

| Component | Hardware | Hourly Rate | Estimated Monthly Cost |
|---|---|---:|---:|
| Sentinel (1.5B) | HF Space (CPU/T4) | ~$0.05 | ~$36.00 |
| Reasoner (7B) | HF Endpoint (A10G) | ~$1.05 | ~$10.00 |
| Coder Hub (7B) | HF Endpoint (A10G) | ~$1.05 | ~$10.00 |
| Sandbox | Local Docker | $0.00 | $0.00 |
| Neo4j / MCP | Local Docker | $0.00 | $0.00 |
| Orchestrators | Local process | $0.00 | $0.00 |
| **Total** |  |  | **~$56.00 / month** |

### Interpretation
- The largest constant monthly cost comes from the always-on Sentinel layer.
- The Reasoner and Coder endpoints are assumed to remain low-cost because they auto-pause.
- The sandbox adds validation quality without adding direct cloud spend.
- Local orchestration and data hosting eliminate managed platform charges.

### Budget Assumption
This estimate assumes:
- moderate academic-scale usage,
- working auto-pause behavior,
- no major endpoint idle leakage,
- local machine availability for database, orchestrators, and sandbox execution.

---

## 9. Guardrails and Safety Controls

Guardrails are mandatory, especially for graph querying and autonomous patching.

## 9.1 Neo4j Query Safety

Implement **NeMo Guardrails locally** to constrain Reasoner-generated Cypher.

### Primary Goal
Prevent hallucinated Cypher that could modify or delete the local graph database.

### Minimum Policy
- allow read-only query patterns by default,
- reject write operations unless explicitly authorized,
- validate schema usage,
- block destructive clauses,
- log rejected query attempts.

---

## 9.2 Sandbox Safety

The sandbox exists to prevent generated code from touching the host environment.

### Minimum Policy
- run each validation in a disposable Docker container,
- do not mount sensitive host directories,
- disable network access by default,
- apply resource limits where possible,
- destroy the container after every attempt,
- capture logs for debugging without preserving mutable container state.

### Host Protection Objective
Prevent scenarios equivalent to accidental commands such as `rm -rf /`, runaway installs, or untrusted command execution against the host machine.

---

## 9.3 Patch Review Safety

The Critic loop should act as an internal quality firewall.

### Minimum Behavior
- reject malformed code,
- reject low-confidence patches,
- reject patches that pass tests but violate code quality expectations,
- stop after 3 attempts,
- return review feedback instead of looping indefinitely.

---

## 9.4 Orchestrator Safety

The orchestrators should:
- log every endpoint call,
- log selected adapter name,
- log retry count,
- log final success or failure state,
- isolate Slack workflow from VS Code workflow failures,
- separate sandbox failure logs from model-generated critique,
- preserve enough traceability for debugging without exposing secrets.

---

## 10. Implementation Order

This section converts the architecture into a recommended execution sequence.

### Phase 1: Local Data Foundation
1. Stand up local Neo4j in Docker.
2. Load the issue and PR graph.
3. Verify persistence volume mapping.
4. Build and activate the vector index.
5. Confirm graph queryability with sample read-only queries.

### Phase 2: MCP Tool Layer
1. Wrap current ETL logic in a Python MCP server.
2. Expose GitHub, Jira, graph, and repository-context tools.
3. Test all tool endpoints from a local Python script.
4. Confirm stable response schemas for orchestrator consumption.

### Phase 3: Sandbox Foundation
1. Create a base validation image for the repository stack.
2. Install project dependencies inside the image.
3. Define the validation entrypoint command or script.
4. Implement Docker SDK control logic in Python.
5. Confirm disposable container create -> patch -> validate -> destroy behavior.
6. Verify logs and exit codes are captured correctly.

### Phase 4: Model Artifacts
1. Export Planner LoRA.
2. Export Patcher LoRA.
3. Export Critic LoRA.
4. Verify all adapters are compatible with the chosen coder base.

### Phase 5: Hugging Face Inference Deployment
1. Create the Sentinel Space.
2. Create the Reasoner endpoint using Qwen 2.5 Instruct 7B.
3. Create the Coder endpoint using Qwen 2.5 Coder 7B + multi-LoRA.
4. Enable Auto-Pause on both 7B endpoints.
5. Validate endpoint wake and sleep behavior.

### Phase 6: Orchestrators
1. Build the Slack Orchestrator with FastAPI and LangGraph.
2. Build the VS Code Orchestrator with FastAPI and LangGraph.
3. Add logging, adapter routing, retry control, and tool binding.
4. Connect each orchestrator to the correct endpoint.
5. Integrate sandbox validation into the VS Code loop.
6. Feed sandbox failure logs back into the Patcher prompt.

### Phase 7: External Integration
1. Expose Slack Orchestrator through ngrok.
2. Register the Slack event URL.
3. Verify inbound Slack events.
4. Verify outbound channel notifications.
5. Validate VS Code diff delivery.

### Phase 8: Guardrails
1. Install NeMo Guardrails locally.
2. Restrict Cypher generation to safe read-oriented patterns.
3. Validate rejection behavior for destructive queries.
4. Add Critic retry cap enforcement.
5. Apply sandbox isolation defaults and resource limits.

### Phase 9: End-to-End Testing
1. Test Sentinel polling without triggers.
2. Test Sentinel threshold trigger > 0.8.
3. Test Reasoner Cypher generation with schema input.
4. Test Slack notification with citations.
5. Test VS Code Planner -> Patcher -> Sandbox -> Critic loop.
6. Test forced sandbox failure and corrective retry behavior.
7. Test forced Critic rejection after sandbox success.
8. Test final `.diff` return path.

---

## 11. Final Checklist

Use this as the immediate implementation checklist.

- [ ] Seed the local Neo4j Docker instance.
- [ ] Verify the Neo4j vector index is active.
- [ ] Wrap the existing ETL logic in an MCP server.
- [ ] Test MCP calls from a local Python script.
- [ ] Create a base sandbox validation image for the target repository.
- [ ] Define a validation script or command such as `pytest`, `npm test`, or `test.sh`.
- [ ] Implement Docker SDK logic for disposable validation runs.
- [ ] Verify sandbox exit code, stdout, and stderr capture.
- [ ] Export LoRA adapters for Planner, Patcher, and Critic from the Coder base.
- [ ] Create two separate 7B Hugging Face endpoints:
  - [ ] Qwen 2.5 Instruct 7B for the Reasoner
  - [ ] Qwen 2.5 Coder 7B + adapters for the Coder Hub
- [ ] Build the Slack Orchestrator.
- [ ] Build the VS Code Orchestrator.
- [ ] Integrate the sandbox into the patch loop.
- [ ] Feed sandbox failure logs back to the Patcher.
- [ ] Expose the Slack Orchestrator through ngrok.
- [ ] Configure Slack event delivery to the ngrok URL.
- [ ] Implement NeMo Guardrails locally.
- [ ] Ensure the Reasoner cannot generate destructive Cypher that could delete the local database.
- [ ] Validate Sentinel threshold logic (`score > 0.8`).
- [ ] Validate max retry logic (`max 3`).
- [ ] Validate final Slack and VS Code outputs end to end.

---

## 12. Non-Negotiable Design Decisions

These decisions should remain fixed unless the architecture is intentionally redesigned.

1. **Separate endpoints for Instruct 7B and Coder 7B are required.**
   They are different base models and should not be collapsed into one service.

2. **Neo4j must remain local.**
   This preserves cost control and supports full ownership of the graph.

3. **The MCP layer is mandatory.**
   It is the standard tool interface between agents and live system state.

4. **The Sentinel must stay lightweight and cheap.**
   Its purpose is to avoid waking expensive inference unnecessarily.

5. **The sandbox must run before the Critic.**
   Real execution feedback is more valuable than review-only critique for early correction.

6. **The Critic loop must be bounded.**
   Unlimited retries would increase cost and reduce reliability.

7. **Cypher generation must be guarded.**
   Read-only by default is the safe baseline.

8. **The sandbox should be local and disposable.**
   This keeps cost near zero and prevents patch validation from depending on a remote execution service.

---

## 13. Definition of Done

The deployment can be considered functionally complete when all of the following are true:

- Neo4j is running locally with persisted graph data.
- Vector index is active and queryable.
- MCP tools return live GitHub, Jira, graph, and repository context.
- Sentinel polling works every 30 minutes.
- Thresholded issues trigger the Reasoner correctly.
- Reasoner can generate safe Cypher from schema-aware prompts.
- Slack notifications are sent with evidence or citations.
- Coder Hub successfully switches among Planner, Patcher, and Critic adapters.
- The sandbox can validate generated patches in a disposable Docker container.
- Sandbox failures are fed back to the Patcher as corrective signals.
- Critic evaluates sandbox-passing patches only.
- VS Code receives final patch output as `.diff`.
- Critic rejects bad patches and retries up to 3 times only.
- Auto-pause works on both 7B Hugging Face endpoints.
- Guardrails block unsafe Cypher behavior.
- Monthly cost remains aligned with the projected academic budget.

---

## 14. One-Screen Execution Summary

### Local
- Neo4j in Docker
- MCP Python server wrapping ETL
- Slack Orchestrator (FastAPI and LangGraph)
- VS Code Orchestrator (FastAPI and LangGraph)
- Sandbox validator using disposable Docker containers
- ngrok tunnel for Slack

### Hugging Face
- Sentinel Space: Qwen 1.5B, always on
- Reasoner Endpoint: Qwen 2.5 Instruct 7B, A10G, auto-pause
- Coder Endpoint: Qwen 2.5 Coder 7B + Planner, Patcher, Critic LoRAs, A10G, auto-pause

### Core Logic
- Sentinel monitors -> Reasoner analyzes -> Slack alerts
- Planner strategizes -> Patcher generates -> Sandbox validates -> Critic reviews -> VS Code receives diff

### Budget
- Estimated total: **~$56/month**

---

## 15. Recommended Next Action

Start with the **local Neo4j + MCP + Sandbox foundation first**, because every downstream reasoning and coding workflow depends on reliable graph access, tool access, and real patch validation.
