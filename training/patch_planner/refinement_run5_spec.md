# Run5 Orchestrator Refinement Spec

## Objective

Run5 has narrowed the major error modes. The remaining misses are mostly:

- recall misses on novel or weakly represented patterns,
- path drift (correct intent, wrong file choice),
- sparse-symbol cases where initial evidence is too thin.

The orchestrator should therefore act as a **deterministic evidence retriever and compressor**, not a second planner. It enriches context and re-invokes the same planner once.

## Role Boundaries

- Planner remains the decision-maker for `REQUIRES_CODE_CHANGE`, files, and rationale.
- Orchestrator decides:
  - whether refinement is needed,
  - what retrieval tools to escalate,
  - how to compress and package new evidence.
- Orchestrator does **not** override planner output directly unless hard guardrails are violated (for example, disallowed path).

## Control Flow (Single Deterministic Retry)

1. Run planner with current fast bundle (pass 1).
2. Compute deterministic trigger signals.
3. If no trigger, return pass-1 result.
4. If trigger fires, execute escalation ladder (bounded by level cap).
5. Build delta evidence pack (new evidence only).
6. Re-run the same planner once (pass 2).
7. Return pass-2 output plus orchestration trace metadata.

No recursive loops in phase 1. Maximum planner calls per issue: **2**.

## Planner Inputs by Pass

### Pass 1 (current baseline)

Use existing bundle:

- issue + task instruction,
- planner directive schema,
- narrow GraphRAG candidates,
- current Tree-sitter spans,
- constraints.

### Pass 2 (refinement)

Send **delta-only refinement payload**:

- new candidate files not present in pass 1,
- newly discovered symbols/interfaces,
- conflict markers (supporting and contradicting signals),
- compact reranked candidate table,
- explicit revision instruction.

Avoid replaying all pass-1 evidence to keep token use bounded and signal-dense.

## Deterministic Trigger Rules

Trigger refinement when any of the following is true:

1. **Weak NO**
   - pass-1 decision is `NO`, and confidence is below threshold (for example `< 0.70`) or rationale is sparse.
2. **Sparse spans**
   - Tree-sitter evidence count under minimum (for example `< 2` useful spans).
3. **Path mismatch**
   - top GraphRAG historical files and planner target files have low overlap.
4. **Low-evidence quality**
   - retrieved snippets are short, stale, or conflict-heavy.

Suggested phase-1 policy: trigger if **any one** rule fires; use fixed thresholds committed in config.

## Tool Escalation Policy (Strict Ladder)

Escalate level-by-level and stop as soon as evidence sufficiency criteria are met.

### Level 0 (default)

- Narrow GraphRAG neighborhood around current top files.
- Existing Tree-sitter symbol spans.

### Level 1

- Wider GraphRAG expansion (larger top-k, one extra hop).
- Repository keyword search seeded by issue + planner reason terms.

### Level 2

- Targeted line-level file reads on top reranked candidates.
- Interface tracing:
  - imports/exports,
  - call-site to definition links,
  - related config constants.

### Level 3 (high ambiguity fallback)

- Deeper bounded traversal for unresolved ambiguity:
  - additional transitive symbol hops,
  - broader conflicting-path sampling.

Level 3 is only allowed when ambiguity flag remains high after Level 2.

## Evidence Read/Compression Strategy

Full retrieval output stays outside the planner prompt. The prompt receives only compressed ranked snippets.

### Snippet Contract (bounded)

Each snippet should include:

- `path`
- `symbol` (or `unknown`)
- `excerpt` (short, bounded)
- `reason_tag` (`supports_path`, `conflict_path`, `api_contract`, `test_signal`, `novel_pattern`)
- `score` (deterministic ranking score)

### Compression Rules

- hard cap on snippet count (for example 8 to 12),
- hard cap on excerpt length per snippet,
- deduplicate near-identical snippets,
- prioritize evidence diversity across files/symbols.

## Refinement Payload Back to Planner

Use a fixed schema with five sections:

1. `initial_summary`
   - pass-1 decision, confidence, top files.
2. `new_evidence`
   - compressed snippets from escalated retrieval.
3. `reranked_candidates`
   - deterministic candidate file table with scores and tags.
4. `conflicts`
   - explicit contradictory signals and tie-break hints.
5. `revision_instruction`
   - "revise prior decision using only this delta; keep constraints unchanged."

## Reranking and Conflict Handling

Deterministic reranking score should combine:

- GraphRAG similarity,
- symbol/linkage strength,
- lexical match to issue terms,
- historical fix frequency,
- contradiction penalties.

When evidence conflicts:

- keep both supporting and contradicting snippets,
- require planner to choose one path and explain why,
- log conflict density for future trigger learning.

## Metrics to Validate Orchestrator Value

Track pass-1 vs pass-2 metrics on identical eval sets:

- `NO -> YES` correction count and rate,
- precision delta (guard against false-positive inflation),
- recall lift (primary target),
- path-hit improvement (target file overlap with gold),
- apply-check pass delta for downstream patch generation,
- latency overhead (p50/p95),
- token/cost overhead per solved case.

Minimum acceptance target for phase 1:

- measurable recall gain with precision drop within predefined budget,
- bounded latency increase (for example <= 1.5x p50).

## Logging Contract (Phase 1 Required)

Per sample, persist:

- trigger flags + thresholds,
- max escalation level reached,
- snippets selected for prompt,
- planner pass-1 and pass-2 outputs,
- final chosen output,
- metric labels used for dashboarding.

This trace is required to debug false flips and tune thresholds.

## Phased Implementation Plan

### Phase 1 (now)

- deterministic trigger rules,
- single retry loop (max one refinement pass),
- Levels 0-1 enabled,
- full decision/evidence logging.

### Phase 2

- enable Levels 2-3 for ambiguous cases,
- deterministic reranking upgrade,
- stronger conflict-aware compression.

### Phase 3

- learned trigger policy from phase-1/2 logs,
- learned escalation stopping policy,
- budget-aware dynamic retrieval depth.

## Non-Negotiable Constraints

- orchestrator enriches planner; it does not replace planner logic,
- only one deterministic refinement retry in phase 1,
- pass-2 prompt is delta-evidence-only,
- retrieval remains bounded and reproducible,
- every refinement decision is explainable via logs.
