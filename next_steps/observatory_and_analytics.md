# Observatory & Analytics: Prometheus, Grafana, and Snowflake

This document is a **graduate-project–level** blueprint for observability once AutoBot’s five model agents and two orchestrators are deployed and integrated. It is written so a teammate or an AI IDE can implement instrumentation, choose storage, and set up dashboards without guessing at conventions.

**Scope**

- **Five agents (models)**: Scorer, Reasoner, Patch Planner, Patch Patcher, Critic.  
- **Two orchestrators**:
  1. **Session / pipeline orchestrator** — end-to-end flow (issue in → plan → optional refine → patch → review).
  2. **Refinement / retrieval orchestrator** — conditional second pass (GraphRAG expansion, keyword search, code reads) before re-invoking Planner (or other agents as designed).

**Three pillars (beginner view)**

| System | Best for | Typical data |
|--------|----------|--------------|
| **Prometheus** | Real-time health, SLOs, alerts | Counters, gauges, histograms (time series, scraped every ~15s–1m) |
| **Grafana** | Dashboards and alerts on those series | Reads Prometheus (and optionally Snowflake via connector or SQL proxy) |
| **Snowflake** | Ad hoc analytics, experiment joins, “why did this fail?” | Structured **events** and **traces** (one row per request or per sub-step) |

**Rule of thumb**

- Hot path (latency, errors, QPS) → **Prometheus + Grafana**.  
- Deep forensics, model-version A/B, cost attribution → **Snowflake** (or object store + load into Snowflake).  
- **Never** use Prometheus as a general log or event store; it is for numeric time series.  
- **Do not** stream full prompts/responses to Prometheus; redact and sample for Snowflake or a dedicated log system.

### In practice for this project (AutoBot)

- **Prometheus + Grafana** → health, SLOs, “is the system on fire?”
- **Logs** (structured JSON logs) → why a single request failed; include **correlation id** and/or **trace id** on every line
- **Traces** (optional but useful) → where in the multi-agent flow time was spent (Planner → refinement → external calls)
- **Snowflake** (or batch export) → deeper analytics, experiments, joins to labels — **not** the first line for “live debug this one error”

For failure investigation, use the flow: **metrics (Grafana) to spot the spike** → **logs / traces to explain one request** → **Snowflake for cohort analysis** after the fact.

---

## 1) Naming and labels (use everywhere)

**Metric names** (Prometheus): `autobot_<subsystem>_<name>_<unit>`, e.g. `autobot_planner_requests_total`.  
**Labels** (low cardinality only):

- `env` — `dev`, `staging`, `prod`
- `agent` — `scorer`, `reasoner`, `planner`, `patcher`, `critic`
- `orchestrator` — `session`, `refinement` (or your chosen IDs)
- `model_version` — image tag or adapter hash **short** (e.g. `v5-9b61268`), not a full path
- `outcome` — `success`, `error`, `timeout`, `invalid_output`
- `refinement` — `false`, `true` (was second pass used?)

Avoid high-cardinality labels (user_id, issue_number, file path) on Prometheus series; put those in Snowflake events.

---

## 2) What to put where

### Prometheus (metrics)

- Request counts, error counts, latency histograms, queue depth, GPU/CPU if exposed, token usage **as aggregates** (histogram buckets or per-request summed to a counter with bounded labels if you must).  
- Orchestrator: refinement triggers, ladder level reached, budget exhausted.

### Grafana

- Dashboards: RED (rate, errors, duration) per agent; orchestrator flow; SLO burn.  
- Alert rules (via Grafana Alerting or Alertmanager) on error rate, latency p99, refinement storm.

### Snowflake (events / analytics)

- One table (or few) for **request** and **sub-step** events with: `request_id`, `issue_id` or `trace_id`, `agent`, `model_version`, `latency_ms`, `input_tokens`, `output_tokens`, `outcome`, `refinement_tier` (0–3), optional `redacted_reason` length / hash only.  
- Join to evaluation labels in batch jobs.  
- Ad hoc: “How often does refinement flip NO→YES for planner v5?”

**Optional** for raw logs: Loki, Elasticsearch, or CloudWatch — out of scope here; link from Snowflake with `log_ref` if needed.

---

## 3) Standard metric patterns (implement per service)

