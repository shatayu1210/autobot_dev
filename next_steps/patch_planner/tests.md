# Planner Orchestrator Tests

Run these tests locally to verify the planner refinement loops, conditional logic, and context bounding behaviors.

## 1. Zero-Context Novel Issue Test (Wider GraphRAG Path)
- **Action**: Pass an issue with text completely unrelated to known Airflow bugs (e.g. "Add a mock feature to standard logs"). Ensure `graphrag_candidates` returns empty.
- **Expected**: `detect_triggers()` fires `novel_issue`. The research loop bypasses issue-embedding GraphRAG and instead executes `keyword_search()`. It then runs `get_neighbor_files()` passing the grep-hit paths to find co-modified file structures.
- **Verification**: Check `planner_trace.json` to ensure "GRAPHRAG_NEIGHBORS (novel-issue path)" snippets are populated.

## 2. Bounded Window Accuracy Test
- **Action**: Supply a dummy plan with a specific `code_span` target: `airflow/models/dag.py`, lines 50 to 60.
- **Expected**: `assemble_patcher_input()` does *not* include the entire 4,000-line `dag.py` file. Instead, the `excerpt` in `file_contexts.primary` shows only lines 10 to 100 (±40 line buffer logic).

## 3. Evidence Diversity Check (`compress_delta`)
- **Action**: Flood the research loop `snippets` array with 15 mock hits from `airflow/foo.py` and 2 mock hits from `airflow/bar.py`.
- **Expected**: `compress_delta()` strips out 12 of the `foo.py` hits. The final returned string contains exactly 3 snippets for `foo.py` and 2 for `bar.py`.

## 4. Import & Caller Tracing Test (Cross-File Hops)
- **Action**: Define a test plan targeting `airflow/utils/timezone.py`.
- **Expected**: The research loop successfully executes `get_callers()` finding at least 3 downstream files that call `timezone` functions. `assemble_patcher_input` dynamically locates those downstream files, resolves their paths, and injects their tree-sitter symbols into `file_contexts.supporting`.

## 5. Test File Discovery
- **Action**: Run `find_test_files()` passing `["airflow/api_connexion/endpoints/dag_endpoint.py"]`.
- **Expected**: The function successfully uses regex heuristics to locate and return `tests/api_connexion/endpoints/test_dag_endpoint.py`.
