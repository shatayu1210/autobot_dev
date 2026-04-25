"""
Build production-grade patcher dataset with Tree-sitter + GraphRAG enrichment.

Outputs:
  - patcher_train.jsonl
  - patcher_eval.jsonl
  - patcher_test.jsonl
  - dataset_report.json
"""

from __future__ import annotations

import argparse
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


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Build strict patcher dataset JSONL splits with Tree-sitter and GraphRAG context."
    )
    script_dir = Path(__file__).resolve().parent
    parser.add_argument(
        "--input-path",
        type=Path,
        required=True,
        help="PR dataset path (.csv or .jsonl).",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        required=True,
        help="Local git repo path for base/head blob extraction by SHA.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=script_dir / "outputs",
        help="Output directory for split JSONLs and dataset_report.json.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-files-touched", type=int, default=3)
    parser.add_argument("--single-file-only", action="store_true")
    parser.add_argument("--max-additions", type=int, default=350)
    parser.add_argument("--max-patch-tokens", type=int, default=1200)
    parser.add_argument("--max-seq-tokens", type=int, default=4096)
    parser.add_argument("--split-train", type=float, default=0.8)
    parser.add_argument("--split-eval", type=float, default=0.1)
    parser.add_argument("--split-test", type=float, default=0.1)
    parser.add_argument("--disable-treesitter", action="store_true")
    parser.add_argument("--disable-graphrag", action="store_true")
    parser.add_argument("--graphrag-top-k", type=int, default=6)
    parser.add_argument("--graphrag-query-version", type=str, default="v1_file_neighbors")
    parser.add_argument("--neo4j-uri", type=str, default="bolt://localhost:7687")
    parser.add_argument("--neo4j-user", type=str, default="neo4j")
    parser.add_argument("--neo4j-password", type=str, default="autobot_password")

    # Optional issue context columns.
    parser.add_argument("--issue-title-col", type=str, default="issue_title")
    parser.add_argument("--issue-body-col", type=str, default="issue_body")
    parser.add_argument("--issue-labels-col", type=str, default="issue_labels")
    parser.add_argument("--issue-comments-col", type=str, default="comments_summary")
    parser.add_argument(
        "--allowed-extensions",
        type=str,
        default=".py,.ts,.tsx,.js,.jsx,.sql,.yaml,.yml,.json,.md,.rst,.toml,.ini,.cfg,.sh,.dockerfile",
        help="Comma-separated list of file extensions to keep (lowercase, include dot).",
    )

    args = parser.parse_args()
    if round(args.split_train + args.split_eval + args.split_test, 6) != 1.0:
        raise ValueError("Split ratios must sum to 1.0")

    return Config(
        input_path=args.input_path,
        repo_path=args.repo_path,
        out_dir=args.out_dir,
        seed=args.seed,
        max_files_touched=args.max_files_touched,
        single_file_only=args.single_file_only,
        max_additions=args.max_additions,
        max_patch_tokens=args.max_patch_tokens,
        max_seq_tokens=args.max_seq_tokens,
        split_train=args.split_train,
        split_eval=args.split_eval,
        split_test=args.split_test,
        enable_treesitter=not args.disable_treesitter,
        enable_graphrag=not args.disable_graphrag,
        graphrag_top_k=args.graphrag_top_k,
        graphrag_query_version=args.graphrag_query_version,
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        neo4j_password=args.neo4j_password,
        issue_title_col=args.issue_title_col,
        issue_body_col=args.issue_body_col,
        issue_labels_col=args.issue_labels_col,
        issue_comments_col=args.issue_comments_col,
        allowed_extensions={x.strip().lower() for x in args.allowed_extensions.split(",") if x.strip()},
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

    allowed = set(record.get("input", {}).get("constraints", {}).get("allowed_files", []))
    touched = set(out.get("touched_files", []))
    if not touched.issubset(allowed):
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

    py_files = []
    selected_files = []
    diff_blocks = []
    touched_files = []
    all_hunk_headers = []
    ts_spans = []
    span_stats = {"ast_spans": 0, "fallback_spans": 0}
    file_statuses = {}

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
                span_stats["ast_spans"] += sum(1 for s in spans_for_file if s.get("symbol_type") != "fallback_hunk_window")
                span_stats["fallback_spans"] += sum(1 for s in spans_for_file if s.get("symbol_type") == "fallback_hunk_window")
            except RuntimeError:
                # Do not kill full build for occasional historical/rename mismatches.
                drop_counts["treesitter_blob_lookup_failures"] += 1
                # Last-resort fallback from patch hunk headers without base blob text.
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

    # Recompute totals on selected files only for precision.
    selected_additions = int(sum(int(f.get("additions", 0) or 0) for f in selected_files))
    selected_deletions = int(sum(int(f.get("deletions", 0) or 0) for f in selected_files))
    selected_patch_tokens = int(sum(len((f.get("patch") or "").split()) for f in selected_files if f.get("patch")))
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

    planner_directive = {
        "requires_code_change": "YES",
        "confidence": "MEDIUM",
        "reason": "Issue requires code modifications in target files.",
        "root_cause": (str(row.get("pr_body", "")) or str(row.get("pr_title", "")) or "Unknown root cause.")[:220],
        "target_files": [normalize_file_path(f.get("filename", "")) for f in selected_files],
        "target_functions": [s["symbol"] for s in ts_spans if s.get("touched_by_gold_patch")][:8],
        "test_strategy": "Update/add focused unit tests for modified behavior.",
    }

    issue_context = {
        "title": str(row.get(cfg.issue_title_col, ""))[:200],
        "body": str(row.get(cfg.issue_body_col, ""))[:1200],
        "labels": parse_labels(row.get(cfg.issue_labels_col, "")),
        "comments_summary": str(row.get(cfg.issue_comments_col, ""))[:1000],
    }

    if cfg.enable_graphrag:
        graphrag_context = graphrag_for_files(
            neo4j_session,
            planner_directive["target_files"],
            cfg.graphrag_top_k,
        )
        graphrag_context["query_version"] = cfg.graphrag_query_version
    else:
        graphrag_context = {
            "enabled": False,
            "query_version": cfg.graphrag_query_version,
            "candidate_files_topk": [],
            "historical_idioms": [],
            "historical_ci_stats": {},
        }

    constraints = {
        "allowed_files": planner_directive["target_files"],
        "forbid_new_files": True,
        "forbid_unrelated_refactors": True,
        "output_format": "unified_diff_only",
    }

    # Quality labels
    valid_diff = diff_has_unified_format(unified_diff)
    touches_only_allowed = set(planner_directive["target_files"]).issuperset(set(touched_files))
    structural_pass = True
    compile_pass = True
    # Syntax-level check from applying patch against base blobs.
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
        "Generate a minimal unified diff that implements the planner directive exactly. "
        "Edit only allowed files, do not create new files, and avoid unrelated refactors. "
        "Output ONLY unified diff text."
    )

    record = {
        "id": f"{repo}#pr{pr_number}",
        "split": None,  # assigned later
        "repo": repo,
        "task_type": "patch_generation",
        "input": {
            "instruction": instruction,
            "planner_directive": planner_directive,
            "issue_context": issue_context,
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
            # Backward-compatible aliases retained for existing dashboards/scripts.
            "py_files_touched": selected_files_touched,
            "py_total_additions": selected_additions,
            "py_total_deletions": selected_deletions,
            "py_total_patch_tokens": selected_patch_tokens,
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
                "treesitter_enabled": cfg.enable_treesitter,
                "seed": cfg.seed,
                "ast_span_count": span_stats["ast_spans"],
                "fallback_span_count": span_stats["fallback_spans"],
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
    cfg = parse_args()
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
