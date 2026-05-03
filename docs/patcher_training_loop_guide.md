# AutoBot training loop — Patcher

Focused goal: **train the Patcher in an AI-assisted loop until baseline vs tuned metrics converge**.

Assume your repo root is `autobot_dev` on your machine.

**Context wiring for this agent:** datasets are built with **GraphRAG (Neo4j)** and **Tree-sitter** grounding when enabled (default in `build_patcher_data.py` unless `--disable-treesitter`). Neo4j must be ingested before building; Tree-sitter index should match your Airflow clone.

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

**Why we need this:** stale PR/issue exports cause retrieval drift and wrong file paths.

Reference:
- `autobot_dev/etl/training_data/README.md`

Required files:
- `autobot_dev/etl/training_data/issues_clean.jsonl`
- `autobot_dev/etl/training_data/prs_clean.jsonl`
- `autobot_dev/etl/training_data/cleaning_report.json`

---

## 3) Start GraphRAG and ingest Neo4j

**Why we need this:** the patcher builder uses graph-backed context alongside PR data.

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

## 4) Rebuild Tree-sitter index (when Airflow clone or training data alignment changed)

**Why we need this:** Tree-sitter spans and path grounding in `build_patcher_data.py` depend on `treesitter_index.json` matching your local Airflow checkout.

Reference:
- `autobot_dev/tree_sitter/README.md`

```bash
cd autobot_dev/tree_sitter
python3 build_treesitter_index.py \
  --repo "autobot_dev/tree_sitter/airflow" \
  --output "autobot_dev/tree_sitter/treesitter_index.json"
```

---

## 5) Rebuild Patcher training data

**Why we need this:** model behavior is bounded by builder output and validation stats in `dataset_report.json`.

**Builder script:**
- `autobot_dev/training/patch_patcher/build_patcher_data.py`

**Typical outputs (default):**
- `autobot_dev/training/patch_patcher/outputs/patcher_train.jsonl`
- `autobot_dev/training/patch_patcher/outputs/patcher_eval.jsonl`
- `autobot_dev/training/patch_patcher/outputs/patcher_test.jsonl`
- `autobot_dev/training/patch_patcher/outputs/dataset_report.json`

After changing filters, caps, GraphRAG usage, or Tree-sitter usage, rerun the same command and compare `dataset_report.json` across runs.

---

## 6) Train Patcher adapter

**Why we need this:** this is where data + prompt/schema become unified-diff generation behavior.

Work under:
- `autobot_dev/training/patch_patcher/`

Keep run artifacts versioned (`v1`, `run5`, …) and record exact config (LoRA ranks, LR, epochs, max length, batch size).

---

## 7) Evaluate baseline vs tuned

**Why we need this:** retraining decisions must use **deltas**, not tuned-only scores.

Evaluate **baseline (base)** and **tuned (adapter)** on the **same rows**.

### Evaluation protocol (required)
- One main notebook/script that does **training + post-train eval**.
- Same eval subset for base and tuned.
- Fixed **~30-example** quick check after each train run (fixed seed or IDs).
- Before freezing a checkpoint, run a **larger** pass (e.g. full `patcher_eval` split or 80+ rows).

### Patcher-focused metric set
- Valid unified diff rate
- Allowed-file compliance rate
- File-level precision / recall / F1 vs gold touched files
- Optional: hunk-level precision / recall / F1
- Optional: apply/parse pass rate on checkout at recorded base SHA
- G-Eval (sampled) on patch quality / issue alignment
- Latency and token/cost deltas

Do **not** use BLEU/ROUGE as primary decision metrics.

Store artifacts under something like:
- `autobot_dev/evals/patcher/vX/...`

---

## 8) AI-assisted improvement loop

**Why we need this:** iterative, targeted fixes beat random hyperparameter guesses.

After each train+eval round:

1. Share script path **`autobot_dev/training/patch_patcher/build_patcher_data.py`**, exact build command, `dataset_report.json`, train config, baseline vs tuned metrics, and failure examples.
2. Ask for changes across: trainer config, **patcher build script**, or **GraphRAG / Tree-sitter** context wired into inputs.
3. Apply changes → rebuild data → retrain → re-evaluate.
4. Repeat until §9 criteria hold.

### Prompt to AI after each run (copy/paste)

```text
You are reviewing my latest AutoBot Patcher train+eval run.

Latest notebook or script results: [X]
Data build script: autobot_dev/training/patch_patcher/build_patcher_data.py
Data build command: [paste exact command]
Train config: [paste]
Eval: baseline vs tuned, same subset ([N] rows, seed/IDs = [...])

Compare baseline vs tuned and propose the smallest set of fixes.
Bucket each recommendation under one or more of:
1) Training script/notebook config or parameters
2) Training data logic in build_patcher_data.py (filters, splits, labels, caps)
3) Context/format: Neo4j GraphRAG ingestion/query assumptions and/or Tree-sitter index alignment with autobot_dev/tree_sitter/airflow

Per recommendation give: path(s), exact change, expected metric impact, risks, validation for next run.

Then output a numbered next-loop plan using only these guide section numbers: §4, §5, §6, §7.
```

### Loop-back rule
- GraphRAG and/or Tree-sitter context overhaul → **§2–§4**, then **§5–§7**.
- Build script-only changes → **§5–§7**.
- Train config-only → **§6–§7**.

---

## 9) Stop / continue criteria

**Why we need this:** avoid endless tuning with no measurable gain.

**Stop** when tuned consistently beats baseline on core metrics, no major regression on compliance/safety proxies, G-Eval directionally agrees with hard metrics, and latency/cost are acceptable.

**Continue** when gains are unstable, failures cluster by path/context coverage, or format/apply regressions appear.

---

## 10) Order every cycle

**Why we need this:** avoids skipping Neo4j or index steps by mistake.

1. §2 Refresh ETL data  
2. §3 Re-ingest GraphRAG if raw data or graph policy changed  
3. §4 Rebuild Tree-sitter if Airflow clone or grounding policy changed  
4. §5 Run `build_patcher_data.py`  
5. §6 Train adapter  
6. §7 Evaluate baseline vs tuned  
7. §8 Run the AI review prompt  
8. Repeat until §9 convergence criteria  

---

Sister guide (Critic / verdict agent): [`critic_training_loop_guide.md`](critic_training_loop_guide.md).
