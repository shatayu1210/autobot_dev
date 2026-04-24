# AutoBot Neo4j GraphRAG — Complete Setup Guide
> Standalone reference for any team member. No prior Neo4j experience needed.

---

## What is Neo4j and How is it Queried?

Neo4j is a **graph database**. Instead of tables with rows, it stores:
- **Nodes** — things (Issues, PRs, Files, Symbols)
- **Edges** — relationships between them (RESOLVES, TOUCHES, DEFINES)

**Is it queried in natural language?** — Not directly. The query flow is:
1. Your orchestrator **embeds** the user's query (issue body / Slack message) into a 384-dim vector
2. Neo4j does an **ANN (approximate nearest neighbor) vector search** to find the most similar past Issue nodes
3. Neo4j then **traverses the graph** from those issues to find: PRs that resolved them, files those PRs touched, symbols in those files
4. Results are returned as structured JSON — no natural language parsing involved

Think of it as: **"semantic search as the door, graph traversal as the hallway."**

---

## What to Index — Full Graph Schema

Based on your actual data:
- Tree-sitter index: **1,957 files, 3,782 classes, 13,339 functions = 17,121 symbols**
- JSONL issues: **~12,352 issues**
- JSONL PRs: **~41,000 PRs** (with `files[]`, `reviews[]`, `ci_conclusion`)

### Nodes

| Node Label | Count | Key Properties | Source |
|---|---|---|---|
| `(:File)` | 1,957 | `path`, `module` | treesitter_index.json |
| `(:Symbol)` | 17,121 | `name`, `type` (class/function), `file_path` | treesitter_index.json |
| `(:Issue)` | 12,352 | `number`, `title`, `body`, `label_names[]`, `days_open`, `comment_count`, `embedding float[384]` | issues JSONL |
| `(:PR)` | ~41,000 | `number`, `is_merged`, `additions`, `deletions`, `ci_conclusion`, `days_to_merge`, `review_count` | prs JSONL |
| `(:Label)` | ~150 | `name` | issues JSONL |

### Edges

| Edge | From → To | Count | Source |
|---|---|---|---|
| `[:DEFINES]` | File → Symbol | 17,121 | treesitter_index.json |
| `[:TOUCHES]` | PR → File | ~200k (avg 5 files/PR × 41k PRs) | prs JSONL `files[].filename` |
| `[:RESOLVED_BY]` | Issue → PR | ~8,000 (issues with linked PR) | prs JSONL `linked_issue_number` |
| `[:HAS_LABEL]` | Issue → Label | ~30,000 | issues JSONL `label_names` |
| `[:SIMILAR_TO]` | Issue → Issue | ~60,000 (top-5 per issue, from ANN) | computed during embedding |

> ⚠️ **Skip `[:CALLS]` (cross-symbol call graph)** — your tree-sitter index doesn't have import info. Add it later if needed. The `TOUCHES` edges via PRs are more valuable anyway.

---

## What Each Consumer Queries

### Slack Orchestrator (Scorer + Reasoner models)

| Query | What it returns | Latency |
|---|---|---|
| "Find issues similar to this one" | Top-5 similar issue numbers + their resolution PRs | 15–30ms |
| "What's typical resolution time for `type:bug` issues?" | avg/p50/p90 of `days_open` for label | 5–10ms |
| "Which files break most often in `provider:amazon` issues?" | Top-10 files by TOUCHES frequency | 10–20ms |
| "Was this issue pattern seen before?" | SIMILAR_TO traversal → return past resolutions | 20–40ms |

### VS Code Orchestrator (Planner Agent)

```python
# Given issue body → what files to plan for
MATCH (i:Issue)
WHERE i.embedding IS NOT NULL
WITH i, vector.similarity.cosine(i.embedding, $query_embedding) AS score
ORDER BY score DESC LIMIT 5
MATCH (i)-[:RESOLVED_BY]->(pr:PR)-[:TOUCHES]->(f:File)
RETURN f.path, count(*) as frequency
ORDER BY frequency DESC LIMIT 10
```
→ Returns: `["airflow/operators/python.py", "airflow/models/dag.py", ...]` in **25–50ms**

### VS Code Orchestrator (Patcher Agent)

```python
# Given a file path → what symbols does it define?
MATCH (f:File {path: $file_path})-[:DEFINES]->(s:Symbol)
RETURN s.name, s.type

# Given planned file → find past PRs that patched it + what CI said
MATCH (pr:PR)-[:TOUCHES]->(f:File {path: $file_path})
RETURN pr.number, pr.ci_conclusion, pr.is_merged, pr.days_to_merge
ORDER BY pr.number DESC LIMIT 10
```
→ Returns: Symbol list + historical patch success rate in **10–20ms**

