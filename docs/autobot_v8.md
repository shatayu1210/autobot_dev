# AutoBot — Training & Architecture Reference v8

This is the canonical architecture/training reference for the patch pipeline.

---

## Final Planner Decisions

1. **Hybrid retrieval is final**
   - GraphRAG supplies historical priors (`top-K` candidate files).
   - Tree-sitter supplies structural grounding for those files (symbols and ranges).
2. **Planner output is file-first**
   - `REQUIRES_CODE_CHANGE` + `REASON` + `PLAN`.
   - No trained `CONFIDENCE` output class.
3. **Retrieval confidence is a cue, not label**
   - Keep it in prompt/context for human/orchestrator.
   - Do not train planner to emit confidence buckets.
4. **Top-K defaults**
   - GraphRAG `K = 6`.
   - Tree-sitter context includes bounded per-file symbol context only.

## Planner Target Cleaning (Final Training Rule)

To keep Planner outputs patcher-actionable, `What to change` targets are cleaned before dataset write:

- strip HTML comments and markdown image embeds
- strip raw URLs
- remove ASF/license and PR-template boilerplate lines
- collapse whitespace and keep concise intent text
- cap to a short word budget (current builder: 180 words)

This prevents the planner from learning noisy "template prose" and improves downstream patch execution clarity.

---

## Orchestrator Refinement (Code-Level Behavior)

Refinement is not just natural language prompting. It is a bounded tool workflow:

1. Run Planner once on initial context.
2. If `REQUIRES_CODE_CHANGE = YES`, run a refinement pass:
   - fetch code spans for top planned files/symbols,
   - score evidence quality,
   - optionally re-rank files,
   - produce a refined plan for HITL approval.
3. If planner says `NO`, run only a cheap verification probe (1-2 targeted checks). Escalate to refinement only if contradiction is strong.
4. After HITL approval, run Patcher -> Sandbox -> Critic loop.

Pseudo-flow:

```python
plan = planner(issue, graphrag_ctx, treesitter_ctx)
if plan.requires_code_change == "YES":
    refined = refine_with_tools(plan, issue)
    human_approve(refined)
    run_patch_loop(refined)
else:
    if quick_no_guard_check(issue):
        return summary_path(plan)
    refined = refine_with_tools(plan, issue, forced=True)
    human_approve(refined)
    run_patch_loop(refined)
```

---

## Replan Policy

Default runtime policy is:
- Planner once -> Patcher/Critic bounded retries

Re-enter Planner only on strategy-level signals:
- wrong subsystem/file family selected,
- repeated failures due to wrong symbol/file assumptions,
- new issue evidence appears mid-run (comments/scope change).

Do not replan for ordinary implementation errors; keep those in patch retries.

---

## Novel-Issue Fallback

If GraphRAG is sparse or empty:
1. broaden candidate search with bounded keyword/symbol matching,
2. probe candidate files with limited code-span reads,
3. re-score and refine plan,
4. proceed to HITL gate.

Use hard budgets to avoid endless scans:
- max files per round,
- max symbols per file,
- max lines per symbol,
- max refinement rounds.

---

## Required Ablations

Run and report all 3:
- GraphRAG-only
- Tree-sitter-only
- Hybrid (target)

Track:
- File Recall@3/@5
- hallucination rate
- exact file match
- novel-slice performance (GT not in top-K).

---

## Patcher Dataset Finalization Notes

For final one-off patcher training, dataset construction is strict and reproducible:

- Source pool: PR records from `prs_clean.jsonl`
- Fast prefilter before expensive processing (numeric + file-type checks)
- Multi-extension support (Python + common TS/JS/config/doc files)
- Tree-sitter spans persisted for Python files where blob lookup is available
- fallback hunk-window spans injected when AST/blob context is unavailable, preserving contextual grounding
- GraphRAG file candidates and historical idioms persisted with query metadata
- PR-level 80/10/10 split and hard validation before write
- End-of-run console summary + `dataset_report.json` for auditability

This balances robustness and speed under tight timeline constraints while keeping training/inference constraints aligned.
