"""
Build patcher FT dataset aligned with inference orchestrator context assembly.

Proxies Planner scope from issue/PR metadata; builds file_contexts (primary /
supporting / tests) similar to patcher-orchestrator; supervised rows set
allowed_edit_files to gold-touched paths.

Outputs:
  - patcher_train.jsonl
  - patcher_eval.jsonl
  - patcher_test.jsonl
  - dataset_report.json

Optional: set MAX_TRAIN_EXAMPLES to cap train rows after the stratified split (faster SFT;
eval/test unchanged). See training/patch_patcher/patcher_handover.md for sample-size guidance.

Run directly: edit INPUT_PATH / REPO_ROOT / OUTPUT_DIR below, then
  python training/patch_patcher/build_patcher_data.py
"""

from __future__ import annotations

import ast
import json
import logging
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

try:
from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None


LOG = logging.getLogger("build_patcher_data")

# -----------------------------------------------------------------------------
# Direct run: edit paths and knobs (no argparse).
# -----------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
# build_patcher_data.py → training/patch_patcher/: parents[1] is autobot_dev repo root
_WORKSPACE_ROOT = _THIS_DIR.parents[1]

INPUT_PATH = _WORKSPACE_ROOT / "etl" / "training_data" / "prs_clean.jsonl"
REPO_ROOT = _WORKSPACE_ROOT
OUTPUT_DIR = _THIS_DIR / "outputs"

RNG_SEED = 42
MAX_FILES_TOUCHED = 3
SINGLE_FILE_ONLY = False
MAX_ADDITIONS = 350
MAX_PATCH_TOKENS = 1200
MAX_SEQ_TOKENS = 12288
SPLIT_TRAIN = 0.8
SPLIT_EVAL = 0.1
SPLIT_TEST = 0.1
# After the 80/10/10 split, optionally cap train rows (None = full train). Useful for
# shorter LoRA runs; eval/test are unchanged. Reproducible shuffle uses cfg.seed + 4133.
MAX_TRAIN_EXAMPLES: int | None = None
ENABLE_TREESITTER_LEGACY_SPANS = True
ENABLE_GRAPHRAG = True
GRAPHRAG_TOP_K = 6
GRAPHRAG_QUERY_VERSION = "v1_file_neighbors"
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "autobot_password"
ISSUE_TITLE_COL = "issue_title"
ISSUE_BODY_COL = "issue_body"
ISSUE_LABELS_COL = "issue_labels"
ISSUE_COMMENTS_COL = "comments_summary"
_ALLOWED_EXT_STR = ".py,.ts,.tsx,.js,.jsx,.sql,.yaml,.yml,.json,.md,.rst,.toml,.ini,.cfg,.sh,.dockerfile"
ALLOWED_EXTENSIONS: set[str] = {x.strip().lower() for x in _ALLOWED_EXT_STR.split(",") if x.strip()}

PRIMARY_FULL_MAX_LINES = 380
PRIMARY_WINDOW_PAD = 20
PRIMARY_MAX_LINES_WINDOW_MODE = 280
MODULE_SEARCH_PREFIXES = ("", "src", "lib", "packages")

SUPPORTING_TOP_K_WINDOWS = 12
SUPPORTING_MAX_LINES_PER_WINDOW = 95
GRAPH_SUPPORTING_SNIPPET_LINES = 70
SAME_FILE_SUPPORTING_SYMBOLS = 4
IMPORT_RESOLVE_MAX = 12
CALLERS_RG_MAX_FILES = 4

TEST_CONTEXT_MAX_WINDOWS = 8
TEST_MAX_LINES_PER_WINDOW = 200


def _default_allowed_extensions() -> set[str]:
    return set(ALLOWED_EXTENSIONS)


@dataclass
class Config:
    input_path: Path
    repo_path: Path
    out_dir: Path
    seed: int
    max_files_touched: int
    single_file_only: bool
    max_additions: int
    max_patch_tokens: int
    max_seq_tokens: int
    split_train: float
    split_eval: float
    split_test: float
    enable_treesitter: bool
    enable_graphrag: bool
    graphrag_top_k: int
    graphrag_query_version: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    issue_title_col: str
    issue_body_col: str
    issue_labels_col: str
    issue_comments_col: str
    allowed_extensions: set[str]
    tracked_py_paths: frozenset[str]
    max_train_examples: int | None = None

    primary_full_max_lines: int = PRIMARY_FULL_MAX_LINES
    primary_window_pad: int = PRIMARY_WINDOW_PAD
    primary_max_lines_window_mode: int = PRIMARY_MAX_LINES_WINDOW_MODE
    supporting_top_k_windows: int = SUPPORTING_TOP_K_WINDOWS
    supporting_max_lines_per_window: int = SUPPORTING_MAX_LINES_PER_WINDOW
    enable_callers_rg: bool = True