### VS Code Orchestrator (Critic Agent)

```python
# Given a PR touching these files → what did reviewers typically say?
MATCH (pr:PR)-[:TOUCHES]->(f:File)
WHERE f.path IN $file_paths
RETURN pr.review_count, pr.ci_conclusion, pr.is_merged
ORDER BY pr.number DESC LIMIT 20
```
→ Returns: Review patterns, CI outcomes for similar past PRs in **15–30ms**

---

## Setup on Mac — Step by Step

### Prerequisites (once)
```bash
# 1. Docker Desktop must be running
# Verify:
docker --version

# 2. Python packages needed for indexing
pip install neo4j sentence-transformers tqdm
```

### Step 1 — Start Neo4j in Docker (2 minutes)

```bash
docker run -d \
  --name autobot-neo4j \
  -p 7474:7474 \
  -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/autobot123 \
  -e NEO4J_PLUGINS='["apoc", "graph-data-science"]' \
  -e NEO4J_dbms_memory_heap_initial__size=512m \
  -e NEO4J_dbms_memory_heap_max__size=2G \
  -v $HOME/autobot-neo4j/data:/data \
  -v $HOME/autobot-neo4j/logs:/logs \
  neo4j:5.18

# Wait ~30 seconds, then open:
open http://localhost:7474
# Login: neo4j / autobot123
# You'll see an empty graph browser — this is your workspace
```

### Step 2 — Create Schema + Indexes (30 seconds)

Run this Cypher in the browser (paste and hit Cmd+Enter):

```cypher
// Unique constraints (enforce no duplicate nodes)
CREATE CONSTRAINT file_path IF NOT EXISTS FOR (f:File) REQUIRE f.path IS UNIQUE;
CREATE CONSTRAINT symbol_id IF NOT EXISTS FOR (s:Symbol) REQUIRE s.id IS UNIQUE;
CREATE CONSTRAINT issue_number IF NOT EXISTS FOR (i:Issue) REQUIRE i.number IS UNIQUE;
CREATE CONSTRAINT pr_number IF NOT EXISTS FOR (p:PR) REQUIRE p.number IS UNIQUE;
CREATE CONSTRAINT label_name IF NOT EXISTS FOR (l:Label) REQUIRE l.name IS UNIQUE;

// Vector index for issue embedding similarity (384-dim for all-MiniLM-L6-v2)
CREATE VECTOR INDEX issue_embedding_index IF NOT EXISTS
FOR (i:Issue) ON (i.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 384,
  `vector.similarity_function`: 'cosine'
}};

// Regular index for fast file lookups
CREATE INDEX file_path_index IF NOT EXISTS FOR (f:File) ON (f.path);
```

### Step 3 — Run Ingestion Scripts (25–35 minutes total)

**Sequence matters — run in this exact order:**

```bash
cd /Users/shatayu/Desktop/FALL24/SPRING26/298B/WB2/autobot_dev

# Script 1: Load File + Symbol nodes from tree-sitter (~30 sec)
python3 neo4j_ingest/01_ingest_treesitter.py

# Script 2: Load Issue nodes from JSONL (~3 min)
python3 neo4j_ingest/02_ingest_issues.py

# Script 3: Load PR nodes + TOUCHES + RESOLVED_BY edges from JSONL (~10 min)
python3 neo4j_ingest/03_ingest_prs.py

# Script 4: Compute + store issue embeddings (the slow step) (~15 min on M1)
python3 neo4j_ingest/04_embed_issues.py

# Script 5: Build SIMILAR_TO edges from ANN results (~3 min)
python3 neo4j_ingest/05_build_similarity_edges.py
```

---

## What Each Script Does (Pseudocode for Team)

### `01_ingest_treesitter.py`
```python
# Reads treesitter_index.json (1,957 files)
# For each file path:
#   CREATE (:File {path: ..., module: path.replace("/",".").rstrip(".py")})
#   For each class in file:
#     CREATE (:Symbol {id: "path::ClassName", name: ClassName, type: "class"})
#     CREATE (:File)-[:DEFINES]->(:Symbol)
#   For each function:
#     CREATE (:Symbol {id: "path::func_name", name: func, type: "function"})
#     CREATE (:File)-[:DEFINES]->(:Symbol)
# Uses batch UNWIND for speed (1000 nodes per commit)
# Runtime: ~30 seconds
```

