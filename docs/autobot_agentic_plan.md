# AutoBot Agentic System — 2-Week Build Plan
> Planner → Patcher → Critic pipeline with GraphRAG, MCP, Neo4j, VS Code plugin, RLHF

---

## 1. The Three Models — What They Do and How They're Trained

### Planner
**Role:** Given an issue, predict *which files to modify* and *what intent each file serves*.

**Input at inference:**
- Issue title + body
- Top-K candidate files retrieved from Neo4j symbol graph (GraphRAG prior)
- Tree-sitter structural grounding for those files (symbol names, kinds, line ranges; optional short spans)
- Recent similar issue resolutions (GraphRAG context)

**Output:** Structured plan — `[{file: "path/to/file.py", intent: "Fix the X method to handle Y edge case"}, ...]`

**Training data (from your JSONL):**
- `issues.jsonl` → issue body
- `prs.jsonl` → `files[].filename` from the resolving PR = ground truth file list
- Pair: `(issue_body + retrieved_candidates) → file_list + intents`

**Planner target cleaning rule (important):**
- Before writing `PLAN -> What to change`, clean PR body text to remove template/license noise.
- Strip HTML comments, markdown image/link clutter, and boilerplate contribution text.
- Keep only concise engineering intent (short capped text) so Patcher receives unambiguous guidance.

**Loss:** Next-token prediction on the structured plan. At eval, measure **File Recall@K** (did the model predict the right files?).

---

### Patcher
**Role:** Given a plan + file contents, generate a **unified diff** (git patch).

**Input at inference:**
- Issue body + Planner output (file list + intents)
- Actual file contents fetched live via MCP/GitHub API
- Historical similar patches from Neo4j (few-shot context)

**Output:** `git diff`-format unified patch

**Training data:**
- `prs.jsonl` → `files[].patch` field = ground truth unified diff
- Input constructed as: `<issue>\n<file_content>\n<intent>` → `<unified_diff>`
- **Key:** Each training example is ONE file's patch, not the whole PR. This keeps context manageable.
- Final builder enforces strict dataset validation and exports train/eval/test splits + dataset report for reproducibility.
- Final one-off build includes multi-extension patch files, while Python files receive Tree-sitter AST span enrichment where available.
- When AST spans are unavailable, fallback hunk-window spans are injected to avoid zero-context patch rows.

**Loss:** Next-token prediction on the diff output.

**Context size concern:** Qwen2.5-Coder-7B has a 32k token window. A full file can be large. Strategy: pass only the **relevant function + surrounding context** (20 lines before/after the identified symbol), not the full file. Tree-sitter tells you exactly which function to extract.

---

### Critic
**Role:** Evaluate a generated patch. Predict review decision + generate actionable feedback.

**Input at inference:**
- The generated patch (from Patcher)
- CI check-run conclusions (`ci_conclusion` field)
- Historical review patterns for similar PRs (Neo4j)

**Output:** `{decision: "APPROVE" | "REQUEST_CHANGES", feedback: "..."}` + line-level comments

**Training data:**
- `prs.jsonl` → `reviews[]` field contains real human reviews with `state` (APPROVED/CHANGES_REQUESTED) and `body`
- Also `review_comments[]` for inline feedback
- Training pair: `(patch + ci_conclusion) → review_state + review_body`

---

## 2. Why Tree-Sitter Heuristics Are Wrong — and What to Do Instead

Your instinct is correct. **Heuristics are fragile** ("if issue mentions `operator`, look in `airflow/operators/`") because:
- They don't generalize to new subsystems
- They fail on indirect dependencies ("issue mentions `dag` but fix is in `serialization/`")
- They require manual maintenance

### The Right Approach: Symbol-Level GraphRAG via Neo4j