def build_default_config(tracked_py: frozenset[str]) -> Config:
    splits = SPLIT_TRAIN + SPLIT_EVAL + SPLIT_TEST
    if round(splits, 6) != 1.0:
        raise ValueError("SPLIT_TRAIN + SPLIT_EVAL + SPLIT_TEST must sum to 1.0")
    return Config(
        input_path=INPUT_PATH,
        repo_path=REPO_ROOT,
        out_dir=OUTPUT_DIR,
        seed=RNG_SEED,
        max_files_touched=MAX_FILES_TOUCHED,
        single_file_only=SINGLE_FILE_ONLY,
        max_additions=MAX_ADDITIONS,
        max_patch_tokens=MAX_PATCH_TOKENS,
        max_seq_tokens=MAX_SEQ_TOKENS,
        split_train=SPLIT_TRAIN,
        split_eval=SPLIT_EVAL,
        split_test=SPLIT_TEST,
        enable_treesitter=ENABLE_TREESITTER_LEGACY_SPANS,
        enable_graphrag=ENABLE_GRAPHRAG,
        graphrag_top_k=GRAPHRAG_TOP_K,
        graphrag_query_version=GRAPHRAG_QUERY_VERSION,
        neo4j_uri=NEO4J_URI,
        neo4j_user=NEO4J_USER,
        neo4j_password=NEO4J_PASSWORD,
        issue_title_col=ISSUE_TITLE_COL,
        issue_body_col=ISSUE_BODY_COL,
        issue_labels_col=ISSUE_LABELS_COL,
        issue_comments_col=ISSUE_COMMENTS_COL,
        allowed_extensions=_default_allowed_extensions(),
        tracked_py_paths=tracked_py,
        max_train_examples=MAX_TRAIN_EXAMPLES,
    )


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def fail_fast_checks(cfg: Config, df: pd.DataFrame) -> None:
    if not cfg.input_path.exists():
        raise FileNotFoundError(f"Input dataset not found: {cfg.input_path}")
    if not cfg.repo_path.exists():
        raise FileNotFoundError(f"Repo path not found: {cfg.repo_path}")
    required = {
        "repo",
        "pr_number",
        "base_sha",
        "head_sha",
        "files_json",
        "total_additions",
        "total_deletions",
        "total_patch_tokens",
    }
    missing = [c for c in sorted(required) if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")


def load_input_dataframe(input_path: Path) -> pd.DataFrame:
    """
    Load either:
      - CSV with required flat columns, or
      - JSONL in prs_clean-like nested format and normalize into required columns.
    """
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(input_path)
    if suffix == ".jsonl":
        rows = []
        with input_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                pr = obj.get("pr", {})
                files = obj.get("files", [])
                if files is None:
                    files = []
                if not isinstance(files, list):
                    files = []
                rows.append(
                    {
                        "repo": obj.get("repo", ""),
                        "pr_number": obj.get("pr_number") or pr.get("number"),
                        "base_sha": ((pr.get("base") or {}).get("sha") if isinstance(pr.get("base"), dict) else None),
                        "head_sha": ((pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None),
                        "files_json": json.dumps(files, ensure_ascii=False),
                        "total_additions": pr.get("additions", 0),
                        "total_deletions": pr.get("deletions", 0),
                        "total_patch_tokens": sum(
                            len((x.get("patch") or "").split())
                            for x in files
                            if isinstance(x, dict) and x.get("patch")
                        ),
                        "pr_title": pr.get("title", ""),
                        "pr_body": pr.get("body", ""),
                        "issue_title": "",
                        "issue_body": "",
                        "issue_labels": "",
                        "comments_summary": "",
                    }
                )
        return pd.DataFrame(rows)
    raise ValueError(f"Unsupported input format: {input_path}. Use .csv or .jsonl")


def parse_json_field(v: Any, default: Any) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return default
    if isinstance(v, (dict, list)):
        return v
    s = str(v).strip()
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def parse_labels(v: Any) -> list[str]:
    data = parse_json_field(v, None)
    if isinstance(data, list):
        return [str(x) for x in data]
    if data is None:
        s = str(v).strip() if v is not None else ""
        return [x.strip() for x in s.split(",") if x.strip()] if s else []
    return [str(data)]


def file_allowed_by_extension(path: str, allowed_extensions: set[str]) -> bool:
    p = normalize_file_path(path).lower()
    if p.endswith("/dockerfile") or p == "dockerfile":
        return ".dockerfile" in allowed_extensions or "dockerfile" in allowed_extensions
    for ext in allowed_extensions:
        if p.endswith(ext):
            return True
    return False


def likely_has_allowed_patch(files_json_value: Any, allowed_extensions: set[str]) -> bool:
    """
    Cheap prefilter to avoid expensive processing on obviously irrelevant rows.
    Returns True if files_json appears to contain at least one allowed file with patch text.
    """
    files = parse_json_field(files_json_value, [])
    if not isinstance(files, list) or not files:
        return False
    for f in files:
        if not isinstance(f, dict):
            continue
        fname = normalize_file_path(f.get("filename", ""))
        patch = f.get("patch")
        if file_allowed_by_extension(fname, allowed_extensions) and bool(patch):
            return True
    return False


def normalize_file_path(p: str) -> str:
    s = str(p or "").replace("\\", "/").strip()
    # Remove only explicit leading "./" segments; preserve dotfiles.
    while s.startswith("./"):
        s = s[2:]
    return s


def git_list_tracked_paths(repo_path: Path, suffix: str = ".py") -> frozenset[str]:
    cmd = ["git", "-C", str(repo_path), "ls-files", "-z"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        return frozenset()
    out = []
    for rel in proc.stdout.split(b"\0"):
        if not rel:
            continue
        decoded = rel.decode("utf-8", errors="replace").replace("\\", "/")
        if decoded.lower().endswith(suffix.lower()):
            out.append(decoded.strip())
    return frozenset(out)


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    intervals = [(a, b) for a, b in intervals if a <= b]
    if not intervals:
        return []
    intervals.sort(key=lambda x: (x[0], x[1]))
    merged: list[tuple[int, int]] = [intervals[0]]
    for lo, hi in intervals[1:]:
        ml, mh = merged[-1]
        if lo <= mh + 1:
            merged[-1] = (ml, max(mh, hi))
        else:
            merged.append((lo, hi))
    return merged


def clip_windows_to_budget(
    intervals: list[tuple[int, int]], max_lines: int
) -> list[tuple[int, int]]:
    if not intervals:
        return []
    total = sum(h - lo + 1 for lo, h in intervals)
    if total <= max_lines:
        return intervals
    out: list[tuple[int, int]] = []
    budget = max_lines
    for lo, hi in intervals:
        span = hi - lo + 1
        if span <= budget:
            out.append((lo, hi))
            budget -= span
            if budget <= 0:
                break
            continue
        out.append((lo, lo + budget - 1))
        break
    return merge_intervals(out)


def is_probable_test_path(path: str) -> bool:
    p = path.lower().replace("\\", "/")
    if "/tests/" in p or "/test/" in p or "/testing/" in p:
        return True
    bn = Path(p).name
    if bn.startswith("test_") or bn.endswith("_test.py") or bn == "conftest.py":
        return True
    if bn.startswith("test") and bn.endswith(".py"):
        return True
    return False


def numbered_text(lines: list[str], lo: int, hi: int) -> str:
    parts = []
    for i, ln in enumerate(lines[lo - 1 : hi], start=lo):
        parts.append(f"{i:6d}|{ln}")
    return "\n".join(parts)


def build_primary_windows(
    repo_path: Path,
    base_sha: str,
    fname: str,
    patch_text: str,
    cfg: Config,
) -> list[dict[str, Any]]:
    base_text = get_blob_at_sha(repo_path, base_sha, fname)
    lines = base_text.splitlines()
    n = len(lines)
    headers = parse_hunk_headers(patch_text)
    if not headers and n > 0:
        headers = [{"old_start": 1, "old_count": min(80, n), "new_start": 1, "new_count": min(80, n)}]

    intervals: list[tuple[int, int]] = []
    pad = cfg.primary_window_pad
    for h in headers:
        lo = max(1, int(h["old_start"]) - pad)
        hi = min(n, int(h["old_start"]) + max(int(h["old_count"]), 1) + pad - 1)
        if n > 0:
            intervals.append((lo, hi))
    intervals = merge_intervals(intervals)

    if n > 0 and n <= cfg.primary_full_max_lines:
        presentation = "full"
        slices = [(1, n)]
    elif intervals:
        presentation = "window"
        slices = clip_windows_to_budget(intervals, cfg.primary_max_lines_window_mode)
    else:
        presentation = "window"
        slices = [(1, min(n, cfg.primary_max_lines_window_mode) if n else 1)] if n else [(1, 1)]

    out: list[dict[str, Any]] = []
    for lo, hi in slices:
        if not lines:
            out.append(
                {
                    "path": fname,
                    "role": "primary",
                    "source": "changed_in_pr",
                    "presentation": presentation,
                    "line_start": 1,
                    "line_end": 1,
                    "text": "",
                }
            )
            continue
        lo = max(1, lo)
        hi = min(len(lines), hi)
        out.append(
            {
                "path": fname,
                "role": "primary",
                "source": "changed_in_pr",
                "presentation": presentation,
                "line_start": lo,
                "line_end": hi,
                "text": numbered_text(lines, lo, hi),
            }
        )
    return out


def _resolve_module_path_candidates(repo_path: Path, fragments: tuple[str, ...]) -> list[str]:
    if not fragments:
        return []
    name = fragments[-1]
    parent_chunks = fragments[:-1]
    found = []
    for pref in MODULE_SEARCH_PREFIXES:
        stem = repo_path / pref / Path(*parent_chunks) if parent_chunks else repo_path / pref
        p1 = stem / (name + ".py")
        p2 = stem / name / "__init__.py"
        for pth in (p1, p2):
            try:
                if pth.is_file():
                    found.append(normalize_file_path(str(pth.relative_to(repo_path))))
            except ValueError:
                continue
    return sorted(set(found))[: IMPORT_RESOLVE_MAX * 2]


def import_targets_absolute(repo_path: Path, source: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        tree = ast.parse(source)
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                tops = tuple(al.name.split("."))
                for p in _resolve_module_path_candidates(repo_path, tops):
                    out.append((p, "import"))
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0):
                continue
            if not node.module:
                continue
            tops = tuple(node.module.split("."))
            hits = _resolve_module_path_candidates(repo_path, tops)
            for nm in node.names:
                if nm.name == "*":
                    out.extend([(h, "import_from_star") for h in hits][:IMPORT_RESOLVE_MAX])
                    continue
                suffix = tops + (nm.name,)
                for p in _resolve_module_path_candidates(repo_path, suffix):
                    out.append((p, "import_from"))
    return list(dict.fromkeys(out))[:IMPORT_RESOLVE_MAX]


def same_file_symbols_for_supporting(base_text: str, touched_ln: set[int], max_symbols: int) -> list[tuple[int, int, str, str]]:
    symbols: list[tuple[int, int, str, str]] = []
    try:
        tree = ast.parse(base_text)
    except Exception:
        return symbols
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lo = getattr(node, "lineno", None) or 1
            hi = getattr(node, "end_lineno", None) or lo
            if any(lo <= ln <= hi for ln in touched_ln):
                continue
            st = "class" if isinstance(node, ast.ClassDef) else "function"
            symbols.append((lo, hi, node.name, st))
    symbols.sort(key=lambda z: abs(z[0] - (min(touched_ln) if touched_ln else z[0])))
    return symbols[:max_symbols]


def rg_python_files_matching(repo_path: Path, substring: str, exclude: set[str], limit: int) -> list[str]:
    """Best-effort ripgrep; empty if rg missing or failures."""
    if not substring or not substring.strip():
        return []
    proc = subprocess.run(
        ["rg", "-l", "-F", "-g", "*.py", substring, "."],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        return []
    paths = []
    for line in (proc.stdout or "").splitlines():
        p = normalize_file_path(line.strip())
        if p and p not in exclude:
            paths.append(p)
            if len(paths) >= limit * 10:
                break
    uniq: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
        if len(uniq) >= limit:
            break
    return uniq


def snippet_head_numbered(blob: str, max_lines: int) -> tuple[str, int, int]:
    lines = blob.splitlines()
    if not lines:
        return "", 1, 1
    hi = min(len(lines), max_lines)
    return numbered_text(lines, 1, hi), 1, hi


def build_supporting_windows(
    repo_path: Path,
    base_sha: str,
    primary_paths: set[str],
    selected_py_sources: dict[str, str],
    touched_line_by_file: dict[str, set[int]],
    cfg: Config,
    graph_neighbor_paths: list[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int, int]] = set()

    def append_entry(path: str, source: str, text: str, lo: int, hi: int) -> None:
        if path in primary_paths:
            return
        key = (path, lo, hi)
        if key in seen_keys:
            return
        if len(entries) >= cfg.supporting_top_k_windows:
            return
        seen_keys.add(key)
        entries.append(
            {
                "path": path,
                "role": "supporting",
                "source": source,
                "line_start": lo,
                "line_end": hi,
                "text": text,
            }
        )

    for pth, py_src in selected_py_sources.items():
        touched_ln = touched_line_by_file.get(pth, set())
        for rel, kind in import_targets_absolute(repo_path, py_src):
            if rel in primary_paths:
                continue
            if rel not in cfg.tracked_py_paths:
                continue
            try:
                blob = get_blob_at_sha(repo_path, base_sha, rel)
                body, lo, hi = snippet_head_numbered(blob, GRAPH_SUPPORTING_SNIPPET_LINES)
                append_entry(rel, f"resolved_import::{kind}", body, lo, hi)
            except Exception:
                continue
            if len(entries) >= cfg.supporting_top_k_windows:
                return entries

        try:
            base_full = get_blob_at_sha(repo_path, base_sha, pth)
        except Exception:
            continue
        for slo, shi, sym, stype in same_file_symbols_for_supporting(base_full, touched_ln, SAME_FILE_SUPPORTING_SYMBOLS):
            hi = min(len(base_full.splitlines()), shi + 18)
            lo = max(1, slo - 6)
            lines = base_full.splitlines()
            body = numbered_text(lines, lo, hi)
            append_entry(
                pth,
                f"same_file_ast_symbol::{stype}:{sym}",
                body,
                lo,
                hi,
            )
            if len(entries) >= cfg.supporting_top_k_windows:
                return entries

    if graph_neighbor_paths and cfg.enable_graphrag:
        for rel in graph_neighbor_paths:
            rel_n = normalize_file_path(rel)
            if not rel_n or rel_n in primary_paths:
                continue
            if rel_n not in cfg.tracked_py_paths and not file_allowed_by_extension(
                rel_n, cfg.allowed_extensions
            ):
                continue
            try:
                blob = get_blob_at_sha(repo_path, base_sha, rel_n)
                body, lo, hi = snippet_head_numbered(blob, GRAPH_SUPPORTING_SNIPPET_LINES)
                append_entry(rel_n, "graphrag_co_touch_neighbor", body, lo, hi)
            except Exception:
                continue
            if len(entries) >= cfg.supporting_top_k_windows:
                return entries

    if cfg.enable_callers_rg:
        for pth in sorted(primary_paths):
            if not pth.endswith(".py"):
                continue
            touched_ln = touched_line_by_file.get(pth)
            syms = []
            try:
                base_full = get_blob_at_sha(repo_path, base_sha, pth)
                for slo, shi, sym, _st in parse_python_symbols_for_names(base_full, touched_ln or set()):
                    syms.append(sym)
            except Exception:
                continue
            for sym in syms[:3]:
                excludes_primary = frozenset(primary_paths)
                for hit in rg_python_files_matching(repo_path, sym + "(", excludes_primary, CALLERS_RG_MAX_FILES):
                    if hit in primary_paths:
                        continue
                    if hit not in cfg.tracked_py_paths:
                        continue
                    try:
                        blob = get_blob_at_sha(repo_path, base_sha, hit)
                        body, lo, hi = snippet_head_numbered(blob, GRAPH_SUPPORTING_SNIPPET_LINES)
                        append_entry(hit, f"rg_callee_occurrence::{sym}", body, lo, hi)
                    except Exception:
                        continue
                    if len(entries) >= cfg.supporting_top_k_windows:
                        return entries

    return entries[: cfg.supporting_top_k_windows]


def parse_python_symbols_for_names(base_text: str, touched_ln: set[int]) -> list[tuple[int, int, str, str]]:
    out: list[tuple[int, int, str, str]] = []
    try:
        tree = ast.parse(base_text)
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lo = getattr(node, "lineno", None) or 1
            hi = getattr(node, "end_lineno", None) or lo
            if touched_ln and not any(lo <= ln <= hi for ln in touched_ln):
                continue
            st = "class" if isinstance(node, ast.ClassDef) else "function"
            out.append((lo, hi, node.name, st))
    out.sort(key=lambda z: z[0])
    return out


def nearest_test_snapshots(
    repo_path: Path,
    base_sha: str,
    primary_paths: list[str],
    tracked_tests: frozenset[str],
    changed_tests: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    prim_non_test = [p for p in primary_paths if not is_probable_test_path(p)]
    stems = sorted(
        {
            Path(p).stem.replace("_", "")
            for p in prim_non_test
            if Path(p).stem and len(Path(p).stem.replace("_", "")) >= 3
        }
    )
    dirs = sorted({str(Path(p).parent).replace("\\", "/") for p in prim_non_test})

    ranked: list[tuple[int, str]] = []
    for tp in tracked_tests:
        if tp in used or tp in changed_tests:
            continue
        score = 0
        tpn = tp.replace("\\", "/").lower()
        for d in dirs:
            if d and d in tp:
                score += 3
                break
        for st in stems:
            if len(st) >= 3 and st.lower() in Path(tp).stem.lower():
                score += 2
                break
        if score:
            ranked.append((score, tp))
    ranked.sort(key=lambda z: (-z[0], z[1]))

    def add(rel: str, source: str) -> None:
        nonlocal out
        if rel in used or len(out) >= TEST_CONTEXT_MAX_WINDOWS:
            return
        try:
            blob = get_blob_at_sha(repo_path, base_sha, rel)
            lines_l = blob.splitlines()
            hi = min(len(lines_l), TEST_MAX_LINES_PER_WINDOW)
            if hi < 1:
                return
            out.append(
                {
                    "path": rel,
                    "role": "tests",
                    "source": source,
                    "line_start": 1,
                    "line_end": hi,
                    "text": numbered_text(lines_l, 1, hi),
                }
            )
            used.add(rel)
        except Exception:
            return

    for ct in dict.fromkeys(changed_tests):
        add(ct, "changed_test_file")
        if len(out) >= TEST_CONTEXT_MAX_WINDOWS:
            return out

    for _score, tp in ranked:
        add(tp, "nearest_tracked_test")
        if len(out) >= TEST_CONTEXT_MAX_WINDOWS:
            break

    return out


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def parse_hunk_headers(patch_text: str) -> list[dict[str, int]]:
    headers: list[dict[str, int]] = []
    pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for line in patch_text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2) or 1)
        new_start = int(m.group(3))
        new_count = int(m.group(4) or 1)
        headers.append(
            {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
            }
        )
    return headers


def diff_has_unified_format(unified_diff: str) -> bool:
    return unified_diff.startswith("--- a/") and "\n+++ b/" in unified_diff and "\n@@" in unified_diff


def get_blob_at_sha(repo_path: Path, sha: str, file_path: str) -> str:
    cmd = ["git", "-C", str(repo_path), "show", f"{sha}:{file_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git show failed for {sha}:{file_path}: {proc.stderr.strip()}")
    return proc.stdout


def apply_patch_to_text(base_text: str, patch_text: str) -> str:
    """
    Minimal unified-diff applier for single-file patch text body (starting with @@ lines).
    Returns patched text when apply succeeds, otherwise raises ValueError.
    """
    base_lines = base_text.splitlines(keepends=True)
    out_lines: list[str] = []
    i = 0  # index in base_lines
    patch_lines = patch_text.splitlines(keepends=True)
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    p = 0
    while p < len(patch_lines):
        line = patch_lines[p]
        m = hunk_re.match(line.rstrip("\n"))
        if not m:
            p += 1
            continue
        old_start = int(m.group(1))
        # copy unchanged lines before hunk
        target_i = old_start - 1
        while i < target_i and i < len(base_lines):
            out_lines.append(base_lines[i])
            i += 1
        p += 1
        # process hunk body
        while p < len(patch_lines) and not patch_lines[p].startswith("@@"):
            hl = patch_lines[p]
            if not hl:
                p += 1
                continue
            tag = hl[0]
            content = hl[1:]
            if tag == " ":
                if i >= len(base_lines) or base_lines[i].rstrip("\n") != content.rstrip("\n"):
                    raise ValueError("Context mismatch while applying patch")
                out_lines.append(base_lines[i])
                i += 1
            elif tag == "-":
                if i >= len(base_lines) or base_lines[i].rstrip("\n") != content.rstrip("\n"):
                    raise ValueError("Deletion mismatch while applying patch")
                i += 1
            elif tag == "+":
                out_lines.append(content)
            elif tag == "\\":
                # "\ No newline at end of file" metadata line
                pass
            else:
                raise ValueError(f"Unsupported hunk line prefix: {tag}")
            p += 1
    # append remaining base
    out_lines.extend(base_lines[i:])
    return "".join(out_lines)


def parse_python_symbols(file_text: str) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    try:
        tree = ast.parse(file_text)
    except Exception:
        return symbols
    lines = file_text.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None) or start
            if not start or not end:
                continue
            start = max(1, start)
            end = max(start, end)
            symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"
            code = "\n".join(lines[start - 1 : end])
            symbols.append(
                {
                    "symbol": node.name,
                    "symbol_type": symbol_type,
                    "start_line": start,
                    "end_line": end,
                    "code": code,
                }
            )
    symbols.sort(key=lambda x: (x["start_line"], x["end_line"]))
    return symbols


def touched_lines_from_patch_headers(headers: list[dict[str, int]]) -> set[int]:
    touched: set[int] = set()
    for h in headers:
        start = h["old_start"]
        count = max(1, h["old_count"])
        touched.update(range(start, start + count))
    return touched


def symbol_intersects_touched(symbol: dict[str, Any], touched_lines: set[int]) -> bool:
    return any(symbol["start_line"] <= ln <= symbol["end_line"] for ln in touched_lines)


def build_treesitter_context_for_file(
    repo_path: Path,
    base_sha: str,
    file_path: str,
    patch_text: str,
    context_window: int = 8,
) -> list[dict[str, Any]]:
    if not file_path.endswith(".py"):
        return []
    base_text = get_blob_at_sha(repo_path, base_sha, file_path)
    symbols = parse_python_symbols(base_text)
    touched_headers = parse_hunk_headers(patch_text)
    touched_lines = touched_lines_from_patch_headers(touched_headers)
    lines = base_text.splitlines()

    selected = []
    if symbols:
        for s in symbols:
            touched = symbol_intersects_touched(s, touched_lines)
            if not touched and len(selected) >= 6:
                continue
            start = max(1, s["start_line"] - context_window)
            end = min(len(lines), s["end_line"] + context_window)
            code = "\n".join(lines[start - 1 : end])
            selected.append(
                {
                    "file": file_path,
                    "symbol": s["symbol"],
                    "symbol_type": s["symbol_type"],
                    "start_line": start,
                    "end_line": end,
                    "code": code,
                    "touched_by_gold_patch": touched,
                }
            )
        selected.sort(key=lambda x: (not x["touched_by_gold_patch"], x["start_line"]))
        if selected:
            return selected[:8]

    # Fallback: hunk-centered windows when AST symbols are unavailable.
    fallback = []
    for idx, h in enumerate(touched_headers[:6], start=1):
        start = max(1, h["old_start"] - context_window)
        end = min(len(lines), h["old_start"] + max(h["old_count"], 1) + context_window)
        code = "\n".join(lines[start - 1 : end]) if lines else patch_text
        fallback.append(
            {
                "file": file_path,
                "symbol": f"hunk_{idx}",
                "symbol_type": "fallback_hunk_window",
                "start_line": start,
                "end_line": end,
                "code": code,
                "touched_by_gold_patch": True,
            }
        )
    return fallback


def connect_neo4j(cfg: Config):
    if not cfg.enable_graphrag:
        return None
    if GraphDatabase is None:
        raise RuntimeError("neo4j package is not installed but GraphRAG is enabled")
    return GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))


