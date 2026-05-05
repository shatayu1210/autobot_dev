# AutoBot training loop — Critic

Focused goal: **train the Critic (patch verdict agent) in an AI-assisted loop until baseline vs tuned metrics converge**.

Assume your repo root is `autobot_dev` on your machine.

**Context wiring for this agent:** `build_critic_data.py` pulls **Neo4j** (“historical review friction” via review/file relationships) and **PR payloads** (`prs_clean.jsonl`). It does **not** currently read `treesitter_index.json`. Tree-sitter is optional for consistency with repo hygiene or future builder upgrades, not required for today’s critic JSONL pipeline.

---

## 1) Set up environment

**Why we need this:** reproducible dependencies prevent random training/eval failures.

```bash
cd autobot_dev
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2) Refresh ETL training data

**Why we need this:** merged vs unmerged labels and review text come from cleaned PR exports; stale files hurt verdict quality.

Reference:
- `autobot_dev/etl/training_data/README.md`

Required files:
- `autobot_dev/etl/training_data/issues_clean.jsonl`
- `autobot_dev/etl/training_data/prs_clean.jsonl`
- `autobot_dev/etl/training_data/cleaning_report.json`

---

## 3) Start GraphRAG and ingest Neo4j

**Why we need this:** critic examples include **Neo4j-backed** friction snippets; ingestion must reflect current graph schema and PR linkage.

Reference:
- `autobot_dev/graphrag/README.md`

```bash
cd autobot_dev/graphrag
docker compose up -d

cd autobot_dev
source .venv/bin/activate
python graphrag/ingest_graph_actual.py
```

Optional:
```bash
python graphrag/vectorize_issues.py
```

Sanity queries:
- `MATCH (n) RETURN count(n);`
- `MATCH ()-[r]->() RETURN count(r);`

---

## 4) Tree-sitter index (optional for Critic track)

**Why we need this:** not consumed by **`build_critic_data.py`** today. Rebuild if you refreshed the Airflow clone for parity with Planner/Patcher, or plan to extend the critic prompt with structural code context later.

Reference:
- `autobot_dev/tree_sitter/README.md`

```bash
cd autobot_dev/tree_sitter
python3 build_treesitter_index.py \
  --repo "autobot_dev/tree_sitter/airflow" \
  --output "autobot_dev/tree_sitter/treesitter_index.json"
```

Skip this step entirely if critic-only iteration does not touch that stack.

---

## 5) Rebuild Critic training data

**Why we need this:** labels (`ACCEPT` / `REVISE` / `REJECT`-style framing in `output`), input template, and GraphRAG joins are fully determined here.

**Builder script:**
- `autobot_dev/training/patch_critic/build_critic_data.py`

**Main output:**
- `autobot_dev/training/patch_critic/critic_train_graphrag.jsonl`

See also project notes:
- `autobot_dev/training/patch_critic/critic_handover.md`

If you rebalance classes, change truncation (plan/diff caps), or adjust Neo4j queries, rerun the builder and capture the exact CLI or script invocation you used.

---

## 6) Train Critic adapter

**Why we need this:** this is where the VERDICT + REASONING format and calibration become stable.

Work under:
- `autobot_dev/training/patch_critic/`

Version adapters and configs per run (`v1`, `v2`, …).

---

## 7) Evaluate baseline vs tuned

**Why we need this:** deltas vs base expose overfitting or class collapse.

Evaluate **baseline** and **tuned** on the **same** held-out rows.

### Evaluation protocol (required)
- One main notebook/script for **train + eval**.
- Same eval IDs for base and tuned.
- **~30-example** deterministic quick check after each train.
- Larger confirmation eval before declaring a checkpoint “final.”

### Critic-focused metric set
- Accuracy and **macro-F1** (or per-class precision/recall) on parsed `VERDICT` vs gold label
- Confusion-matrix stress (which classes get confused)
- “Format validity” rate: outputs parse as `VERDICT: …` / `REASONING: …`
- G-Eval on reasoning quality conditioned on verdict (sampled)
- Latency / token deltas

Do **not** use BLEU/ROUGE as primary decision metrics.

Store runs under something like:
- `autobot_dev/evals/critic/vX/...`

---

## 8) AI-assisted improvement loop

**Why we need this:** fastest convergence is targeted changes to Neo4j fields, builder logic, or training objective.

After each train+eval round:

1. Share **`autobot_dev/training/patch_critic/build_critic_data.py`**, exact build invocation, train config, baseline vs tuned metrics, and confusing verdict examples (with inputs redacted if needed).
2. Ask for fixes in: trainer config, **build_critic_data** (queries, caps, splits, balancing), or **GraphRAG** schema/ingestion assumptions (§3). Tree-sitter only if you deliberately add that signal to inputs.
3. Apply → rebuild data → retrain → re-evaluate.

### Prompt to AI after each run (copy/paste)

```text
You are reviewing my latest AutoBot Critic train+eval run.

Latest notebook or script results: [X]
Data build script: autobot_dev/training/patch_critic/build_critic_data.py
Data build command: [paste exact command]
Train config: [paste]
Eval: baseline vs tuned, same subset ([N] rows, seed/IDs = [...])

Compare baseline vs tuned and propose the smallest set of fixes.
Bucket each recommendation under one or more of:
1) Training script/notebook config or parameters
2) Training data logic in build_critic_data.py (labels, merges, truncation, Neo4j query text, class balance)
3) Context/format: GraphRAG/Neo4j ingestion and freshness (prs_clean alignment with graph); Tree-sitter only if we added structural context to critic inputs

Per recommendation give: path(s), exact change, expected metric impact, risks, validation for next run.

Then output a numbered next-loop plan using only these guide section numbers: §4, §5, §6, §7 (omit §4 when Tree-sitter is unused).
```

### Loop-back rule
- Graph/context from Neo4j or ETL mismatch → **§2–§3**, then **§5–§7**.
- Builder-only → **§5–§7**.
- Train-config-only → **§6–§7**.

---

## 9) Stop / continue criteria

**Why we need this:** avoids spinning on noisy small-sample swings.

**Stop** when tuned beats baseline on macro-F1 / key class recalls without regressing rare classes or format validity.

**Continue** when verdict confusion persists, reasoning diverges from G-Eval, or labels look misaligned with PR metadata.

---

## 10) Order every cycle

**Why we need this:** Neo4j and `prs_clean.jsonl` stay in sync with the builder.

1. §2 Refresh ETL data  
2. §3 Re-ingest GraphRAG if extracts or ingestion changed  
3. §4 Optional Tree-sitter rebuild (skip if critic-only + no structural context in builder)  
4. §5 Run `build_critic_data.py`  
5. §6 Train adapter  
6. §7 Evaluate baseline vs tuned  
7. §8 Run the AI review prompt  
8. Repeat until §9 convergence criteria  

---

Sister guide (Patcher / diff generation): [`patcher_training_loop_guide.md`](patcher_training_loop_guide.md).
