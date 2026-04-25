# AutoBot Agentic Patcher - Detailed Handover & Specifications

## 1. Role & Inference Loop

You own **AutoBot Patcher (Model 4)** in the Planner -> Patcher -> Critic workflow.

Inference order in VS Code:
1. Planner outputs target files + intent.
2. Orchestrator refinement verifies/updates plan with bounded code evidence.
3. Tree-sitter spans are fetched from base-file content for Python targets where AST symbols are available.
4. If AST symbols are unavailable (including non-Python files), the builder injects fallback hunk-window spans so rows remain grounded.
5. Patcher receives structured directive + context and must emit **unified diff only**.

Guardrail expectation:
- No edits outside allowed files.
- No unrelated refactors.
- Unified diff must parse (`--- a/`, `+++ b/`, `@@`).

---

## 2. Current Dataset Builder (Production Version)

Dataset builder script:
- `training/patch_patcher/build_patcher_data.py`

Outputs:
- `patcher_train.jsonl`
- `patcher_eval.jsonl`
- `patcher_test.jsonl`
- `dataset_report.json`

### Input source
- `etl/training_data/prs_clean.jsonl` (or CSV equivalent)
- local git repo checkout (for `base_sha` blob extraction)
- Neo4j (optional GraphRAG context)

### JSONL row shape
Each row contains nested:
- `id`, `split`, `repo`, `task_type`
- `input`:
  - `instruction`
  - `planner_directive`
  - `issue_context`
  - `treesitter_context` (language/base_sha/spans)
  - `graphrag_context` (candidate files + idioms + optional CI stats)
  - `constraints` (allowed files, output format, no unrelated refactors)
- `output`:
  - `unified_diff`
  - `touched_files`
  - `gold_hunk_headers`
- `meta`:
  - SHAs, touched counts, patch/add/delete totals
  - quality labels
  - provenance

---

## 3. Filters & Quality Gates (Current Defaults)

The builder intentionally applies strict bounds to train a robust patch generator:

- `max_files_touched <= 3` (or optional `--single-file-only`)
- `max_additions <= 350`
- `max_patch_tokens <= 1200`
- sequence budget cap (`--max-seq-tokens`, default 4096)
- keep rows with actual patch text
- enforce unified-diff structural validity
- touched files must be subset of allowed planner files

### File type scope
Current one-off final builder supports multiple extensions (not only Python):
- `.py,.ts,.tsx,.js,.jsx,.sql,.yaml,.yml,.json,.md,.rst,.toml,.ini,.cfg,.sh,.dockerfile`

Path normalization preserves dotfiles (e.g. `.pre-commit-config.yaml`) and only strips explicit leading `./` segments.
Tree-sitter span extraction is Python AST-first; non-Python files get base-file hunk-window fallback spans (`symbol_type=fallback_hunk_window`) so `treesitter_context.spans` is still populated when possible.

---

## 4. Runtime Optimization & Robustness

### Fast prefilter stage
Before expensive per-row processing (git blob + Neo4j + AST), the script applies cheap numeric/path prefilters.  
This significantly reduces wall-clock build time.

### Robust failure handling
- `files=null` rows handled safely.
- Missing `sha:file` blobs (renames/deletes/history drift) do not crash full run; rows continue with tracked failure counters.
- Graph query avoids hard dependency on unavailable `ci_conclusion` property.
- End-of-run summary now reports span enrichment counters:
  - rows with any span
  - rows with fallback spans
  - rows with non-Python touched files
  - rows with non-Python and any span
  - total AST spans
  - total fallback spans
- End-of-run summary also prints dotfile-path sanity on eval split:
  - bad ref count for `a/pre-commit-config.yaml`
  - good ref count for `a/.pre-commit-config.yaml`
  - PASS/WARN status

---

## 5. Split Strategy and Validation

- PR-level split only
- Stratified 80/10/10 by file-count + patch-size buckets
- Duplicate IDs rejected
- Missing SHA rows rejected
- Non-serializable rows rejected

The script prints summary + sanity samples at end and writes full counts/distributions to `dataset_report.json`.

Recommended delivery bundle for teammates:
- `patcher_train.jsonl`
- `patcher_eval.jsonl`
- `patcher_test.jsonl`
- `dataset_report.json`
- optional but recommended: exact build command + `git rev-parse HEAD`

---

## 6. Training Notes

Recommended patcher fine-tuning profile (Qwen2.5-Coder-7B):
- max length ~3072
- LoRA rank ~64
- batch size ~4 (with grad accumulation)
- LR ~5e-5 (drop to 2.5e-5 if unstable)

Use `patcher_train.jsonl` as train source and track:
- valid diff rate
- touches-only-allowed-files rate
- structural/compile pass labels
- eval split exact-format compliance
