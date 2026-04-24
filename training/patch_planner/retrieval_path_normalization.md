# Planner Retrieval Path Normalization (Training + Inference Parity)

This note documents path normalization rules added to `build_planner_data.py` so training context and production context stay aligned.

## Why this was needed

GraphRAG candidate files are often in classic layout paths like:
- `airflow/models/taskinstance.py`
- `airflow/providers/google/cloud/hooks/gcs.py`

Tree-sitter index keys from newer monorepo layout are often:
- `airflow-core/src/airflow/models/taskinstance.py`
- `providers/google/src/airflow/providers/google/cloud/hooks/gcs.py`

Without aliasing, many valid GraphRAG candidates fail to map to Tree-sitter entries.

## Alias rules (implemented)

For each candidate path `p`, the builder tries:

1. Direct normalized path:
- `p`

2. Core alias:
- If `p` starts with `airflow/`, also try `airflow-core/src/{p}`

3. Provider alias:
- If `p` starts with `airflow/providers/{provider}/...`, also try
  `providers/{provider}/src/airflow/providers/{provider}/...`

First existing key in `treesitter_index` is used.

## Required production parity

Replicate the exact same alias logic in inference-time orchestrator retrieval before building Tree-sitter context for Planner/Patcher.

If training uses alias mapping but production does not, you'll introduce a train/inference context mismatch.

## Notes

- The builder now prints coverage stats at the end (GraphRAG + Tree-sitter coverage).
- Test files are still excluded from index by current Tree-sitter builder defaults.
  If test-symbol context is desired, include tests in index build and re-run dataset build.
