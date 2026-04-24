# Patch Planner Run5 Results (Working Summary)

This file is a quick-access summary of run5 outcomes used to guide next-step design.

## Context

- Notebook: `training/patch_planner/planner_training_run5.ipynb`
- Data builder: `training/patch_planner/build_planner_data.py`
- Eval artifacts path (run convention): `evals/planner/v5`

## High-level conclusions

1. Run5 improved grounding quality and reduced broad failure modes from prior runs.
2. Remaining misses are relatively narrow and mostly recall-oriented (`false NO` patterns).
3. Many misses look like retrieval/context-shaping gaps (path drift, sparse context, indirect evidence) rather than core generation collapse.
4. Best next improvement is orchestrator-assisted conditional refinement, not unconditional multi-pass prompting.

## Operational observations from run5 cycle

- Best checkpoint behavior was reached during training; eval reruns should load adapters and run eval-only flow.
- Post-restart eval requires loading base model + tokenizer + adapter (without re-creating fresh LoRA adapters).
- Splitting model-load and `get_peft_model(...)` into separate cells avoids adapter stacking/key mismatch errors.

## Known gap categories to target next

- Recall misses on valid issues where exact file match is absent but module-level evidence exists.
- Novel issue wording where historical nearest neighbors are weak.
- Cases with sparse/empty tree-sitter spans for top candidates.
- Historical path drift where file normalization and fallback retrieval are needed.

## Proposed next action

Implement a deterministic orchestrator refinement loop for low-confidence planner outputs:

- pass-1 planner (current path),
- conditional retrieval expansion,
- pass-2 planner with compact evidence delta,
- strict schema + grounding validation before final output.

Detailed design and trigger policy:
- `next_steps/patch_planner/README.md`

## TODO: fill exact metric table

When final run5 eval metrics are consolidated, append:

- YES precision / recall / F1
- Recall@1 / Recall@3 / Recall@6 (if applicable)
- G-Eval aggregate and key rubric buckets
- first-pass vs second-pass (future orchestrator baseline)