def graphrag_for_files(session, file_paths: list[str], top_k: int) -> dict[str, Any]:
    """
    Equivalent retrieval function for patcher context:
      - candidate files by co-touch frequency
      - historical idiom snippets from PR bodies
      - optional CI stats
    """
    if session is None:
        return {
            "enabled": False,
            "query_version": "disabled",
            "candidate_files_topk": [],
            "historical_idioms": [],
            "historical_ci_stats": {},
        }
    candidate_query = """
    UNWIND $files AS fname
    MATCH (f:File {filename: fname})<-[:TOUCHES]-(p:PR)-[:TOUCHES]->(cand:File)
    WHERE cand.filename <> fname
    RETURN cand.filename AS file, count(*) AS score
    ORDER BY score DESC
    LIMIT $top_k
    """
    idiom_query = """
    UNWIND $files AS fname
    MATCH (p:PR)-[:TOUCHES]->(f:File {filename: fname})
    WHERE p.body_truncated IS NOT NULL
    RETURN f.filename AS file, p.number AS source_pr, p.body_truncated AS snippet
    LIMIT 60
    """
    # Avoid property-reference warnings on graphs without ci_conclusion.
    ci_query = """
    UNWIND $files AS fname
    MATCH (p:PR)-[:TOUCHES]->(f:File {filename: fname})
    RETURN f.filename AS file, count(*) AS total
    """
    candidate_rows = session.run(candidate_query, files=file_paths, top_k=top_k).data()
    idiom_rows = session.run(idiom_query, files=file_paths).data()
    ci_rows = session.run(ci_query, files=file_paths).data()
    return {
        "enabled": True,
        "query_version": "v1_file_neighbors",
        "candidate_files_topk": [
            {"file": r["file"], "score": float(r["score"])} for r in candidate_rows
        ],
        "historical_idioms": [
            {
                "file": r["file"],
                "source_pr": r.get("source_pr"),
                "snippet": (r.get("snippet") or "")[:220],
            }
            for r in idiom_rows[:20]
        ],
        "historical_ci_stats": {
            r["file"]: {
                "total": int(r["total"]),
                "ci_pass_rate": None,
            }
            for r in ci_rows
        },
    }