For **each** HTTP or RPC handler that calls an agent:

| Pattern | Type | Name example | Notes |
|--------|------|----------------|------|
| Requests | Counter | `autobot_<agent>_requests_total` | `outcome` label |
| Latency | Histogram | `autobot_<agent>_latency_seconds` | buckets: 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32 |
| Errors | Counter | `autobot_<agent>_errors_total` | `error_type` if finite set |
| In-flight (optional) | Gauge | `autobot_<agent>_inflight` | for concurrency limits |

**Orchestrator-specific**

| Pattern | Type | Name example |
|--------|------|----------------|
| Pass | Counter | `autobot_orchestrator_session_passes_total` — `pass` = `first`, `refinement` |
| Refinement trigger | Counter | `autobot_orchestrator_refinement_triggers_total` — `agent` = `planner` |
| Ladder level | Histogram or Counter | `autobot_refinement_retrieval_level_max` (numeric 0–3) |
| Time to first token (optional) | Histogram | `autobot_<agent>_ttft_seconds` |

### Token and cost (aggregates only in Prometheus)

Prefer:

- `autobot_<agent>_tokens_input_total` / `tokens_output_total` (counters) incremented per request, **no** per-issue label.

For cost dashboards, multiply by a static price per 1K tokens in Grafana or in Snowflake.

---

## 4) Per-agent catalog (deep enough to implement)

### 4.1 Scorer

- `autobot_scorer_requests_total` — labels: `outcome`  
- `autobot_scorer_latency_seconds`  
- `autobot_scorer_score_distribution` — optional histogram of numeric score if schema fixed  
- Log to Snowflake: `request_id`, `pr_or_issue_id`, `model_version`, `score`, `latency_ms`, `token_counts`

### 4.2 Reasoner

- Same pattern as Scorer.  
- If multi-step: `autobot_reasoner_step_latency_seconds` — `step` in bounded set, e.g. `chain_of_thought`, `final`.

### 4.3 Patch Planner

- `autobot_planner_requests_total` — `outcome`, `refinement` (true if this invocation was post-refinement)  
- `autobot_planner_latency_seconds`  
- `autobot_planner_yes_no_total` — `label` = `yes`, `no` (aggregate only)  
- `autobot_planner_grounding_failures_total` — if you detect empty candidates / parse failure

Snowflake: decision `yes_no`, `candidate_files_count`, `graphrag_k_used`, `treesitter_spans_n`, `refinement_tier`, `parent_request_id` if second pass.

### 4.4 Patch Patcher

- `autobot_patcher_requests_total`  
- `autobot_patcher_diff_valid_total` — `valid` = `true`/`false` (bounded)  
- `autobot_patcher_files_touched` — histogram (1–3 or buckets)

### 4.5 Critic

- `autobot_critic_requests_total`  
- `autobot_critic_verdict_total` — `verdict` in bounded set: `pass`, `fail`, `revise`  
- `autobot_critic_latency_seconds`

---

## 5) Orchestrators (two) — detailed

### 5.1 Session / pipeline orchestrator

**Responsibility**: Route issue → (optional) scorer/reasoner → planner → (optional) patcher → critic; manage IDs and session state.

**Metrics**

- `autobot_orchestrator_session_starts_total`  
- `autobot_orchestrator_session_completions_total` — `outcome` = `success` / `partial` / `error`  
- `autobot_orchestrator_session_duration_seconds` — end-to-end histogram  
- `autobot_orchestrator_agent_invocations_total` — `agent` = each of five (how many times each ran per “session” if you expose per session, use aggregate counter without session id)  
  - **Better**: one counter per edge: `autobot_orchestrator_stage_total` — `stage` = `planner`, `patcher`, …

**Snowflake** (one row per completed session, or per stage):

- `trace_id`, `issue_id`, `stages_executed` (array or JSON), `total_latency_ms`, `planner_refinement_used` boolean, `error_stage` if any.

### 5.2 Refinement / retrieval orchestrator

**Responsibility**: Triggers, ladder level, second planner call, token budget for evidence pack.

**Metrics**

