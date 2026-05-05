# AutoBot Dev — Full System Setup Guide

This guide walks through setting up every component needed to run a complete
AutoBot demo: ETL data, GraphRAG, tree-sitter index, the VS Code plugin, the
local orchestrator, and the MCP server.

---

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.10+ | `brew install python` |
| Node.js | 18+ | `brew install node` |
| Git | any | pre-installed on macOS |
| Docker Desktop | latest | [docker.com](https://www.docker.com/products/docker-desktop/) |
| Ollama (macOS app) | latest | [ollama.com](https://ollama.com) |
| VS Code | 1.85+ | [code.visualstudio.com](https://code.visualstudio.com) |

---

## 1. Clone and root environment

```bash
git clone <your-repo-url> autobot_dev
cd autobot_dev
cp .env.example .env          # fill in credentials (see comments inside)
```

---

## 2. Ollama — local LLM for the orchestrator

The local orchestrator uses Ollama as its default LLM backend during
development (free, no tokens consumed).

### 2a. Install the Ollama macOS app

1. Download the macOS app from [ollama.com](https://ollama.com).
2. Open the `.dmg`, drag Ollama to Applications, and launch it.
3. You'll see the Ollama icon in your macOS menu bar — this means the server
   is running on `http://127.0.0.1:11434`.
4. In Ollama's **Settings** tab you can adjust context size (recommended: set
   to at least `8192` for code tasks). The "Expose config" toggle is not
   needed for our use.

### 2b. Pull the orchestrator model

```bash
# Pull once (~4 GB). Ollama must be running (menu bar icon visible).
ollama pull qwen2.5-coder:7b
```

Verify it works:

```bash
ollama run qwen2.5-coder:7b "Say hello in one word."
# Should respond: Hello
```

> **Tip:** When the Ollama app is open, `ollama serve` is already running
> automatically. You do **not** need to run `ollama serve` separately.

---

## 3. Local Orchestrator (Flask backend)

The orchestrator is the Python Flask server that the VS Code extension calls.
It handles issue fetching, planner calls, patcher/critic loops, and GitHub
API integration.

```bash
cd autobot_vscode/local_orchestrator

# Copy environment template
cp .env.example .env
# Default is AUTOBOT_MODE=ollama — no changes needed for local dev.
# To use Gemini Flash instead, set AUTOBOT_MODE=google_ai and GOOGLE_API_KEY.

# Install dependencies
pip3 install flask python-dotenv requests langchain-google-genai

# Start the server (keep this terminal open)
python3 app.py
# → Listening on http://127.0.0.1:5000
```

### Orchestrator LLM modes

Edit `local_orchestrator/.env` to switch backends:

| Mode | Setup | Notes |
|---|---|---|
| `ollama` (default) | Ollama app running + model pulled | Free, local, safe for dev |
| `google_ai` | Set `GOOGLE_API_KEY` from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free tier available (Gemini 2.0 Flash Lite: 1500 req/day) |
| `vertex` | `gcloud auth application-default login` + GCP project | For production / demo |
| `stub` | Nothing needed | Hardcoded fake responses for UI testing |

### GitHub token (live issue fetching)

Without a token, the orchestrator returns stub data for issue queries.
To get live issue data:

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Generate a classic token with `public_repo` scope
3. Add to `local_orchestrator/.env`:
   ```
   GITHUB_TOKEN=github_pat_your_token_here
   ```

---

## 4. MCP Server (GitHub adhoc queries)

The MCP server powers adhoc queries from the AutoBot VS Code chat such as:
- "What is the status of issue #66158?"
- "Who is assigned to issue #45000?"
- "Show the top 5 open issues by risk score"
- "Show linked PRs for issue #50000"
- "What were the recent comments on issue #48000?"
- "What files did PR #60000 touch?"
- "Show CI status for PR #58000"

The MCP server wraps the same GitHub REST API endpoints used in the full ETL
(`/issues`, `/issues/{n}`, `/issues/{n}/comments`, `/issues/{n}/timeline`,
`/pulls/{n}`, `/pulls/{n}/files`, `/pulls/{n}/reviews`,
`/commits/{sha}/check-runs`) and exposes them as MCP tools callable from the
VS Code extension.

### 4a. Install MCP server dependencies

```bash
cd slackbot
pip3 install fastmcp requests google-cloud-aiplatform
```

### 4b. Configure

Copy `.env` variables from `etl/.env` (the same GitHub token is used):

```bash
# In slackbot/.env (create if missing):
GITHUB_TOKEN=github_pat_your_token_here
GITHUB_OWNER=apache
GITHUB_REPO=airflow

# GCP Vertex AI (optional — for risk scoring; skip for pure GitHub queries)
GCP_PROJECT_ID=your-project-id
GCP_LOCATION=us-west1
SCORER_ENDPOINT_ID=your-endpoint-id
REASONER_ENDPOINT_ID=your-endpoint-id
```

### 4c. Start the MCP server

```bash
cd slackbot
# SSE mode (recommended for VS Code integration):
MCP_TRANSPORT=sse python3 mcp_server.py
# → MCP Server listening on 0.0.0.0:8080

# Or stdio mode (for Claude Desktop / direct MCP client):
python3 mcp_server.py
```

### 4d. MCP tools exposed

| Tool | What it does |
|---|---|
| `get_issue_status(issue_number)` | Fetch title, state, assignee, labels, open date, body |
| `get_issue_comments(issue_number)` | Fetch all comments on an issue |
| `get_linked_prs(issue_number)` | Find PRs linked to an issue via timeline + body parsing |
| `get_pr_details(pr_number)` | Fetch PR metadata, files changed, reviews, CI status |
| `get_top_risk_items(top_n)` | Score open issues and return top N risky items |
| `search_issues(query, state)` | Search issues by keyword |

> **Note:** The current `mcp_server.py` implements `get_top_risk_items`. The
> remaining tools listed above are planned additions to match the GitHub
> endpoints already used in `slackbot/full_extract.py` and
> `code_pipeline/agents/planner/agent.py`.

---

## 5. VS Code Extension

The AutoBot VS Code extension provides the conversational chat sidebar where
you interact with the Planner → Patcher → Critic pipeline.

### 5a. Compile the extension

```bash
cd autobot_vscode
npm install
npm run compile
```

### 5b. Launch in Extension Development Host

1. Open VS Code.
2. `File → Open Folder` → select `autobot_vscode/`.
3. Press `F5` (or `Run → Start Debugging`).
4. A new **Extension Development Host** VS Code window opens.

### 5c. Open the AutoBot chat panel

1. In the **Extension Development Host** window, click the 🤖 AutoBot icon
   in the left Activity Bar.
2. The **AutoBot** chat sidebar opens.

### 5d. Configure the extension

In the **Extension Development Host** VS Code window:

1. Press `Cmd+Shift+P` → `AutoBot: Set API bearer token` (only needed if
   connecting to a remote/cloud orchestrator with auth).
2. Open `Settings` (`Cmd+,`) → search `autobot`:
   - `autobot.serverUrl`: `http://localhost:5000` (default, correct for local)
   - `autobot.orchestratePath`: `/api/orchestrate` (default)

### 5e. Point the extension to the Airflow repo

In the chat sidebar, set the **Repo** field to your local Airflow checkout:

```
/Users/<you>/Desktop/FALL24/SPRING26/298B/WB2/autobot_dev/tree_sitter/airflow
```

Or click the `…` button to browse for the folder.

### 5f. Try it out

Type in the chat input at the bottom:

```
fix issue #45123
```

or

```
check issue #66158
```

The agent will fetch the issue, plan a patch (using the configured LLM), and
present the plan for your approval.

---

## 6. GraphRAG / Neo4j (for production retrieval)

The planner uses Neo4j for historically-grounded candidate file retrieval.
This step is optional for stub/LLM-only mode but required for real demo runs.

```bash
cd graphrag
docker compose up -d        # starts Neo4j on localhost:7474 / 7687
python3 ingest_graph_actual.py
# Optional: add embedding similarity edges
python3 vectorize_issues.py
```

Verify in Neo4j Browser at `http://localhost:7474`:

```cypher
MATCH (n) RETURN count(n);
MATCH ()-[r]->() RETURN count(r);
```

Reference: `graphrag/README.md`

---

## 7. Tree-sitter index (for code span retrieval)

The planner uses a pre-built tree-sitter index to extract function/class
spans from the Airflow repo. Rebuild this whenever the Airflow checkout is
updated.

```bash
cd tree_sitter
python3 build_treesitter_index.py \
  --repo "$(pwd)/airflow" \
  --output "$(pwd)/treesitter_index.json"
```

The index file (`treesitter_index.json`, ~2.5 MB) is already committed and
points to `tree_sitter/airflow/`. Only rebuild if you update the checkout.

---

## 8. ETL pipeline (for training data)

Only needed if you are rebuilding training datasets or refreshing Snowflake.

```bash
cd etl
cp .env.example .env           # fill GitHub + Snowflake credentials
docker compose up -d           # starts Airflow + Postgres
# Open http://localhost:8080 (admin / admin)
# Trigger DAG: full_extract
```

For the full ETL flow including cleaning and loading:

```bash
python3 clean_and_consolidate.py
python3 load_to_snowflake.py \
  --account <account> --user <user> --password '<pw>' \
  --source cleaned --mode both
```

Reference: `etl/README.md`

---

## 9. Complete demo run checklist

Use this checklist before a full demo:

- [ ] Ollama app is open (menu bar icon visible)
- [ ] `qwen2.5-coder:7b` model is pulled (`ollama list`)
- [ ] Orchestrator running: `python3 autobot_vscode/local_orchestrator/app.py`
  - [ ] `GITHUB_TOKEN` set in orchestrator `.env` for live issue data
  - [ ] `AUTOBOT_MODE=ollama` (or `google_ai` / `vertex` for demo quality)
- [ ] MCP server running: `cd slackbot && MCP_TRANSPORT=sse python3 mcp_server.py`
- [ ] VS Code extension compiled (`npm run compile` in `autobot_vscode/`)
- [ ] Extension Development Host open (F5 from `autobot_vscode/` in VS Code)
- [ ] Repo path set to `tree_sitter/airflow/` in AutoBot chat sidebar
- [ ] Neo4j running (`docker ps` shows neo4j container) [optional but recommended]
- [ ] Tree-sitter index exists at `tree_sitter/treesitter_index.json`

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Chat shows "Stub issue" | Add `GITHUB_TOKEN` to `local_orchestrator/.env` and restart orchestrator |
| Orchestrator error "ollama not reachable" | Open the Ollama macOS app (look for menu bar icon) |
| `tsc: command not found` | Run `npm install` first inside `autobot_vscode/` |
| Extension not visible in sidebar | Make sure Extension Development Host window is focused, not the main VS Code window |
| Flask 502/connection refused | Make sure `python3 app.py` is running in `local_orchestrator/` |
| Plan always returns stub | Set `AUTOBOT_MODE=ollama` (not `stub`) in orchestrator `.env` |
