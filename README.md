# AutoBot Dev Monorepo

This repository contains the end-to-end AutoBot pipeline for:
- collecting GitHub issue/PR data,
- building cleaned and time-sliced datasets,
- generating model labels/training artifacts,
- running model evaluation notebooks, and
- serving predictions in Slack.

## What This Repo Contains

| Path | Purpose | Typical use |
|------|---------|-------------|
| `etl/` | Airflow + Docker ETL from GitHub to Snowflake (extract, clean, snapshot) | Data ingestion and snapshot generation |
| `labelling/` | Label generation pipeline (`scorer`, `reasoner`, `planner`, `patcher`, `critic`) | Produce supervised training labels from snapshots |
| `training/` | Model training notebooks/scripts (`bottleneck_detector`, `patch_planner`) | Train and tune task-specific models |
| `evals/` | Evaluation notebooks for training runs and planner/critic outputs | Compare quality across runs |
| `slackbot/` | Slack app + tooling to query data and return bottleneck analysis | User-facing bot integration |
| `cli/` | Lightweight utility scripts and one-off data prep jobs | Local scripts for exports/indexing |
| `code_pipeline/` | Separate starter/lab project for multi-agent evaluation experiments | Reference/experimental pipeline |

## Data/Model Flow

1. `etl/` extracts and cleans GitHub data, then creates T+7/T+14 snapshots.
2. `labelling/` converts snapshots into model-specific labels.
3. `training/` uses labeled data to train bottleneck/planner components.
4. `evals/` tracks model quality and iteration results.
5. `slackbot/` consumes outputs for interactive predictions.

## Quickstart By Goal (AI IDE Friendly)

Use this section when onboarding a new teammate or prompting an AI IDE assistant to run specific pipelines.

### Goal A: Rebuild ETL training corpus (`issues_clean.jsonl`, `prs_clean.jsonl`)

1. **Set environment**
   - `cd etl`
   - copy template env if needed: `cp .env.example .env`
   - populate GitHub + Snowflake credentials
2. **Run extraction**
   - run Airflow DAG `full_extract` (see `etl/README.md`)
   - choose `sink_mode=local` (writes JSONL to `etl/extracted_data/`) or `sink_mode=snowflake`
3. **Consolidate and clean**
   - `python3 etl/clean_and_consolidate.py`
   - outputs written to `etl/training_data/`
4. **Validate outputs**
   - check these files exist:
     - `etl/training_data/issues_clean.jsonl`
     - `etl/training_data/prs_clean.jsonl`
     - `etl/training_data/cleaning_report.json`
5. **If extraction was interrupted**
   - fix checkpoint drift: `python3 etl/recover_missing_issues.py --apply`
   - or rebuild checkpoints: `python3 etl/rebuild_checkpoints.py`

Primary reference: `etl/README.md`

### Goal B: Load cleaned data into Snowflake

Use local loader when Airflow is unstable or for repeatable manual loads.

```bash
cd etl
python3 load_to_snowflake.py \
  --account <account> \
  --user <user> \
  --password '<password>' \
  --source cleaned \
  --mode both
```

Reference: `etl/load_to_snowflake.py` and `etl/README.md`

### Goal C: Stand up GraphRAG and ingest Neo4j graph

1. Start Neo4j:
   - `cd graphrag && docker compose up -d`
2. Ingest graph:
   - `python ingest_graph_actual.py`
3. (Optional) add embeddings:
   - `python vectorize_issues.py`
4. Sanity check in Neo4j Browser (`http://localhost:7474`):
   - `MATCH (n) RETURN count(n);`
   - `MATCH ()-[r]->() RETURN count(r);`

Reference: `graphrag/README.md`

### Goal D: Rebuild tree-sitter index for planner retrieval

Rebuild when Airflow repo snapshot changes or when training data is refreshed and you want path/context alignment.

```bash
cd tree_sitter
python3 build_treesitter_index.py \
  --repo "/absolute/path/to/autobot_dev/tree_sitter/airflow" \
  --output "/absolute/path/to/autobot_dev/tree_sitter/treesitter_index.json"
```

Reference: `tree_sitter/README.md`

### Goal E: Generate patcher/planner training datasets

1. Ensure prerequisites:
   - cleaned ETL data in `etl/training_data/`
   - Neo4j running (if GraphRAG enabled)
   - tree-sitter index built for planner path-grounding
2. Run builders under `training/patch_*` (for example `patch_patcher/build_patcher_data.py`, `patch_planner/build_planner_data.py`)
3. Keep generated large artifacts in ignored local dirs; only commit scripts/docs/notebooks intended for collaboration.

### AI IDE prompt template (copy/paste)

Use this as a starting prompt for any assistant in Cursor/VS Code:

```text
You are helping with autobot_dev. First read README.md plus:
- etl/README.md
- graphrag/README.md
- tree_sitter/README.md

Goal: <replace with one goal above>.
Constraints:
- Do not commit secrets or large generated data.
- Prefer reproducible commands and explicit validation checks.
- After running steps, summarize outputs and next actions.
```

## Quick Start (Most Common Path)

### 1) Run ETL

1. `cd etl`
2. Copy template env: `cp .env.example .env`
3. Fill credentials and set Snowflake connection (`snowflake_default`)
4. Start services: `docker compose up -d`
5. Trigger DAGs in Airflow UI (extract -> clean -> snapshot)

### 2) Build labels

1. `cd labelling`
2. Install deps: `pip install -r requirements.txt`
3. Run examples:
   - `python label_pipeline.py --model scorer`
   - `python label_pipeline.py --model all --stats`

### 3) Train and evaluate

- Use notebooks under `training/` for model training
- Use notebooks under `evals/` for run comparison and quality checks

## ETL DAGs at a Glance

- `full_extract`: full GitHub issues/PR ingestion to Snowflake RAW tables
- `test_extract`: small smoke test extraction (useful before full runs)
- `clean_bot_issues`: legacy cleaner DAG (kept in history; current cleaning path is script-driven in `etl/clean_and_consolidate.py`)
- `snapshot_issues`: CLEANED -> PRELAB issue snapshots at T+7/T+14 (if enabled in your branch)

## Security Notes

- Never commit real secrets (`.env`, private keys, service account files, API tokens).
- Use `.env.example` as the only committed env template.
- If a key is ever exposed, rotate it immediately and clean history before public release.

## Repo Status

This is an active research/development monorepo. Some folders (for example `evals/` and parts of `code_pipeline/`) are exploratory and may change structure between runs.
