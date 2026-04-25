# Tree-sitter Index Workflow

This folder contains tooling for building `treesitter_index.json` from a local Airflow clone.

## When to rebuild

Rebuild the index whenever:
- the Airflow source snapshot changes, or
- ETL training data was refreshed after **2026-04-17** and you want retrieval context aligned with newer paths/history.

## Update steps

1. Refresh local Airflow clone:
   - Pull latest changes in `tree_sitter/airflow/` (or reclone if needed).
2. Rebuild index:

```bash
cd tree_sitter
python3 build_treesitter_index.py \
  --repo "/Users/shatayu/Desktop/FALL24/SPRING26/298B/WB2/autobot_dev/tree_sitter/airflow" \
  --output "/Users/shatayu/Desktop/FALL24/SPRING26/298B/WB2/autobot_dev/tree_sitter/treesitter_index.json"
```

3. Confirm output exists and has non-trivial size:
   - `tree_sitter/treesitter_index.json`

## Notes

- `training/patch_planner/build_planner_data.py` can load this path directly.
- Keep index generation repeatable by documenting the Airflow revision used for each rebuild.
