# AutoBot GraphRAG Schema & Entity Usage Mapping

This document provides the definitive architecture for how the Neo4j GraphRAG database is queried by the system's various orchestrators and models. It outlines the specific graph traversals performed and the exact high-signal fields extracted from the ETL pipeline to feed context into the LLMs.

## Graph Schema definition
Your Neo4j ingestion script should construct the following core nodes from your Snowflake data:

1. **`ISSUE` Node:** `title`, `body_text`, `created_at`, `embedding` (OpenAI/local vector)
2. **`PR` Node:** `title`, `body_text`, `merged_at`, `ci_failure_count`
3. **`FILE` Node:** `filename`
4. **`REVIEW` Node:** `body_text`, `state` (e.g., CHANGES_REQUESTED), `is_inline_comment` (boolean), `diff_hunk` (if inline)

---

## 1. VS Code Orchestrator (General Developer Context Queries)
**Use Case:** The developer is working on a bug and asks the VS Code chat interface: *"I'm seeing a race condition in the scheduler when handling missing task instances. How was this handled in the past?"*
**GraphRAG Traversal:** `Developer Query Embedding -> [SIMILAR_TO] -> Historical Issues -> [RESOLVED_BY] -> Historical PRs -> [TOUCHES] -> Files`
**High-Signal Fields Provided:**
*   `historical_pr.title` & `historical_pr.body`: Explains the previous engineering solution directly to the developer.
*   `historical_pr.merged_at`: Calibrates expectations on how long the fix should take.
*   `pr_files[].filename`: Immediately points the developer's attention to the files they likely need to open in their IDE.

## 2. Slack Orchestrator (Ad-Hoc Issue Escalation Queries)
**Use Case:** A scrum master or product manager queries the Slack bot: *"Are there any previous tickets similar to this new UI bottleneck #1042?"*
**GraphRAG Traversal:** `Issue Embedding -> [SIMILAR_TO] -> Historical Issues`
**High-Signal Fields Provided:**
*   `historical_issue.title` & `historical_issue.body`: Provides semantic proof to the non-technical user that this is a known/recurring problem pattern.
*   `historical_issue.created_at` & `status`: Contextualizes how old the historically similar issues were and if they ever got resolved.

## 3. VS Code Orchestrator: The Planner Model
**Use Case:** The Planner needs to know exactly which files to target for its code patch without replying on naive keyword/regex matching against the file tree.
**GraphRAG Traversal:** `Current Issue Embedding -> [SIMILAR_TO] -> Historical Issues -> [RESOLVED_BY] -> Historical PRs -> [TOUCHES] -> Files`
**High-Signal Fields Provided:**
*   `pr_files[].filename`: The absolute ground-truth list of files touched in past similar PRs. Ranked by aggregate traversal frequency, this list is injected into the Planner as the `CANDIDATE_FILES` context.

## 4. VS Code Orchestrator: The Patcher Model
**Use Case:** After the Planner identifies *what* to edit, the Patcher needs to know *how* to idiomatically edit it. It needs the design patterns historically applied to those exact files.
**GraphRAG Traversal:** `Target File -> [TOUCHED_BY] -> Historical PRs`
**High-Signal Fields Provided:**
*   **PR Descriptions:** `pr.body` (e.g., contains architectural intent like *"Migrated database reads to async session"*).
*   **Commit Messages:** `pr.commits[].message`. Dense, atomic signals of changes applied to the target file.
*   *Why this is crucial:* It provides actual idiomatic code patterns from successful past developers, which is vastly superior to a simple historical CI failure statistic.

## 5. VS Code Orchestrator: The Critic Model
**Use Case:** The Critic must evaluate the Patcher's proposed diff. It needs to know the team conventions and historical "gotchas" for the specific files modified, allowing it to catch subjective or architectural flaws before CI even runs.
**GraphRAG Traversal:** `Target File -> [REVIEWED_IN] -> Historical PR Reviews & Comments`
**High-Signal Fields Provided:**
*   **Review Friction Summaries:** `pr_reviews[].body` where `state = 'CHANGES_REQUESTED'`. This grants the Critic broad architectural guardrails (e.g., *"This file must not import from airflow.models due to circular dependencies"*).
*   **Inline Code Friction:** `pr_review_comments[].body` mapped to the `diff_hunk`. This is the goldmine signal. It gives the Critic exact, localized engineering feedback (e.g., *"Remember to call .close() on the session here, we've had memory leaks in this function before"*).
*   **Historical CI Failures:** `check_runs[].conclusion = 'failure'`. When combined with review comments, this helps the model flag historically fragile files.
