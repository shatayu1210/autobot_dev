# RLHF/DPO Pipeline in `slackbot`

This document explains how the RLHF-style preference loop works in this folder, what is implemented today, and how to run it.

## How to start

Use this as your quick start every time.

### First-time setup

1. Create/activate virtual environment and install deps:

```bash
cd /Users/shubhamnaik/Documents/autobot_dev/slackbot
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

2. Start Postgres (Docker option):

```bash
docker start autobot-pg
docker logs -f autobot-pg
```

Wait for `database system is ready to accept connections`, then `Ctrl+C`.

3. Load environment variables and initialize schema:

```bash
cd /Users/shubhamnaik/Documents/autobot_dev/slackbot
set -a; source .env; set +a
./.venv/bin/python -c "import dpo_feedback; dpo_feedback.ensure_schema(); print('schema ok')"
```

### Start Slack bot server (for live capture)

```bash
cd /Users/shubhamnaik/Documents/autobot_dev/slackbot
set -a; source .env; set +a
./.venv/bin/python app.py
```

Server endpoints:
- `/slack/events`
- `/slack/commands`
- `/slack/interactive`
- `/health`

If testing locally with Slack, expose port 3000 via ngrok (or similar) and set Slack Request URLs to your public HTTPS URL.

### Start weekly DPO batch runner

Safe smoke run (no train/deploy):

```bash
cd /Users/shubhamnaik/Documents/autobot_dev/slackbot
set -a; source .env; set +a
./.venv/bin/python scripts/run_weekly_dpo.py --skip-teacher --skip-train --skip-deploy --json
```

Teacher+export run:

```bash
cd /Users/shubhamnaik/Documents/autobot_dev/slackbot
set -a; source .env; set +a
./.venv/bin/python scripts/run_weekly_dpo.py --skip-train --skip-deploy --json
```

## What is implemented

The current pipeline is **DPO-oriented** (preference learning), split into:

1. **Feedback capture (online, Slack events)**
2. **Weekly batch processing (offline runner)**

Implemented files:

- `app.py`  
  Slack Bolt handlers (`app_mention`, `message`, `reaction_added`), including thumbs-down ingestion flow.
- `dpo_feedback.py`  
  Postgres schema init + feedback insert + prompt/rejected extraction helpers.
- `schema/dpo_tables.sql`  
  DB tables for feedback, teacher labels, and training run metadata.
- `dpo_weekly.py`  
  Weekly batch logic (load unlabeled rows, teacher labels, export JSONL, optional train webhook).
- `scripts/run_weekly_dpo.py`  
  CLI entrypoint for weekly batch execution.

## Data model

Tables (from `schema/dpo_tables.sql`):

- `feedback_events`
  - Source rows from Slack thumbs-down events.
  - Core fields: `prompt_text`, `rejected_text`, `metadata`, `reaction`.
- `teacher_labels`
  - GPT teacher-generated preferred text (`chosen_text`) per feedback row.
  - Stores QC status (`accepted`/`rejected`).
- `training_runs`
  - Metadata per weekly run (`run_id`, window, artifact path, status).
- `training_run_rows`
  - Mapping rows for auditability.

## End-to-end flow

## 1) Slack feedback capture

Trigger:
- User adds 👎 (`thumbsdown`/`-1`) on a bot-authored message.

Path:
- Slack -> `POST /slack/events` in `app.py`
- `reaction_added` lazy handler:
  - validates reaction type
  - fetches message/thread context via Slack APIs
  - derives:
    - `prompt_text` (preceding human message)
    - `rejected_text` (reacted bot message)
  - writes row into `feedback_events` using `dpo_feedback.insert_feedback_event()`

Key env flags:
- `DATABASE_URL`
- `DPO_FEEDBACK_DISABLED` (optional off switch)
- `DPO_FEEDBACK_ENSURE_SCHEMA` (optional schema auto-init)

## 2) Weekly DPO batch runner

Command:

```bash
./.venv/bin/python scripts/run_weekly_dpo.py --skip-train --skip-deploy --json
```

Stages:

1. `ensure_schema()`
2. Load unlabeled feedback rows in weekly window (`DPO_WEEK_START`, `DPO_WEEK_END`, default last 7 days)
3. Teacher labeling (OpenAI) unless `--skip-teacher`
4. Export DPO JSONL to `DPO_JSONL_DIR` (default `/tmp/dpo_jsonl`)
5. Record `training_runs`
6. Optional train webhook (`DPO_TRAIN_WEBHOOK_URL`) unless `--skip-train`
7. Deploy is currently skipped/not implemented in this runner unless extended later

## Teacher labeling

Teacher uses OpenAI Chat Completions with a strict system prompt and outputs:

- `CHOSEN_REASONING: ...`

Minimal QC:
- reject too-short outputs
- reject exact copies of rejected text

Stored in `teacher_labels` with `qc_status`.

## Export format

JSONL lines are written as:

```json
{"prompt":"...","chosen":"...","rejected":"..."}
```

This is compatible with typical DPO training setups.

## Environment variables

Required for core flow:

- `DATABASE_URL`

Required for teacher stage:

- `OPENAI_API_KEY`
- optional: `DPO_TEACHER_MODEL` (default `gpt-4o`)
- optional: `OPENAI_BASE_URL`

Optional for batch behavior:

- `DPO_WEEK_START`
- `DPO_WEEK_END`
- `DPO_JSONL_DIR`
- `DPO_TRAIN_WEBHOOK_URL`
- `DPO_HUB_REPO_ID`
- `DPO_NEW_REVISION`

Slack runtime:

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `SLACK_BOT_USER_ID` (optional but recommended)

## Slack app requirements

Event subscriptions:
- `app_mention`
- `message.im`
- `reaction_added`

Bot scopes:
- `app_mentions:read`
- `chat:write`
- `commands`
- `reactions:read`
- `channels:history`
- `groups:history`
- `im:history`
- `mpim:history`

Remember to **Reinstall App to Workspace** after scope updates.

## Validation checklist

1. Schema init works:

```bash
./.venv/bin/python -c "import dpo_feedback; dpo_feedback.ensure_schema(); print('schema ok')"
```

2. Manual insert works:

```bash
./.venv/bin/python -c "import dpo_feedback, time; print(dpo_feedback.insert_feedback_event(slack_team_id='T', channel_id='C', message_ts=str(time.time()), reactor_user_id='U', prompt_text='p', rejected_text='r', metadata={'source':'test'}))"
```

3. Live Slack 👎 capture logs:
- `DPO: stored thumbs-down feedback ...`

4. Weekly run succeeds:

```bash
./.venv/bin/python scripts/run_weekly_dpo.py --skip-train --skip-deploy --json
```

Expected:
- non-zero `loaded_rows` (if new feedback exists)
- `labeled_rows` increases when teacher is enabled
- JSONL artifact path is produced
- `errors` is empty

## Current status

Working now in this folder:
- Slack thumbs-down capture -> Postgres
- Weekly load/label/export runner

Not yet fully implemented here:
- automated HF endpoint deploy/rollback stage in `slackbot` runner

