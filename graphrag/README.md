# AutoBot GraphRAG

This folder contains the local Neo4j GraphRAG setup used to enrich planner/patcher/critic datasets with historical repository signals.

## What this does

1. Loads ETL outputs (`issues_clean.jsonl`, `prs_clean.jsonl`) into Neo4j.
2. Builds graph entities (`Issue`, `PR`, `File`, `Review`, etc.) and relationships.
3. Optionally adds vector embeddings for semantic issue retrieval.
4. Serves graph context to data builders and orchestrators.

## Files

- `docker-compose.yml`: local Neo4j service (with persistent volumes).
- `ingest_graph_actual.py`: graph ingestion script from cleaned ETL JSONL.
- `vectorize_issues.py`: optional local embedding generation for issues.

## Local deployment

### 1) Start Neo4j

```bash
cd autobot_dev/graphrag
docker compose up -d
```

The compose file persists DB data under:
- `graphrag/data` (database files)
- `graphrag/logs`
- `graphrag/plugins`
- `graphrag/import`

So normal restarts do not require re-ingestion.

### 2) Install deps

```bash
cd autobot_dev
source .venv/bin/activate
pip install -r requirements.txt
```

### 3) Ingest graph

```bash
cd graphrag
python ingest_graph_actual.py
```

### 4) (Optional) vectorize issues

```bash
pip install sentence-transformers
python vectorize_issues.py
```

## Operational checks

Neo4j Browser: `http://localhost:7474`  
Default bolt URI: `bolt://localhost:7687`

Quick sanity queries:

```cypher
SHOW DATABASES;
MATCH (n) RETURN count(n);
MATCH ()-[r]->() RETURN count(r);
```

## Re-ingestion rules

- **Need re-ingest**: if `graphrag/data` is deleted or container volumes are removed (`docker compose down -v`).
- **No re-ingest needed**: normal Docker stop/start or machine reboot with persisted `graphrag/data`.

## How this is used downstream

- **Planner**: file candidates from historically similar issue→PR→file traversals.
- **Patcher**: file-specific historical PR/commit patterns.
- **Critic**: review friction and CI-failure context for modified files.

GraphRAG context is typically baked into training JSONL; model training itself does not require Neo4j online once datasets are generated.