def stratified_pr_split(
    records: list[dict[str, Any]], cfg: Config
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(cfg.seed)

    def bucket(rec: dict[str, Any]) -> str:
        fc = rec["meta"]["selected_files_touched"]
        pt = rec["meta"]["selected_total_patch_tokens"]
        file_bucket = "f1" if fc == 1 else ("f2" if fc == 2 else "f3")
        if pt < 250:
            patch_bucket = "p_small"
        elif pt < 700:
            patch_bucket = "p_med"
        else:
            patch_bucket = "p_large"
        return f"{file_bucket}_{patch_bucket}"

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        groups[bucket(r)].append(r)

    train, eval_, test = [], [], []
    for _, grp in groups.items():
        rng.shuffle(grp)
        n = len(grp)
        n_train = int(round(n * cfg.split_train))
        n_eval = int(round(n * cfg.split_eval))
        if n_train + n_eval > n:
            n_eval = max(0, n - n_train)
        n_test = n - n_train - n_eval
        train.extend(grp[:n_train])
        eval_.extend(grp[n_train : n_train + n_eval])
        test.extend(grp[n_train + n_eval : n_train + n_eval + n_test])

    rng.shuffle(train)
    rng.shuffle(eval_)
    rng.shuffle(test)
    return train, eval_, test


def validate_record(record: dict[str, Any], id_set: set[str]) -> list[str]:
    errs = []
    rid = record.get("id")
    if not rid:
        errs.append("missing_id")
    elif rid in id_set:
        errs.append("duplicate_id")
    else:
        id_set.add(rid)

    meta = record.get("meta", {})
    if not meta.get("base_sha") or not meta.get("head_sha"):
        errs.append("missing_sha")

    out = record.get("output", {})
    diff = out.get("unified_diff", "")
    if not diff_has_unified_format(diff):
        errs.append("invalid_unified_diff")

    inp = record.get("input", {})
    allowed_edits = inp.get("allowed_edit_files") or inp.get("constraints", {}).get("allowed_files") or []
    allowed = set(allowed_edits)
    touched = set(out.get("touched_files", []))
    if not allowed:
        errs.append("missing_allowed_edit_files")
    elif not touched.issubset(allowed):
        errs.append("touched_outside_allowed")

    # JSON serializable / parseable.
    try:
        _ = json.dumps(record)
    except Exception:
        errs.append("non_serializable")
    return errs


def report_distribution(values: list[int]) -> dict[str, float]:
    if not values:
        return {"count": 0, "min": 0, "p50": 0, "p90": 0, "max": 0}
    vals = sorted(values)
    return {
        "count": len(vals),
        "min": int(vals[0]),
        "p50": int(vals[int(0.50 * (len(vals) - 1))]),
        "p90": int(vals[int(0.90 * (len(vals) - 1))]),
        "max": int(vals[-1]),
    }


def build_record(
    row: pd.Series,
    cfg: Config,
    neo4j_session,
    drop_counts: Counter,
) -> dict[str, Any] | None:
    repo = str(row["repo"])
    pr_number = int(row["pr_number"])
    base_sha = str(row["base_sha"]).strip()
    head_sha = str(row["head_sha"]).strip()
    files = parse_json_field(row["files_json"], [])
    if not isinstance(files, list) or not files:
        drop_counts["no_files_json"] += 1
        return None

    py_files: list[dict[str, Any]] = []
    selected_files: list[dict[str, Any]] = []
    diff_blocks: list[str] = []
    touched_files: list[str] = []
    all_hunk_headers: list[dict[str, int]] = []
    ts_spans: list[dict[str, Any]] = []
    span_stats = {"ast_spans": 0, "fallback_spans": 0}
    file_statuses: dict[str, str] = {}

    for f in files:
        fname = normalize_file_path(f.get("filename", ""))
        patch = f.get("patch")
        status = str(f.get("status", "modified"))
        file_statuses[fname] = status
        if not fname or not patch:
            continue
        if not file_allowed_by_extension(fname, cfg.allowed_extensions):
            continue
        if patch.startswith("Binary files"):
            continue
        touched_files.append(fname)
        selected_files.append(f)
        if fname.endswith(".py"):
            py_files.append(f)
        diff_blocks.append(f"--- a/{fname}\n+++ b/{fname}\n{patch}")
        all_hunk_headers.extend(parse_hunk_headers(patch))
        if cfg.enable_treesitter:
            try:
                spans_for_file = build_treesitter_context_for_file(
                    cfg.repo_path,
                    base_sha,
                    fname,
                    patch,
                )
                ts_spans.extend(spans_for_file)
                span_stats["ast_spans"] += sum(
                    1 for s in spans_for_file if s.get("symbol_type") != "fallback_hunk_window"
                )
                span_stats["fallback_spans"] += sum(
                    1 for s in spans_for_file if s.get("symbol_type") == "fallback_hunk_window"
                )
            except RuntimeError:
                drop_counts["treesitter_blob_lookup_failures"] += 1
                headers = parse_hunk_headers(patch)
                if headers:
                    h = headers[0]
                    ts_spans.append(
                        {
                            "file": fname,
                            "symbol": "hunk_1",
                            "symbol_type": "fallback_hunk_window",
                            "start_line": h["old_start"],
                            "end_line": h["old_start"] + max(h["old_count"], 1),
                            "code": patch[:1200],
                            "touched_by_gold_patch": True,
                        }
                    )
                    span_stats["fallback_spans"] += 1
                continue
            except Exception:
                drop_counts["treesitter_parse_failures"] += 1
                continue

    if not selected_files or not diff_blocks:
        drop_counts["no_allowed_patch"] += 1
        return None

    selected_files_touched = len(selected_files)
    if cfg.single_file_only and selected_files_touched != 1:
        drop_counts["not_single_file_mode"] += 1
        return None
    if selected_files_touched > cfg.max_files_touched:
        drop_counts["too_many_files"] += 1
        return None

    selected_additions = int(sum(int(f.get("additions", 0) or 0) for f in selected_files))
    selected_deletions = int(sum(int(f.get("deletions", 0) or 0) for f in selected_files))
    selected_patch_tokens = int(
        sum(len((f.get("patch") or "").split()) for f in selected_files if f.get("patch"))
    )
    if selected_additions > cfg.max_additions:
        drop_counts["too_many_additions"] += 1
        return None
    if selected_patch_tokens > cfg.max_patch_tokens:
        drop_counts["too_many_patch_tokens"] += 1
        return None

    unified_diff = "\n".join(diff_blocks)
    if not diff_has_unified_format(unified_diff):
        drop_counts["invalid_unified_diff"] += 1
        return None

    allowed_edit_files = list(dict.fromkeys(touched_files))
    primary_paths_set = set(touched_files)
    touched_line_by_file: dict[str, set[int]] = {}
    for sf in selected_files:
        fp = normalize_file_path(sf.get("filename", ""))
        patch_t = sf.get("patch") or ""
        touched_line_by_file[fp] = touched_lines_from_patch_headers(parse_hunk_headers(patch_t))

    selected_py_sources: dict[str, str] = {}
    for sf in selected_files:
        fp = normalize_file_path(sf.get("filename", ""))
        if fp.endswith(".py"):
            try:
                selected_py_sources[fp] = get_blob_at_sha(cfg.repo_path, base_sha, fp)
            except Exception:
                continue

    file_context_primary: list[dict[str, Any]] = []
    for sf in selected_files:
        fp = normalize_file_path(sf.get("filename", ""))
        pt = sf.get("patch") or ""
        try:
            file_context_primary.extend(
                build_primary_windows(cfg.repo_path, base_sha, fp, pt, cfg)
            )
        except RuntimeError:
            drop_counts["primary_context_blob_miss"] += 1
            if pt:
                h = parse_hunk_headers(pt)
                ln0 = int(h[0]["old_start"]) if h else 1
                file_context_primary.append(
                    {
                        "path": fp,
                        "role": "primary",
                        "source": "changed_in_pr",
                        "presentation": "fallback_patch_only",
                        "line_start": ln0,
                        "line_end": ln0 + 40,
                        "text": numbered_text(pt.splitlines(), 1, min(40, max(1, len(pt.splitlines())))),
                    }
                )

    if cfg.enable_graphrag and neo4j_session is not None:
        graphrag_context = graphrag_for_files(
            neo4j_session,
            allowed_edit_files,
            cfg.graphrag_top_k,
        )
        graphrag_context["query_version"] = cfg.graphrag_query_version
        graph_neighbor_paths = [
            normalize_file_path(str(x.get("file", ""))) for x in graphrag_context.get("candidate_files_topk") or []
        ]
    else:
        graphrag_context = {
            "enabled": False,
            "query_version": cfg.graphrag_query_version,
            "candidate_files_topk": [],
            "historical_idioms": [],
            "historical_ci_stats": {},
        }
        graph_neighbor_paths = []

    file_context_supporting = build_supporting_windows(
        cfg.repo_path,
        base_sha,
        primary_paths_set,
        selected_py_sources,
        touched_line_by_file,
        cfg,
        graph_neighbor_paths=graph_neighbor_paths,
    )

    changed_tests_paths = [p for p in touched_files if is_probable_test_path(p)]
    tracked_test_paths = frozenset(p for p in cfg.tracked_py_paths if is_probable_test_path(p))
    file_context_tests = nearest_test_snapshots(
        cfg.repo_path,
        base_sha,
        touched_files,
        tracked_test_paths,
        changed_tests_paths,
    )

    pr_title_txt = str(row.get("pr_title", "") or "").strip()
    pr_body_txt = str(row.get("pr_body", "") or "").strip()
    issue_title_txt = str(row.get(cfg.issue_title_col, "") or "").strip()
    issue_body_txt = str(row.get(cfg.issue_body_col, "") or "").strip()

    planner_reason_parts: list[str] = []
    if issue_title_txt or issue_body_txt:
        planner_reason_parts.append(
            f"[issue_context] {(issue_title_txt + ' · ' + issue_body_txt)[:1500]}".strip()
        )
    if pr_title_txt or pr_body_txt:
        planner_reason_parts.append(
            f"[pr_metadata] {(pr_title_txt + ' · ' + pr_body_txt)[:1500]}".strip()
        )
    if not planner_reason_parts:
        planner_reason_parts.append("Historical merged PR reconstruction (minimal issue metadata present).")

    plan_entries = [
        {
            "file": normalize_file_path(f.get("filename", "")),
            "what_to_change": (
                f"Implement the validated fix touching this path (merged PR #{pr_number}); "
                f"mirror intent from PR/issue narrative without unrelated refactors."
            )[:580],
        }
        for f in selected_files
    ]

    planner_directive = {
        "requires_code_change": "YES",
        "reason": (" ".join(planner_reason_parts))[:2400],
        "confidence": "MEDIUM",
        "proxy_source": "issue_pr_metadata_reconstruction",
        "plan": plan_entries,
        "scope_target_files": allowed_edit_files,
        "signals": {
            "target_symbols_touched_estimate": [
                s["symbol"] for s in ts_spans if s.get("touched_by_gold_patch")
            ][:16],
            "recommended_test_touch": changed_tests_paths[:8],
            "test_nearby_budget": TEST_CONTEXT_MAX_WINDOWS,
        },
    }

    issue_context = {
        "title": issue_title_txt[:200] if issue_title_txt else pr_title_txt[:200],
        "body": (issue_body_txt or pr_body_txt)[:1500],
        "labels": parse_labels(row.get(cfg.issue_labels_col, "")),
        "comments_summary": str(row.get(cfg.issue_comments_col, ""))[:1000],
    }

    constraints = {
        "allowed_files": allowed_edit_files,
        "forbid_new_files": True,
        "forbid_unrelated_refactors": True,
        "output_format": "unified_diff_only",
    }

    valid_diff = diff_has_unified_format(unified_diff)
    touches_only_allowed = set(allowed_edit_files).issuperset(set(touched_files))
    structural_pass = True
    compile_pass = True
    for f in py_files:
        fp = normalize_file_path(f.get("filename", ""))
        try:
            base_text = get_blob_at_sha(cfg.repo_path, base_sha, fp)
            patched = apply_patch_to_text(base_text, f.get("patch", ""))
            ast.parse(patched)
        except Exception:
            structural_pass = False
            compile_pass = False
            break

    output = {
        "unified_diff": unified_diff,
        "touched_files": touched_files,
        "gold_hunk_headers": [
            f"@@ -{h['old_start']},{h['old_count']} +{h['new_start']},{h['new_count']} @@"
            for h in all_hunk_headers
        ],
    }

    instruction = (
        "You simulate the inference-time patcher. Read planner_directive (scope-only) "
        "and file_contexts (primary changed files, deterministic supporting snippets, nearest tests). "
        "Generate a minimal unified diff that satisfies the planner scope; edits only paths in "
        "allowed_edit_files. Output ONLY unified diff text."
    )

    record = {
        "id": f"{repo}#pr{pr_number}",
        "split": None,
        "repo": repo,
        "task_type": "patch_generation",
        "input": {
            "instruction": instruction,
            "planner_directive": planner_directive,
            "issue_context": issue_context,
            "file_contexts": {
                "primary": file_context_primary,
                "supporting": file_context_supporting,
                "tests": file_context_tests,
            },
            "allowed_edit_files": allowed_edit_files,
            "treesitter_context": {
                "language": "python",
                "base_sha": base_sha,
                "spans": ts_spans if cfg.enable_treesitter else [],
            },
            "graphrag_context": graphrag_context,
            "constraints": constraints,
        },
        "output": output,
        "meta": {
            "pr_number": pr_number,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "file_statuses": file_statuses,
            "selected_files_touched": selected_files_touched,
            "selected_total_additions": selected_additions,
            "selected_total_deletions": selected_deletions,
            "selected_total_patch_tokens": selected_patch_tokens,
            "py_files_touched": selected_files_touched,
            "py_total_additions": selected_additions,
            "py_total_deletions": selected_deletions,
            "py_total_patch_tokens": selected_patch_tokens,
            "file_context_shapes": {
                "primary_segments": len(file_context_primary),
                "supporting_segments": len(file_context_supporting),
                "tests_segments": len(file_context_tests),
            },
            "quality_labels": {
                "valid_diff": valid_diff,
                "touches_only_allowed_files": touches_only_allowed,
                "compile_pass": compile_pass,
                "structural_pass": structural_pass,
            },
            "data_provenance": {
                "source_dataset": str(cfg.input_path),
                "repo_path": str(cfg.repo_path),
                "graphrag_enabled": cfg.enable_graphrag,
                "treesitter_legacy_spans_enabled": cfg.enable_treesitter,
                "seed": cfg.seed,
                "ast_span_count": span_stats["ast_spans"],
                "fallback_span_count": span_stats["fallback_spans"],
                "context_builder": "historical_orchestrator_proxy_v1",
            },
        },
    }

    seq_est = approx_tokens(json.dumps(record["input"])) + approx_tokens(unified_diff)
    if seq_est > cfg.max_seq_tokens:
        drop_counts["over_max_sequence_budget"] += 1
        return None
    return record


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    setup_logging()
    tracked_py = git_list_tracked_paths(REPO_ROOT)
    cfg = build_default_config(tracked_py)
    random.seed(cfg.seed)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)

    LOG.info("Loading input dataset: %s", cfg.input_path)
    df = load_input_dataframe(cfg.input_path)
    fail_fast_checks(cfg, df)
    LOG.info("Rows loaded: %d", len(df))

    # Fast prefilter stage (cheap operations only) to reduce runtime substantially.
    pre_n = len(df)
    df = df[df["total_additions"].fillna(0).astype(int) <= cfg.max_additions]
    df = df[df["total_patch_tokens"].fillna(0).astype(int) <= cfg.max_patch_tokens]
    if cfg.single_file_only:
        # Keep rows where files_json likely has exactly one file marker.
        # Exact enforcement still happens later in strict build_record filtering.
        df = df[df["files_json"].astype(str).str.count('"filename"') <= 1]
    else:
        df = df[df["files_json"].astype(str).str.count('"filename"') <= cfg.max_files_touched]
    df = df[df["files_json"].apply(lambda v: likely_has_allowed_patch(v, cfg.allowed_extensions))]
    post_n = len(df)
    LOG.info(
        "Prefiltered rows for expensive stage: %d -> %d (dropped %d, %.1f%%)",
        pre_n,
        post_n,
        pre_n - post_n,
        (100.0 * (pre_n - post_n) / pre_n) if pre_n else 0.0,
    )

    neo4j_driver = connect_neo4j(cfg)
    neo4j_session = neo4j_driver.session() if neo4j_driver else None

    kept: list[dict[str, Any]] = []
    drop_counts: Counter = Counter()
    id_set: set[str] = set()

    try:
        for _, row in tqdm(df.iterrows(), total=len(df), desc="Building patcher examples"):
            rec = build_record(row, cfg, neo4j_session, drop_counts)
            if rec is None:
                continue
            errs = validate_record(rec, id_set)
            if errs:
                for e in errs:
                    drop_counts[e] += 1
                continue
            kept.append(rec)
    finally:
        if neo4j_session is not None:
            neo4j_session.close()
        if neo4j_driver is not None:
            neo4j_driver.close()

    LOG.info("Kept records: %d", len(kept))
    LOG.info("Dropped records: %d", sum(drop_counts.values()))

    train, eval_, test = stratified_pr_split(kept, cfg)
    if cfg.max_train_examples is not None and len(train) > cfg.max_train_examples:
        rng_sub = random.Random(cfg.seed + 4133)
        rng_sub.shuffle(train)
        train = train[: cfg.max_train_examples]
        LOG.info(
            "Capped train set to max_train_examples=%d (%d rows kept)",
            cfg.max_train_examples,
            len(train),
        )
    for r in train:
        r["split"] = "train"
    for r in eval_:
        r["split"] = "eval"
    for r in test:
        r["split"] = "test"

    train_path = cfg.out_dir / "patcher_train.jsonl"
    eval_path = cfg.out_dir / "patcher_eval.jsonl"
    test_path = cfg.out_dir / "patcher_test.jsonl"
    report_path = cfg.out_dir / "dataset_report.json"

    write_jsonl(train_path, train)
    write_jsonl(eval_path, eval_)
    write_jsonl(test_path, test)

    files_touched = [r["meta"]["selected_files_touched"] for r in kept]
    additions = [r["meta"]["selected_total_additions"] for r in kept]
    patch_tokens = [r["meta"]["selected_total_patch_tokens"] for r in kept]
    qlabels = Counter()
    for r in kept:
        for k, v in r["meta"]["quality_labels"].items():
            qlabels[f"{k}_pass" if v else f"{k}_fail"] += 1

    report = {
        "total_input_rows": int(len(df)),
        "total_kept": len(kept),
        "total_dropped": int(sum(drop_counts.values())),
        "dropped_by_reason": dict(drop_counts),
        "split_sizes": {"train": len(train), "eval": len(eval_), "test": len(test)},
        "distributions": {
            "files_touched": report_distribution(files_touched),
            "additions": report_distribution(additions),
            "patch_tokens": report_distribution(patch_tokens),
        },
        "percent_single_file_rows": (
            100.0 * sum(1 for r in kept if r["meta"]["py_files_touched"] == 1) / len(kept)
            if kept
            else 0.0
        ),
        "percent_passing_quality_labels": {
            "valid_diff": (
                100.0
                * sum(1 for r in kept if r["meta"]["quality_labels"]["valid_diff"])
                / len(kept)
                if kept
                else 0.0
            ),
            "touches_only_allowed_files": (
                100.0
                * sum(1 for r in kept if r["meta"]["quality_labels"]["touches_only_allowed_files"])
                / len(kept)
                if kept
                else 0.0
            ),
            "compile_pass": (
                100.0
                * sum(1 for r in kept if r["meta"]["quality_labels"]["compile_pass"])
                / len(kept)
                if kept
                else 0.0
            ),
            "structural_pass": (
                100.0
                * sum(1 for r in kept if r["meta"]["quality_labels"]["structural_pass"])
                / len(kept)
                if kept
                else 0.0
            ),
        },
        "query_reproducibility": {
            "graphrag_enabled": cfg.enable_graphrag,
            "graphrag_query_version": cfg.graphrag_query_version,
            "graphrag_top_k": cfg.graphrag_top_k,
            "treesitter_enabled": cfg.enable_treesitter,
            "seed": cfg.seed,
            "max_train_examples": cfg.max_train_examples,
        },
        "span_enrichment": {
            "rows_with_any_span": sum(1 for r in kept if r["input"]["treesitter_context"]["spans"]),
            "rows_with_fallback_span": sum(
                1
                for r in kept
                if any(s.get("symbol_type") == "fallback_hunk_window" for s in r["input"]["treesitter_context"]["spans"])
            ),
            "rows_with_non_python_touched_files": sum(
                1
                for r in kept
                if any(not str(p).lower().endswith(".py") for p in r["output"]["touched_files"])
            ),
            "rows_with_non_python_and_any_span": sum(
                1
                for r in kept
                if any(not str(p).lower().endswith(".py") for p in r["output"]["touched_files"])
                and bool(r["input"]["treesitter_context"]["spans"])
            ),
            "total_ast_spans": sum(
                r["meta"]["data_provenance"].get("ast_span_count", 0) for r in kept
            ),
            "total_fallback_spans": sum(
                r["meta"]["data_provenance"].get("fallback_span_count", 0) for r in kept
            ),
        },
        "outputs": {
            "train_jsonl": str(train_path),
            "eval_jsonl": str(eval_path),
            "test_jsonl": str(test_path),
            "report_json": str(report_path),
        },
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    LOG.info("Wrote: %s", train_path)
    LOG.info("Wrote: %s", eval_path)
    LOG.info("Wrote: %s", test_path)
    LOG.info("Wrote: %s", report_path)

    # Built-in end-of-run stats + sanity preview.
    print("\n=== PATCHER DATASET REPORT SUMMARY ===")
    print(f"Input rows: {report['total_input_rows']}")
    print(f"Kept rows: {report['total_kept']}")
    print(f"Dropped rows: {report['total_dropped']}")
    print(f"Split sizes: {report['split_sizes']}")
    print(f"Percent single-file rows: {report['percent_single_file_rows']:.2f}%")
    print("Quality pass rates (%):")
    for k, v in report["percent_passing_quality_labels"].items():
        print(f"  - {k}: {v:.2f}")

    if report["dropped_by_reason"]:
        print("Dropped by reason:")
        for reason, cnt in sorted(report["dropped_by_reason"].items(), key=lambda x: (-x[1], x[0])):
            print(f"  - {reason}: {cnt}")

    print("\nDistributions:")
    for name, d in report["distributions"].items():
        print(
            f"  - {name}: n={d['count']} min={d['min']} p50={d['p50']} p90={d['p90']} max={d['max']}"
        )
    print("\nSpan enrichment:")
    se = report["span_enrichment"]
    print(f"  - rows_with_any_span: {se['rows_with_any_span']}")
    print(f"  - rows_with_fallback_span: {se['rows_with_fallback_span']}")
    print(f"  - rows_with_non_python_touched_files: {se['rows_with_non_python_touched_files']}")
    print(f"  - rows_with_non_python_and_any_span: {se['rows_with_non_python_and_any_span']}")
    print(f"  - total_ast_spans: {se['total_ast_spans']}")
    print(f"  - total_fallback_spans: {se['total_fallback_spans']}")

    print("\n=== SANITY SAMPLE (first 3 train rows) ===")
    for row in train[:3]:
        print(
            f"id={row['id']} split={row['split']} files={len(row['output']['touched_files'])} "
            f"spans={len(row['input']['treesitter_context']['spans'])} "
            f"fc_sup={len(row['input'].get('file_contexts', {}).get('supporting') or [])} "
            f"fc_tst={len(row['input'].get('file_contexts', {}).get('tests') or [])} "
            f"gr_candidates={len(row['input']['graphrag_context']['candidate_files_topk'])}"
        )
        print(
            f"  quality={row['meta']['quality_labels']} "
            f"patch_tokens={row['meta']['py_total_patch_tokens']}"
        )

    print("\nArtifacts:")
    print(f"  - {train_path}")
    print(f"  - {eval_path}")
    print(f"  - {test_path}")
    print(f"  - {report_path}")

    # Dotfile path sanity check for normalize_file_path fix.
    eval_text = eval_path.read_text(encoding="utf-8")
    bad_dotfile_ref = "a/pre-commit-config.yaml"
    good_dotfile_ref = "a/.pre-commit-config.yaml"
    bad_count = eval_text.count(bad_dotfile_ref)
    good_count = eval_text.count(good_dotfile_ref)
    print("\nDotfile path sanity (eval split):")
    print(f"  - bad_ref_count ('{bad_dotfile_ref}'): {bad_count}")
    print(f"  - good_ref_count ('{good_dotfile_ref}'): {good_count}")
    print("  - status: " + ("PASS" if bad_count == 0 else "WARN (unexpected bad dotfile refs found)"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.error("Build failed: %s", exc)
        raise
