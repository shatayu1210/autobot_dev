# Patch Planner Orchestrator Next Steps (Post Run5)

## Why this exists

Run5 substantially improved planner grounding and reduced major failure modes, but remaining misses are now concentrated in narrower cases:

- **Recall misses (false NO)** on valid but indirect fix opportunities.
- **Novel issue phrasing** where exact historical path/file overlap is weak.
- **Path drift / monorepo movement** where true target exists but retrieval confidence is fragmented.

At this stage, additional gains are less about bigger base training and more about a deterministic **orchestrator-assisted refinement loop** that enriches planner context only when needed.

---

## Run5 conclusions that drive this plan

Reference summary: `training/patch_planner/run5_results.md`

Key observed outcomes from run5:

- Model behavior is much better grounded than earlier runs.
- Residual failures are concentrated in **recall** (false NO) rather than broad hallucination.
- Evaluation traces indicate many misses are retrieval/evidence-shaping problems, not purely generation problems.
- A limited second-pass refinement loop is likely the highest-ROI improvement path.

Implication:
- Keep planner strict-grounding policy.
- Add deterministic orchestrator refinement only on low-confidence cases.

---

## What “orchestrator refinement” should mean

The orchestrator should **not silently rewrite the planner answer** in most cases.  
Its primary job is to:

1. detect uncertainty or weak evidence in planner output,
2. run targeted retrieval/traversal tools,
3. compress results into a bounded context pack,
4. re-prompt planner for a second-pass decision.

Only if the second pass still fails basic validity checks should orchestrator apply strict guardrail handling (for example: abstain, request human confirmation, or return low-confidence result).

---

## Initial planner vs refinement planner

### Initial planner input (fast path)

- Issue title/body + labels + summary context
- Top GraphRAG candidate files
- Tree-sitter snippets for top candidates
- Existing system prompt with strict grounding policy

### Refinement input (conditional second pass)

The second pass should receive:

- Initial planner output + confidence flags
- Expanded retrieval pack (only from triggered tools)
- Explicit “delta findings” section:
  - newly discovered candidate files/modules
  - high-signal symbols/functions
  - contradictory evidence vs first pass
- Tight token budget and ranked evidence list

This keeps refinement focused and avoids exploding context size.

---

## Deterministic refinement triggers

Trigger refinement when one or more are true:

1. Planner outputs `NO` with weak/no concrete file evidence.
2. Planner cites only 1 low-confidence candidate file.
3. Candidate files and tree-sitter spans are sparse or empty.
4. Issue semantics strongly imply code change, but plan is abstaining.
5. Path mismatch indicators detected (historical file paths not found).

Do not always refine; keep fast path default.

---

## Tool policy: when to use which retrieval

Use an explicit escalation ladder.

### Level 0 (already done in fast path)

- GraphRAG top-k (narrow)
- tree-sitter symbol spans for top files

### Level 1 (cheap expansion)

- **Wider GraphRAG**: increase top-k and include second-hop neighbors.
- **Repo keyword search**: targeted symbol/keyword search from issue terms and candidate modules.

Use when initial evidence is thin but not contradictory.

### Level 2 (precision verification)

- **Line-level code reads** of top 3-8 candidate files around matched symbols/keywords.
- **Interface tracing** (imports/callers) for suspected symbols.

Use when first-pass plan is `NO` or uncertain but Level 1 found plausible targets.

### Level 3 (deep fallback)

- **Deeper GraphRAG traversal** (additional hops / PR-review evidence / related files).
- **Cross-check with historical patch idioms** for similar issue clusters.

Use only for high-value issues or persistent ambiguity.

---

## Recommended orchestrator loop (single retry)

1. Run planner pass-1.
2. Score confidence + evidence quality.
3. If no trigger: return pass-1.
4. If trigger:
   - execute escalation ladder until enough evidence found or budget reached,
   - build compact refinement bundle,
   - run planner pass-2 with explicit instruction to revise only if evidence justifies it.
5. Validate pass-2 schema/grounding.
6. Return final result + confidence metadata.

Default to one refinement round only (deterministic latency).

---

## Context and token management strategy

The orchestrator should never stream raw file dumps into planner.

Use a two-stage memory model:

1. **Evidence store (full)**: raw retrieval outputs kept outside planner prompt.
2. **Planner bundle (compressed)**: only top-ranked snippets and metadata injected.

Compression rules:

- Keep top 5-12 snippets max (ranked).
- Each snippet includes:
  - file path
  - symbol/function header
  - short excerpt window
  - evidence reason tag
- Drop duplicate/near-duplicate snippets.
- Prefer diversity across modules over many snippets from one file.

This is how line-by-line reads remain useful without exceeding context limits.

---

## What to send back to planner during refinement

Structured block recommended:

1. `INITIAL_DECISION_SUMMARY`
2. `NEW_EVIDENCE_FOUND`
3. `CANDIDATE_FILES_RERANKED`
4. `CONFLICTS_OR_UNCERTAINTIES`
5. `REVISION_INSTRUCTION` (revise only if better grounded)

Planner output should include:

- final YES/NO
- top target files
- short grounded rationale
- confidence
- whether refinement changed decision

---

## Suggested confidence rubric (for orchestration only)

- **High**: multiple aligned file/symbol matches + non-empty TS context.
- **Medium**: partial alignment, some inferred paths.
- **Low**: weak/no concrete code evidence.

Only `Low` should almost always trigger refinement.

---

## Evaluation upgrades for orchestrator path

Track metrics split by:

- no-refinement vs refinement-triggered cases,
- first-pass NO -> second-pass YES flips,
- precision impact of flips,
- recall gains on previously missed positives.

Add dashboards:

- refinement trigger rate,
- average retrieval depth per resolved case,
- latency/token cost delta.

Success criterion: recall gain with minimal precision regression.

---

## Practical implementation phases

### Phase 1 (now)

- Implement trigger logic + one refinement retry.
- Add Level 1 and Level 2 retrieval policies.
- Log evidence bundles for offline analysis.

### Phase 2

- Add deeper GraphRAG fallback for a small subset.
- Add lightweight reranker for snippet selection.

### Phase 3

- Learn trigger policy from historical eval traces.
- Tune dynamic budgets by issue class/severity.

---

## Open design decisions

1. Should orchestrator ever override planner final decision?
   - Recommended: no hard override; prefer planner pass-2 with richer evidence.
2. How much latency budget per issue?
   - Recommended default: single retry with capped retrieval operations.
3. Should novelty detection have custom triggers?
   - Recommended: yes, based on low similarity + semantic mismatch patterns.

---

## Immediate action items

1. Add orchestrator trigger + retry contract to planner pipeline.
2. Implement retrieval ladder with per-level budget caps.
3. Define refinement bundle schema and logging.
4. Extend eval scripts to report first-pass vs second-pass deltas.
5. Run targeted benchmark on run5 failure bucket to quantify recall lift.
