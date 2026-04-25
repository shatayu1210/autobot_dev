# ETL Pipeline Guide

This folder contains the end-to-end data pipeline for building AutoBot training corpora from GitHub + Snowflake.

## What is what

- `dags/full_extract.py`: active production DAG used to pull issues/PRs and write to local JSONL or Snowflake.
- `dags/ref_full_extract.txt`: archived reference snapshot (kept for historical comparison, not executed by Airflow).
- `clean_and_consolidate.py`: dedup + bot removal + issue/PR link enrichment into clean training JSONL.
- `load_to_snowflake.py`: standalone local script to load JSONL into Snowflake.
- `recover_missing_issues.py`: repair checkpoint drift when checkpoint marks items done but JSONL is missing.
- `rebuild_checkpoints.py`: rebuild checkpoint files from exported Snowflake CSVs.
- `training_data/`: cleaned outputs consumed by training scripts.
- `extracted_data/`: raw batched extraction outputs (local sink mode).
- `checkpoints/`: resume state for extraction jobs.

## Prerequisites

- Python env with project dependencies (`pip install -r requirements.txt`).
- Airflow + Docker setup for DAG execution.
- GitHub credentials configured (App creds preferred; PAT fallback supported).
- Snowflake connection configured as `snowflake_default` in Airflow for DAG Snowflake mode.

## Standard workflow

1. **Run extraction DAG** (`full_extract`) with one of:
   - `sink_mode=local`: writes JSONL to `etl/extracted_data/`
   - `sink_mode=snowflake`: bulk loads directly to Snowflake RAW tables
2. **Consolidate and clean**:
   - `python3 etl/clean_and_consolidate.py`
3. **(Optional) load cleaned outputs to Snowflake from local machine**:
   - `python3 etl/load_to_snowflake.py --account <acct> --user <user> --password '<pwd>' --source cleaned --mode both`
4. **Share/consume cleaned outputs** from `etl/training_data/`.

## Airflow extract quick run

- DAG: `full_extract`
- Main params:
  - `sink_mode`: `local` or `snowflake`
  - `issues_pull_size`, `prs_pull_size`
  - `snowflake_database`, `snowflake_schema`, table names

Checkpoint behavior:
- Checkpoints are persisted under `etl/checkpoints/` (via mounted volume in your environment).
- Re-running resumes from checkpoint rather than restarting from zero.

## Checkpoint recovery playbook

Use these in order depending on failure mode:

1. **Checkpoint says done but JSONL missing** (network/drop during extraction):
   - `python3 etl/recover_missing_issues.py` (dry run)
   - `python3 etl/recover_missing_issues.py --apply` (apply fix)
   - Retrigger `extract_issues` task.

2. **Checkpoint files lost, but Snowflake has data**:
   - Export issues/PR CSV from Snowflake.
   - Update paths in `etl/rebuild_checkpoints.py` if needed.
   - Run `python3 etl/rebuild_checkpoints.py`.

## Snowflake load options

### A) From DAG (`sink_mode=snowflake`)
- Best when Airflow env is healthy and long-running.

### B) Local fallback script
- Good for travel/unstable Airflow sessions.
- Cleaned source:
  - `python3 etl/load_to_snowflake.py --account <acct> --user <user> --password '<pwd>' --source cleaned --mode both`
- Raw source:
  - `python3 etl/load_to_snowflake.py --account <acct> --user <user> --password '<pwd>' --source raw --mode both`

## Data contract for downstream training

Expected cleaned files in `etl/training_data/`:
- `issues_clean.jsonl`
- `prs_clean.jsonl`
- `cleaning_report.json`

See `etl/training_data/README.md` for download/refresh instructions.
