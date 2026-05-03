# Orchestrator Update Handoff (Run5)

## What Was Done

We implemented a deterministic **orchestrator refinement layer** around patcher retries so the system does not do blind retry generation.

### New files added

- `orchestrator/refinement.py`
- `orchestrator/__init__.py`
- `orchestrator/README.md`
- `orchestrator/refinement_run5_spec.md`

### Existing file updated

- `colab_patcher_orchestrator_eval.py`

## Why This Was Added

Run5 gaps are now narrow (mostly recall misses, path drift, sparse evidence).
So the highest ROI is not broad retraining, but **targeted evidence enrichment** before retrying patch generation.

The orchestrator is treated as:

- deterministic evidence retriever/compressor
- not a second planner
- not an override engine

## Core Behavior Implemented

## 1) Deterministic trigger rules

Refinement runs when any trigger is true:

- weak planner `NO` + low confidence
- sparse Tree-sitter evidence
- planner-vs-GraphRAG path mismatch
- low evidence quality
- planner-selected target path does not exist

Important example covered:

- if planner asks to edit a non-existent file, orchestrator triggers path recovery instead of blindly creating files.

## 2) Escalation policy (level-based)

- **Level 0:** narrow GraphRAG
- **Level 1:** wider GraphRAG + repo keyword search
- **Level 2:** line-level reads + interface tracing
- **Level 3:** deeper bounded traversal fallback

Escalation stops as soon as evidence sufficiency is reached (or max level hit).

## 3) Context-bounded evidence packing

Before retrying patcher, orchestrator builds a bounded high-signal delta pack:

- max snippets (default 8)
- max chars per snippet (default 1000)
- max total evidence chars (default 10000)
- delta-only evidence vs pass-1
- score-based ranking
- diversity-first selection across files
- near-duplicate removal
- deterministic overflow drops + counters logged

## 4) Patcher retry loop + critic handoff

The old generic revise flow was replaced with deterministic loop control:

1. Attempt patch generation
2. Evaluate quality gates (`apply_ok`, `allowed_files_ok`, `valid_unified_diff`, confidence)
3. If not sufficient and triggers fire, inject orchestrator refinement delta
4. Retry patcher with enriched context
5. Stop after bounded attempts and hand off to critic

Handoff statuses now include:

- `CRITIC_HANDOFF_READY`
- `CRITIC_HANDOFF_WITH_FAILURE`

## 5) Eval-time toolset integration

`colab_patcher_orchestrator_eval.py` now includes an eval wrapper toolset for orchestrator operations:

- path existence checks (git object exists at base SHA)
- GraphRAG candidate extraction from row context
- repo keyword search
- line-level code reads
- interface/service-call style tracing (`def/class/import/from`)

## Runtime Config (Env Vars)

New/used knobs:

- `MAX_PATCHER_ATTEMPTS`
- `ORCH_MAX_SNIPPETS`
- `ORCH_MAX_CHARS_PER_SNIPPET`
- `ORCH_MAX_TOTAL_EVIDENCE_CHARS`
- `ORCH_MODEL_CONTEXT_TOKENS`
- `ORCH_RESERVED_TOKENS`
- `ORCH_MAX_ESCALATION_LEVEL`

Existing knobs still used:

- `MODEL_NAME`
- `ADAPTER_PATH`
- `EVAL_JSONL`
- `REPO_DIR`
- `SMOKE_N`
- `MAX_NEW_TOKENS`
- `N_CANDS`
- `REVISE_N`

## How To Run

Example:

```bash
python colab_patcher_orchestrator_eval.py
```

Optional tuned run:

```bash
MAX_PATCHER_ATTEMPTS=3 \
ORCH_MAX_SNIPPETS=8 \
ORCH_MAX_TOTAL_EVIDENCE_CHARS=10000 \
ORCH_MAX_ESCALATION_LEVEL=3 \
python colab_patcher_orchestrator_eval.py
```

## Expected Impact

- better recall via targeted retrieval
- fewer path-drift failures
- safer retries (bounded + deterministic)
- cleaner handoff to critic with better evidence trail

## Next Suggested Step

Add per-sample JSON trace export for:

- trigger flags
- escalation level
- selected vs dropped snippets
- attempt-by-attempt outcomes

This will make pass1 vs pass2 gain analysis much easier for reporting.
