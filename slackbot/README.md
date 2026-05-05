# slackbot

Slack integration for AutoBot (bot server, env, and related tooling in this directory).

## DPO feedback (thumbs-down)

Bolt handles `reaction_added` for 👎 (`thumbsdown` / `:thumbsdown:` / `-1` shortcode depending on workspace). Rows are inserted into Postgres `feedback_events` when **`DATABASE_URL`** is set.

- **`SLACK_BOT_USER_ID`** — optional if your app replies as a bot; otherwise resolved via **`auth.test`** once.
- **`DPO_FEEDBACK_DISABLED`** — `1` / `true` to turn capture off without removing the handler.
- **`DPO_FEEDBACK_ENSURE_SCHEMA`** — `1` / `true` to run [`schema/dpo_tables.sql`](schema/dpo_tables.sql) on incoming events (use migrations in production when possible).

In the Slack app, subscribe to **`reaction_added`** and grant **`reactions:read`** plus channel/DM **`history`** scopes needed to load thread replies.

## Weekly DPO runner (in `slackbot`)

Run a local smoke test from this folder:

```bash
set -a; source .env; set +a
./.venv/bin/python scripts/run_weekly_dpo.py --skip-teacher --skip-train --skip-deploy --json
```

Run with teacher labeling (OpenAI key required):

```bash
set -a; source .env; set +a
./.venv/bin/python scripts/run_weekly_dpo.py --skip-train --skip-deploy --json
```