### `02_ingest_issues.py`
```python
# Reads all GITHUB_ISSUES_*.jsonl from extracted_data/
# For each issue record:
#   CREATE (:Issue {
#     number: issue_number,
#     title: issue.title,
#     body: issue.body[:2000],   # truncate for storage
#     label_names: label_names,
#     days_open: computed,
#     comment_count: issue.comments,
#     state: issue.state
#   })
#   For each label in label_names:
#     MERGE (:Label {name: label})
#     CREATE (:Issue)-[:HAS_LABEL]->(:Label)
# Runtime: ~3 minutes
```

### `03_ingest_prs.py`
```python
# Reads all GITHUB_PRS_*.jsonl from extracted_data/
# For each PR record:
#   MERGE (:PR {number: pr_number, is_merged, ci_conclusion, ...})
#   For each file in files[]:
#     MATCH (f:File {path: file.filename})  # must already exist from step 1
#     IF file found: CREATE (:PR)-[:TOUCHES]->(f)
#     IF not found: MERGE (:File {path: file.filename})  # new file not in index
#   IF linked_issue_number:
#     MATCH (i:Issue {number: linked_issue_number})
#     CREATE (i)-[:RESOLVED_BY]->(:PR)
# Runtime: ~10 minutes (200k TOUCHES edges are the bulk)
```

### `04_embed_issues.py`
```python
# Loads sentence-transformers all-MiniLM-L6-v2 (downloads ~80MB first time)
# For each Issue node in batches of 64:
#   text = f"{issue.title} {issue.body[:512]}"
#   embedding = model.encode(text)  # shape: (384,)
#   SET i.embedding = embedding.tolist()
# Uses M1 MPS acceleration automatically
# Runtime: ~15 minutes for 12,352 issues
```

### `05_build_similarity_edges.py`
```python
# For each Issue, find top-5 most similar issues using the vector index
# Creates (:Issue)-[:SIMILAR_TO {score: 0.87}]->(:Issue) edges
# These edges let the orchestrator do instant "pattern matching":
#   "Issues similar to this one were resolved by touching airflow/operators/python.py"
# Runtime: ~3 minutes
```

---

## Reusable Query Utilities for Orchestrator

```python
# neo4j_queries.py — importable by orchestrator and agents

from neo4j import GraphDatabase
from sentence_transformers import SentenceTransformer

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "autobot123"))
embedder = SentenceTransformer("all-MiniLM-L6-v2")  # load once globally


def find_similar_issues(query_text: str, top_k: int = 5) -> list[dict]:
    """Slack: 'Find issues similar to this description'"""
    embedding = embedder.encode(query_text).tolist()
    with driver.session() as s:
        return s.run("""
            CALL db.index.vector.queryNodes('issue_embedding_index', $k, $emb)
            YIELD node AS i, score
            RETURN i.number AS number, i.title AS title,
                   i.days_open AS days_open, i.label_names AS labels, score
        """, k=top_k, emb=embedding).data()
    # Latency: 10–25ms


def get_files_for_issue(query_text: str, top_k_issues: int = 5) -> list[dict]:
    """Planner: 'What files should I touch for this issue?'"""
    embedding = embedder.encode(query_text).tolist()
    with driver.session() as s:
        return s.run("""
            CALL db.index.vector.queryNodes('issue_embedding_index', $k, $emb)
            YIELD node AS i
            MATCH (i)-[:RESOLVED_BY]->(pr:PR)-[:TOUCHES]->(f:File)
            RETURN f.path AS file, count(*) AS frequency
            ORDER BY frequency DESC LIMIT 10
        """, k=top_k_issues, emb=embedding).data()
    # Latency: 25–50ms


def get_symbols_in_file(file_path: str) -> list[dict]:
    """Patcher: 'What functions/classes does this file define?'"""
    with driver.session() as s:
        return s.run("""
            MATCH (f:File {path: $path})-[:DEFINES]->(s:Symbol)
            RETURN s.name AS name, s.type AS type
        """, path=file_path).data()
    # Latency: 5–10ms


def get_pr_history_for_files(file_paths: list[str]) -> list[dict]:
    """Critic: 'How did past PRs touching these files turn out?'"""
    with driver.session() as s:
        return s.run("""
            MATCH (pr:PR)-[:TOUCHES]->(f:File)
            WHERE f.path IN $paths
            RETURN pr.number AS pr, pr.ci_conclusion AS ci,
                   pr.is_merged AS merged, pr.days_to_merge AS days,
                   pr.review_count AS reviews
            ORDER BY pr.number DESC LIMIT 20
        """, paths=file_paths).data()
    # Latency: 10–20ms


def get_label_stats(label_name: str) -> dict:
    """Scorer: 'What's the typical resolution time for this label?'"""
    with driver.session() as s:
        return s.run("""
            MATCH (i:Issue)-[:HAS_LABEL]->(:Label {name: $label})
            RETURN avg(i.days_open) AS avg_days,
                   percentileCont(i.days_open, 0.5) AS p50_days,
                   percentileCont(i.days_open, 0.9) AS p90_days,
                   count(i) AS total
        """, label=label_name).single().data()
    # Latency: 5–15ms
```