**Build once (offline, from your repo's tree-sitter output):**

```
Neo4j Graph Schema:
  (:File {path, module})
  (:Symbol {name, type}) — functions, classes, decorators
  (:Issue {number, title, body_embedding})
  (:PR {number, files_touched[]})

  (:File)-[:DEFINES]->(:Symbol)
  (:Symbol)-[:CALLS]->(:Symbol)       # cross-file call graph
  (:Symbol)-[:IMPORTS]->(:Symbol)
  (:PR)-[:TOUCHES]->(:File)
  (:Issue)-[:RESOLVED_BY]->(:PR)
  (:Issue)-[:SIMILAR_TO]->(:Issue)    # embedding similarity edges
```

**At inference (Planner retrieval step):**
1. Embed the issue text
2. Find top-5 `(:Issue)` nodes by embedding similarity in Neo4j
3. Follow `RESOLVED_BY → TOUCHES` edges to get historically-touched files
4. Rank by frequency across similar issues
5. Pass top-10 candidate files to Planner — **no heuristics, pure data-driven**

This replaces your heuristic subset entirely and is more accurate because it learns from actual resolution history.

---

## 3. RLHF — Easiest Yet Impactful: DPO on the Critic

**Why Critic is the best choice for RLHF:**
- You already have **implicit human preference labels in your data** — no new labeling needed
- Every PR with multiple reviews has a natural preference ordering: final APPROVED review > earlier CHANGES_REQUESTED reviews
- DPO (Direct Preference Optimization) requires NO separate reward model — just preference pairs

**DPO dataset construction (from your existing `prs.jsonl`):**

For each PR that went through review rounds:
```
chosen:   reviews where state = "APPROVED" + its body text
rejected: reviews where state = "CHANGES_REQUESTED" for same PR (earlier rounds)
```

This gives you pairs like:
- `(patch, "LGTM, this correctly handles the null case") ← chosen`
- `(patch, "This will fail when input is None") ← rejected`

**Why this works:** The model learns that good reviews identify *what is correct* about a patch, not just *what is wrong*. This aligns the Critic toward actionable, constructive feedback — which is exactly what you want in a VS Code plugin.

**Implementation:** TRL library's `DPOTrainer` on top of your SFT-trained Critic. 3-4 hours of A100 time. No human raters needed.

**Use GitHub API credits for:** labeling ~500 borderline cases where the review body is ambiguous (very short reviews like "LGTM" without explanation). GitHub Copilot or GPT-4 can help score these in batch.

---

## 4. System Architecture

```
VS Code Plugin
     │
     │ (issue context + workspace files)
     ▼
Orchestrator (FastAPI)
     │
     ├──▶ MCP Server ──▶ GitHub API (live: PR status, file contents, CI)
     │
     ├──▶ Neo4j (GraphRAG: similar issues, historical file patterns)
     │
     ├──▶ HuggingFace Inference Endpoint
     │         ├── Planner (Qwen2.5-Coder-7B-SFT)
     │         ├── Patcher (Qwen2.5-Coder-7B-SFT)
     │         └── Critic  (Qwen2.5-Coder-7B-SFT+DPO)
     │
     └──▶ Response: {plan, patch, critique} → Plugin renders diff + review inline
```

**Orchestrator call chain (per issue):**
1. Receive issue number from plugin
2. Fetch issue body via MCP
3. Query Neo4j → get candidate files + similar past resolutions
4. Call `Planner` → structured plan
5. Run orchestrator refinement on YES plans (bounded tool phase): read top candidate code spans, verify evidence, and refine plan details
6. Present refined plan for HITL approval
7. Fetch file contents via MCP for approved planned files only
8. Call `Patcher` per file → unified diffs
9. Assemble full patch, fetch latest CI status via MCP
10. Call `Critic` → decision + feedback
11. If critique indicates strategy-level mismatch (wrong subsystem/file assumptions), re-enter `Planner`; otherwise keep iterating `Patcher` with critic/sandbox feedback
12. Return everything to plugin

**Loop policy (important):**
- Default: `Planner` once, then `Patcher <-> Critic` bounded retries
- Re-enter `Planner` only for strategy-level failures or new issue evidence

**Refinement policy (important):**
- If Planner says YES: always run refinement before HITL approval.
- If Planner says NO: run only a cheap guard check; refine only on contradiction.
- Refinement is bounded search, not open-ended repo crawling.

---

## 5. Is Qwen2.5-Coder-7B Sufficient?

**Yes, with caveats:**

| Model | Sufficient? | Concern |
|---|---|---|
| Planner | ✅ Yes | File retrieval is ranking + generation — 7B handles well |
| Patcher | ✅ Yes | Code gen within 32k window — 7B is strong here |
| Critic | ✅ Yes | Review generation is text-to-text — 7B with DPO works well |

**One real concern:** PRs touching >30 files. Solution: The Planner only selects top-5 files. The Patcher generates one file's patch at a time. Context never blows up.

**Alternative if quality is insufficient:** Qwen2.5-Coder-14B. Same API, 2x the A100 time, meaningfully better on multi-file reasoning.

---

## 6. Evaluation — Proving Gains Over Baseline

### Test Set Strategy
- **Split by time:** Train on issues/PRs with `created_at < 2024-01-01`, test on `2024-01-01 to 2025-01-01`
- This exactly mimics real deployment (model trained on past, evaluated on future)
- Hold out ~3,000–4,000 PRs for test set

### Per-Model Metrics

**Planner:**
| Metric | Baseline | Target |
|---|---|---|
| File Recall@3 | BM25 on issue text | +15% over BM25 |
| File Recall@5 | BM25 | +10% |
| Exact File Match | Random retrieval | +40% |

Baseline = BM25 over file paths + docstrings. Run it as a simple `rank_bm25` retrieval and compare.

**Patcher:**
| Metric | Definition |
|---|---|
| CodeBLEU | Match against actual PR diff (structural + token overlap) |
| Compilation Rate | Does `python -m py_compile` pass on the patched file? |
| Test Pass Rate | Run repo's existing test suite on the patch (use Docker + the test suite) |
| Edit Distance | Levenshtein distance between generated and actual diff |

Baseline = the unmodified file (zero diff) — trivially passes compilation but scores 0 on CodeBLEU.
Stronger baseline = GPT-4o zero-shot with same prompt (expensive but meaningful comparison).

**Critic:**
| Metric | Definition |
|---|---|
| Decision Accuracy | APPROVE vs REQUEST_CHANGES — compare to actual human decision |
| BERTScore | Semantic similarity of generated review body to actual human review |
| ROUGE-L | Recall of actual review content in generated review |

Baseline = always predict APPROVE (majority class). Your fine-tuned model should beat this by 20%+ on decision accuracy.

### End-to-End Eval (Most Impressive for Demo)
Pick 30 real issues from your test set that have clear, merged PRs.
1. Run your pipeline: Planner → Patcher → Critic
2. Compare generated patch to actual merged PR
3. Human score 1-5: "Would you merge this patch?" (4 team members = 120 ratings)
4. Report: mean score vs GPT-4o baseline on same issues

---

## 7. 2-Week Sprint — 4 Members

### Week 1: Build in Parallel

| Member | Days 1–5 |
|---|---|
| **M1: ML Lead** | Train all 3 models on A100. Fix tree-sitter → replace with Neo4j symbol retrieval for training inputs. Push models to HuggingFace. Build SFT → DPO pipeline for Critic. |
| **M2: Graph + Data** | Set up Neo4j. Ingest code graph from tree-sitter. Ingest issue/PR resolved_by edges from your JSONL. Build embedding similarity index. Test GraphRAG queries for Planner retrieval. |
| **M3: Backend** | Build FastAPI orchestrator. Integrate MCP server (GitHub API calls: file fetch, issue fetch, CI status). Wire Planner → Patcher → Critic chain. Add Neo4j query step. |
| **M4: Frontend** | VS Code extension scaffold. Issue detection (parse GitHub URLs in editor). Call orchestrator. Render diff inline using VS Code diff editor API. Show Critic feedback as diagnostics. |

### Week 2: Integrate, Evaluate, Polish

| Day | Work |
|---|---|
| **Day 8–9** | Full integration: Plugin ↔ Orchestrator ↔ Models ↔ Neo4j ↔ MCP. Fix integration bugs. |
| **Day 10** | DPO training on Critic. Use TRL `DPOTrainer`. Run overnight on A100. |
| **Day 11** | Run full eval suite on test set. Compute all metrics. Compare vs baseline. |
| **Day 12** | Human eval: 4 members each score 30 issues (2 hours). Compile results. |
| **Day 13** | Fix top-3 failure modes found in eval. Buffer for rate limit / infra issues. |
| **Day 14** | End-to-end demo prep. Record a live demo video. Write results section. |

---

## 8. Key Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Patcher context overflow on large files | Truncate to relevant function ± 20 lines using tree-sitter symbol extraction |
| Neo4j graph build takes too long | Use only the main `airflow/` directory, not all 500k lines. Pre-filter to ~3k Python files |
| HuggingFace cold start latency | Deploy with `text-generation-inference` (TGI) and keep endpoint warm |
| DPO makes Critic worse (reward hacking) | Use β=0.1 (conservative KL penalty). Fallback to SFT-only if DPO hurts decision accuracy |
| MCP server GitHub rate limits | Cache file contents in Redis with 1hr TTL. Use same GitHub App credentials |
| VS Code diff rendering complexity | Use `vscode.diff` command with virtual documents — 20 lines of code, not custom UI |

---

## 9. One-Paragraph Summary for Your Team

> We're building a 3-model agentic pipeline (Planner → Patcher → Critic) fine-tuned from Qwen2.5-Coder-7B on 12k closed issues and 41k PRs extracted from apache/airflow. The Planner predicts which files to modify using GraphRAG over a Neo4j code symbol graph (replacing fragile heuristics). The Patcher generates per-file unified diffs given file content fetched live via MCP. The Critic evaluates patches using DPO-trained preferences derived from real GitHub review history. All three models are served on HuggingFace and coordinated by a FastAPI orchestrator that a VS Code plugin calls when opening any GitHub issue. Gains are proven by File Recall@K (Planner), CodeBLEU + compilation rate (Patcher), and decision accuracy + BERTScore (Critic) on a time-split test set of 3,000 post-2024 PRs, plus a 30-issue human eval scored by the team.