- `autobot_refinement_triggers_total` — `agent` = `planner` (who was refined)  
- `autobot_refinement_retrieval_level_reached` — `level` in `0,1,2,3` (Counter per level)  
- `autobot_refinement_evidence_snippets_injected` — histogram (count per pass-2)  
- `autobot_refinement_budget_exhausted_total`  
- `autobot_refinement_flip_total` — `from` = `no`, `to` = `yes` (only if you can detect; else Snowflake only)

**Snowflake**

- `refinement_trigger_reason` (enum), `levels_executed[]`, `snippets_dropped_due_to_budget` int, `pass1_decision`, `pass2_decision`.

---

## 6) Grafana: recommended dashboards (panels)

1. **Overview** — RPS, error %, p50/p99 latency per agent.  
2. **Orchestrator** — session completion rate, refinement trigger rate, level distribution.  
3. **Planner** — pass-1 vs pass-2 latency, YES/NO rates (aggregate), grounding failure rate.  
4. **SLO** — e.g. 99% of sessions &lt; 60s, burn rate.  
5. **Cost** — `tokens_input_total` + `tokens_output_total` per hour (Grafana transform).

Alert examples:

- `rate(autobot_planner_errors_total[5m]) / rate(autobot_planner_requests_total[5m]) > 0.05`  
- `histogram_quantile(0.99, autobot_orchestrator_session_duration_seconds) > 120`

---

## 7) Snowflake: minimal schema (analytics)

**Table: `AUTOBOT_OBSERVABILITY.AGENT_EVENTS`** (illustrative)

| Column | Type | Notes |
|--------|------|--------|
| `event_id` | VARCHAR | UUID |
| `trace_id` | VARCHAR | One per user request |
| `parent_event_id` | VARCHAR | For nested calls |
| `ts` | TIMESTAMP_TZ | Event time |
| `env` | VARCHAR | dev/staging/prod |
| `agent` | VARCHAR | scorer, reasoner, … |
| `orchestrator` | VARCHAR | session, refinement, null |
| `model_version` | VARCHAR | |
| `latency_ms` | NUMBER | |
| `input_tokens` | NUMBER | |
| `output_tokens` | NUMBER | |
| `outcome` | VARCHAR | success, error, … |
| `metadata` | VARIANT | JSON: refinement_level, file_count, yes_no, etc. |

**ETL options**

- Batch: hourly JSON files → `COPY INTO`  
- Stream: Kafka / queue → Snowpipe  
- For a thesis: batch is enough.

**Queries you want to run**

- Refinement flips and precision impact by `model_version`.  
- Median evidence snippets by level.  
- Correlation between `treesitter_spans` and correct YES@k.

---

## 8) Implementation checklist (for AI IDE or engineer)

1. **Instrument** each agent service with Prometheus client (Python: `prometheus_client`; expose `/metrics` on a dedicated port or path).  
2. **Add middleware** to emit `requests_total`, `latency_seconds`, `errors_total` for each route.  
3. **Orchestrator** emits stage counters and refinement-specific metrics at decision points.  
4. **Deploy Prometheus** with scrape configs pointing at all service `/metrics` endpoints.  
5. **Deploy Grafana**, add Prometheus data source, import/create dashboards in §6.  
6. **Emit Snowflake events** asynchronously (do not block request path; use queue or batch buffer).  
7. **PII/secret policy**: no raw tokens in metrics; redact in Snowflake `metadata` or store hashes.  
8. **Document** SLOs and on-call runbooks in `docs/` or this folder.

---

## 9) How this differs from “dumping APIs to Snowflake”

- **APIs** (HTTP endpoints) are not “dumped” into Snowflake.  
- You **export structured events** (one row per logical step) from the same process that serves the API.  
- **Prometheus** scrapes **metrics** from the process; it does not replace a warehouse.  
- **Grafana** visualizes Prometheus (and can query Snowflake for slower, richer reports if configured).

This split keeps production monitoring fast and cheap, while still enabling rigorous thesis-level analysis in Snowflake.

---

## 10) Reference reading order

1. `next_steps/patch_planner/README.md` — refinement loop and context budgeting.  
2. This file — cross-cutting observability.  
3. Red Hat / CNCF SRE patterns (RED/USE) for more on SLOs.

When deployment topology is known (K8s vs single VM), add a short “Service discovery and scrape” subsection to the repo and point `prometheus.yml` at stable DNS names for each `autobot-*` service.