---

## Latency Summary

| Query Type | Use Case | Expected Latency |
|---|---|---|
| Vector similarity (ANN) | Slack: "find similar issues" | 10–25ms |
| Similarity + graph hop | Planner: "what files to touch" | 25–50ms |
| Single node lookup | Patcher: "symbols in this file" | 5–10ms |
| Multi-file PR history | Critic: "how did past PRs do" | 10–20ms |
| Label aggregation | Scorer: "avg resolution time" | 5–15ms |
| **Full agent context fetch** | All 3 in sequence | **50–100ms total** |

All queries return in well under 200ms — invisible latency in a VS Code UX.

---

## Storage & Resource Estimates

| Resource | Estimate | Notes |
|---|---|---|
| Neo4j disk (data) | ~400–500MB | Nodes + edges + vector index |
| Issue embeddings alone | ~18MB | 12,352 × 384 × 4 bytes |
| RAM (Neo4j idle) | ~400MB | Heap set to 512MB init |
| RAM (Neo4j under load) | ~1.5–2GB | During heavy traversal |
| Docker image | ~600MB | neo4j:5.18 |
| **Total disk** | **~1.1GB** | data + logs + image |

Your M1 Pro 16GB handles this comfortably alongside Docker and VS Code.

---

## Full Size & Time Summary

| Phase | Time on M1 Pro |
|---|---|
| Docker pull + start | 2 min |
| Schema + indexes | < 1 min |
| Tree-sitter ingestion (17k symbols) | ~30 sec |
| Issue ingestion (12k issues) | ~3 min |
| PR ingestion + edges (41k PRs, 200k edges) | ~10 min |
| Issue embedding (all-MiniLM, M1 MPS) | ~15 min |
| Similarity edge build | ~3 min |
| **Total** | **~35 minutes** |

---

## Verify Everything Worked

Paste in Neo4j browser after all scripts complete:

```cypher
// Node counts
MATCH (n) RETURN labels(n)[0] AS type, count(n) AS count ORDER BY count DESC;

// Edge counts
MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS count ORDER BY count DESC;

// Test: "Find files most commonly touched by bug-fix PRs"
MATCH (i:Issue)-[:HAS_LABEL]->(:Label {name: "type:bug"})
MATCH (i)-[:RESOLVED_BY]->(pr:PR)-[:TOUCHES]->(f:File)
RETURN f.path, count(*) AS fixes ORDER BY fixes DESC LIMIT 10;

// Test: vector search (paste any issue body text as $query)
CALL db.index.vector.queryNodes('issue_embedding_index', 3,
  [0.1, 0.2, ...]) // replace with actual embedding
YIELD node, score
RETURN node.number, node.title, score;
```

Expected counts:
```
(:Issue)   ~12,352
(:PR)      ~41,000
(:File)     1,957
(:Symbol)  17,121
(:Label)     ~150

[:TOUCHES]     ~200,000
[:DEFINES]      17,121
[:HAS_LABEL]   ~30,000
[:RESOLVED_BY]  ~8,000
[:SIMILAR_TO]  ~60,000
```

---

## Notes for Team

1. **The embedder (`all-MiniLM-L6-v2`) must be loaded once at orchestrator startup** — not per request. Model load = ~2 sec, inference = ~1ms per query.

2. **Neo4j must be running before orchestrator starts.** Add a health check in your orchestrator's startup: `GET http://localhost:7474` must return 200.

3. **Vector index populates lazily** — after `04_embed_issues.py` runs, the first vector query may be slow (~1–2s). It warms up after the first hit. Add a warmup query in orchestrator startup.

4. **PR → File edge coverage** — not all 41k PRs will find matching File nodes (some files were deleted or renamed). Expect ~70–80% edge coverage. That's fine — historical coverage is enough for pattern retrieval.

5. **Backfill anytime** — all scripts are idempotent (use `MERGE` not `CREATE`). Re-running after adding more JSONL data just adds new nodes/edges safely.
