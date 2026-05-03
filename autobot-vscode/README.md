# AutoBot VS Code extension

This folder contains the **AutoBot** VS Code UI (Planner → Patcher → Critic). The extension does **not** talk to Ollama or Neo4j directly. It sends HTTP requests to a **local orchestrator** (`local_orchestrator/`), which can call **Ollama**, **Neo4j**, GitHub, etc. depending on your `.env`.

Default orchestrator URL (matches extension settings): **`http://localhost:5000`**.

---

## Prerequisites

- **VS Code** (or Cursor), **Node.js** (for building the extension), **Python 3.10+** (orchestrator).
- **Docker Desktop** (Mac): for Neo4j used by GraphRAG.
- **Repo layout**: assume the repo root folder is named `autobot_dev` on your machine.

---

## End-to-end setup (recommended order)

### 1) Put cleaned ETL files in place

Graph ingestion expects the same three files used elsewhere in the repo.

1. Open [`etl/training_data/README.md`](../etl/training_data/README.md) and download from Google Drive:
   - `issues_clean.jsonl`
   - `prs_clean.jsonl`
   - `cleaning_report.json`
2. Place them under **`autobot_dev/etl/training_data/`** with **exact filenames**.

Without these, GraphRAG ingestion will not run correctly.

---

### 2) Start Neo4j and ingest GraphRAG

From the repo root (`autobot_dev`):

```bash
cd autobot_dev/graphrag
docker compose up -d
```

Wait until Neo4j is healthy (first start can take a minute). Default credentials (see `graphrag/docker-compose.yml`): user **`neo4j`**, password **`autobot_password`**.

Install Python deps at repo root if you have not already:

```bash
cd autobot_dev
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Ingest the graph from the cleaned JSONL files:

```bash
cd autobot_dev
source .venv/bin/activate
python graphrag/ingest_graph_actual.py
```

Optional but recommended for semantic issue similarity (embeddings in Neo4j):

```bash
python graphrag/vectorize_issues.py
```

Sanity check (Neo4j Browser at `http://localhost:7474`): run `MATCH (n) RETURN count(n);` and confirm counts look non-trivial.

More detail: [`graphrag/README.md`](../graphrag/README.md).

---

### 3) Install and run Ollama on macOS

1. Install **Ollama** from [https://ollama.com](https://ollama.com) (Mac app).
2. Pull the coder model used by the local orchestrator default:

   ```bash
   ollama pull qwen2.5-coder:7b
   ```

3. Ensure Ollama is serving the HTTP API on **`127.0.0.1:11434`**.
   - On Mac, **opening the Ollama app** usually starts the server in the background.
   - You can also run `ollama serve` in a terminal if you prefer it explicit.

The orchestrator reads **`OLLAMA_HOST`** (default `http://127.0.0.1:11434`) and **`OLLAMA_MODEL`** (default `qwen2.5-coder:7b`).

---

### 4) Configure and start the local orchestrator

```bash
cd autobot_dev/autobot-vscode/local_orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit **`.env`**:

- Set **`AUTOBOT_MODE=ollama`** (recommended for local dev).
- Confirm **`OLLAMA_HOST`** / **`OLLAMA_MODEL`** match step 3.
- If you want the orchestrator to query Neo4j (similar issues, graph-assisted flows), uncomment and set **`NEO4J_URI`**, **`NEO4J_USER`**, **`NEO4J_PASS`** to match your GraphRAG compose (`bolt://localhost:7687`, `neo4j`, `autobot_password` by default).
- Optional: **`GITHUB_TOKEN`** for live GitHub issue fetch (see `.env.example`).

Start the API (keep this terminal open):

```bash
cd autobot_dev/autobot-vscode/local_orchestrator
source .venv/bin/activate
uvicorn app:app --host 127.0.0.1 --port 5000 --reload
```

You should see the server listening on **port 5000**.

---

### 5) Build and run the VS Code extension

In a **second** terminal:

```bash
cd autobot_dev/autobot-vscode
npm install
npm run compile
```

**Option A — Launch from this repo (Extension Development Host)**

1. Open the folder **`autobot_dev/autobot-vscode`** in VS Code / Cursor.
2. Run **Run → Start Debugging** (or **F5**) and choose **“AutoBot: Run Extension”** if prompted.
3. A new window opens with the extension loaded.

**Option B — Command Palette in your daily editor**

1. Open the Command Palette: **Ctrl+Shift+P** (Windows/Linux) or **Cmd+Shift+P** (macOS).
2. Run **`AutoBot: Open orchestration panel`**.
3. If the AutoBot activity bar icon does not appear, use the command above or open the view from the Activity Bar → **AutoBot**.

In the Extension Development Host window, open the **AutoBot** side bar → **Orchestration** panel.

---

## VS Code settings

In **Settings**, search for **AutoBot**:

| Setting | Default | Meaning |
|--------|---------|--------|
| `autobot.serverUrl` | `http://localhost:5000` | Base URL of the orchestrator (no trailing slash). |
| `autobot.orchestratePath` | `/api/orchestrate` | POST path for orchestration. |
| `autobot.requestTimeoutMs` | `300000` | Timeout for long model runs (5 minutes). |

If your orchestrator uses auth, run **`AutoBot: Set API bearer token`** once (stored in Secret Storage).

---

## Troubleshooting

| Symptom | What to check |
|--------|----------------|
| Extension cannot reach server | Orchestrator running? `curl -s http://127.0.0.1:5000/docs` (FastAPI docs) or health depending on app. |
| Model errors / empty responses | Ollama running? `ollama list`. Model pulled? Same name as `OLLAMA_MODEL`. |
| Graph / similar-issue features empty | Neo4j up? Ingest + optional vectorize completed? `.env` Neo4j vars set in `local_orchestrator`? |
| Build errors for extension | Run `npm install` and `npm run compile` in `autobot-vscode`. |

---

## Related docs

- GraphRAG: [`graphrag/README.md`](../graphrag/README.md)
- ETL training data: [`etl/training_data/README.md`](../etl/training_data/README.md)
- Environment template: [`local_orchestrator/.env.example`](local_orchestrator/.env.example)
