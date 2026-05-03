# Orchestrator Refinement Layer

This folder implements the deterministic refinement layer for run5-style Planner/Patcher orchestration.

## What It Does

- Uses repo toolset methods (GraphRAG, keyword search, line reads, interface tracing).
- Decides when to run extra retrieval using deterministic trigger rules.
- Builds a high-signal, bounded context pack for the second pass.
- Stops patcher loop deterministically and hands off to critic.

## Trigger Logic Implemented

Refinement runs when **any** of these are true:

- weak `NO` (`requires_code_change=NO` with low confidence),
- sparse symbol evidence,
- planner-vs-GraphRAG path mismatch,
- low evidence quality,
- planner selected a target file path that does not exist.

The missing-path trigger supports your example:

- if planner asks to edit non-existent file, orchestrator escalates retrieval and reranks nearest existing paths instead of creating a new file blindly.

## Escalation Ladder

- **Level 0**: narrow GraphRAG
- **Level 1**: wider GraphRAG + keyword search
- **Level 2**: line-level reads + interface tracing
- **Level 3**: deeper bounded fallback traversal

The loop escalates only until evidence sufficiency is met or max level is reached.

## Context Bounding

The context packer is deterministic and budgeted:

- max snippets (default 8),
- max chars per snippet (default 1000),
- max total evidence chars (default 10k),
- optional max total evidence tokens cap (`max_total_evidence_tokens`),
- delta-only evidence selection,
- score-based ranking,
- cross-file diversity first,
- near-duplicate removal,
- overflow drops with logging counters.

## Patcher Loop Stop -> Critic Handoff

Handoff happens when:

1. latest patch attempt passes apply + file-allow + unified-diff checks and meets confidence floor, or
2. max patch attempts is reached (critic still receives best failed candidate + diagnostics).

## Main API

- Preferred single entrypoint:
  - `DeterministicOrchestrator(config=...)`
  - `orchestrator.refine(...)`
  - `orchestrator.should_handoff(...)`

- Endpoint routing helper:
  - `HFModelRouter` with `RouterConfig` and `EndpointConfig` for planner/patcher/critic calls.

- Telemetry helper:
  - `JsonlTelemetryLogger` for per-request orchestrator traces.

- Low-level APIs (still available):
  - `run_orchestrator_refinement(...)`
  - `evaluate_triggers(...)`
  - `decide_refinement(...)`
  - `should_handoff_to_critic(...)`

See `orchestrator/refinement.py` for full dataclasses and configuration knobs.
