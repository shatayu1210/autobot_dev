# autobot_dev

Monorepo for AutoBot data and tooling. Today this repo focuses on **ETL** (GitHub → Snowflake via Airflow). You can add other top-level folders later (e.g. `training/`, `labelling/`) without changing the layout below.

---

## Repository layout

| Path | Purpose |
|------|---------|
| **`etl/`** | Apache Airflow + Docker setup, DAGs, and Python deps for extract / clean / snapshot pipelines |
| *(future)* | e.g. `training/`, `notebooks/`, `labelling/` — add as needed |

---

## ETL (`etl/`)

Airflow runs in Docker (see `etl/docker-compose.yml`). DAGs live in **`etl/dags/`** and are mounted into the container, so they show up in the Airflow UI after the scheduler parses them.

### Prerequisites

- Docker + Docker Compose
- Snowflake: an Airflow connection **`snowflake_default`** (Admin → Connections) with your account, user, password, warehouse, database, etc.
- GitHub: either a **PAT** (`GITHUB_TOKEN`) or **GitHub App** credentials in `.env` — see `etl/.env.example`

### Quick start

1. `cd etl`
2. Copy env template: `cp .env.example .env` and fill in secrets (never commit `.env`).
3. Configure **`snowflake_default`** in the Airflow UI (or equivalent env-based connection if you use that pattern).
4. Build and start: `docker compose up -d` (from `etl/`; see `docker-compose.yml` for ports, e.g. web UI).
5. In the UI, unpause the DAGs you need and trigger manually where schedules are `None`.

### DAGs (what each one does)

All of these run **on demand** in the UI unless you add a schedule later.

#### `full_extract` — production GitHub → Snowflake

- Pulls **closed issues** and **linked PRs** from the **`apache/airflow`** GitHub repo.
- Writes into Snowflake **RAW** tables (issues + PRs).
- Uses checkpoints so a long run can **resume** if it stops partway.
- Good for: filling or refreshing your RAW layer from GitHub.

#### `test_extract` — tiny sanity check (no Snowflake required for the sample path)

- Grabs only **10 issues** (and their linked PRs) as a **smoke test**.
- Writes **JSON files** to `test_output/` on your machine (via the Docker volume mount).
- Good for: verifying GitHub auth and extract logic before a big `full_extract`.

#### `clean_bot_issues` — RAW → CLEANED in Snowflake

- Reads a **source** issues table (default: RAW) and writes a **cleaned** copy (default: CLEANED).
- Drops rows you don’t want for modelling (e.g. titles that look like **chore / bump / dependabot** — patterns are configurable in params).
- Good for: one table you trust for downstream steps (snapshots, labelling).

#### `snapshot_issues` — CLEANED → PRELAB (T+7 and T+14)

- For each issue, builds **two** “as-of” views: **7 days** and **14 days** after **issue creation** (`created_at`).
- **Re-computes** which PRs were linked **using only timeline (and body/comments) up to that snapshot date** — it does **not** rely on the old `linked_pr_numbers` column for that.
- Strips **closure** info in the snapshot (e.g. treats as still **open** in the snapshot JSON) so the row matches “what you’d have known at day 7 / 14.”
- Writes **`SCORER_ISSUES_T7`** and **`SCORER_ISSUES_T14`** in **PRELAB** (defaults in DAG params).
- Tagged **`snapshot`** in the Airflow UI so it’s easy to filter.
- Good for: handing off consistent issue snapshots to scoring / labelling scripts.

Default database/schema/table names are in each DAG’s **Trigger** params and docstrings (e.g. `AIRFLOW_ML.CLEANED.GITHUB_ISSUES`, `AIRFLOW_ML.PRELAB.SCORER_ISSUES_T7` / `_T14`).

### Important files

| File | Role |
|------|------|
| `etl/Dockerfile` | Airflow image + `requirements.txt` install |
| `etl/docker-compose.yml` | Postgres metadata DB, webserver, scheduler, env wiring |
| `etl/requirements.txt` | Extra Python packages (Snowflake provider, JWT, etc.) |
| `etl/.env.example` | Safe template for local secrets and compose vars |
| `etl/.gitignore` | Ignores `.env`, keys (`*.pem`), logs, `temp_script.py`, local outputs |

### Secrets & local-only files

Do **not** commit:

- `.env`, private keys (`.pem`), service account JSONs
- `temp_script.py` (local GitHub App token experiments — ignored by `etl/.gitignore`)

Share these with collaborators out-of-band (1Password, etc.).

### Snapshot implementation note

The **Airflow** entry point for T+7 / T+14 snapshots is the DAG `snapshot_issues` in `etl/dags/snapshot_issues.py`. If you keep a separate CLI copy elsewhere, treat the DAG file as the source of truth for scheduled runs.

---

## Contributing / next steps

- Add folders like `training/` or `labelling/` at the repo root when you’re ready; keep README sections parallel to this **ETL** block for discoverability.
