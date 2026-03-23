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
- `clean_bot_issues`: RAW -> CLEANED filtering/preprocessing
- `snapshot_issues`: CLEANED -> PRELAB issue snapshots at T+7/T+14

## Security Notes

- Never commit real secrets (`.env`, private keys, service account files, API tokens).
- Use `.env.example` as the only committed env template.
- If a key is ever exposed, rotate it immediately and clean history before public release.

## Repo Status

This is an active research/development monorepo. Some folders (for example `evals/` and parts of `code_pipeline/`) are exploratory and may change structure between runs.
