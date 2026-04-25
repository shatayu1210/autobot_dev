"""
AutoBot — Tree-sitter Symbol Index Builder
============================================
Run this ONCE on your local Airflow clone before Colab training.

What it does:
  - Walks all Python files in the cloned Airflow repo
  - Parses each file with Tree-sitter to extract classes and functions
  - Builds a compact JSON index: { "file_path": { "classes": [...], "functions": [...] } }
  - Saves to treesitter_index.json (upload this to Colab)

At training time the notebook reads this file and for each training example
extracts only the relevant subset of entries (matching keywords from issue text).
That subset is typically 300-600 tokens, well within the 2048 token budget.

At production/inference time the VSCode plugin runs Tree-sitter live on the
cloned repo — same index structure, exact accuracy on current HEAD.

Usage:
    pip install tree-sitter tree-sitter-python
    python build_treesitter_index.py --repo /path/to/airflow --output treesitter_index.json

Requirements:
    pip install tree-sitter>=0.21.0 tree-sitter-python
"""

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ============================================================================
# TREE-SITTER SETUP
# ============================================================================

def build_parser():
    """Build and return a Tree-sitter Python parser."""
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser

        PY_LANGUAGE = Language(tspython.language())
        parser = Parser(PY_LANGUAGE)
        return parser
    except ImportError:
        raise ImportError(
            "Install tree-sitter dependencies:\n"
            "  pip install tree-sitter>=0.21.0 tree-sitter-python"
        )


# ============================================================================
# SYMBOL EXTRACTION
# ============================================================================

def extract_symbols_python(file_path: Path, parser) -> dict:
    """
    Parse a Python file and extract top-level class and function names.
    Returns dict with keys 'classes' and 'functions'.
    """
    try:
        source = file_path.read_bytes()
    except Exception:
        return {"classes": [], "functions": []}

    try:
        tree = parser.parse(source)
    except Exception:
        return {"classes": [], "functions": []}

    classes = []
    functions = []

    def walk(node, depth=0):
        """Walk AST nodes and extract class/function names at any depth."""
        if node.type == "class_definition":
            # Get the name node (first named child)
            for child in node.children:
                if child.type == "identifier":
                    name = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
                    classes.append(name)
                    break
            # Also extract methods inside class
            for child in node.children:
                walk(child, depth + 1)

        elif node.type == "function_definition":
            for child in node.children:
                if child.type == "identifier":
                    name = source[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
                    # Include all functions — methods are useful for Planner grounding
                    functions.append(name)
                    break
            # Walk into function body for nested functions
            for child in node.children:
                if child.type == "block":
                    walk(child, depth + 1)
        else:
            for child in node.children:
                walk(child, depth + 1)

    walk(tree.root_node)

    # Deduplicate while preserving order
    seen = set()
    classes_dedup = [c for c in classes if not (c in seen or seen.add(c))]
    seen = set()
    functions_dedup = [f for f in functions if not (f in seen or seen.add(f))]

    return {"classes": classes_dedup, "functions": functions_dedup}


def _dedup_keep_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for v in values:
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def extract_symbols_textual(file_path: Path) -> dict:
    """
    Lightweight multi-language symbol extraction for non-Python files.
    Keeps schema compatible with planner: {"classes": [...], "functions": [...]}.
    """
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {"classes": [], "functions": []}

    ext = file_path.suffix.lower()
    classes: list[str] = []
    functions: list[str] = []

    # JS / TS / TSX / JSX
    if ext in {".js", ".jsx", ".ts", ".tsx"}:
        classes.extend(re.findall(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)", text))
        functions.extend(
            re.findall(
                r"\b(?:function|async function)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                text,
            )
        )
        functions.extend(
            re.findall(
                r"\bconst\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(",
                text,
            )
        )
        functions.extend(
            re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:async\s*)?\(",
                text,
            )
        )
    # Java / Kotlin / C# (best-effort)
    elif ext in {".java", ".kt", ".kts", ".cs"}:
        classes.extend(
            re.findall(
                r"\b(?:class|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)",
                text,
            )
        )
        functions.extend(
            re.findall(
                r"\b(?:public|private|protected|internal|static|\s)+\s*"
                r"[A-Za-z_<>\[\], ?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
                text,
            )
        )
    # Go / Rust / C/C++ / PHP
    elif ext in {".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".php"}:
        functions.extend(re.findall(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
        functions.extend(re.findall(r"\bfunc\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
        classes.extend(re.findall(r"\b(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)", text))
        functions.extend(
            re.findall(
                r"\b[A-Za-z_][A-Za-z0-9_*\s]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*\{",
                text,
            )
        )
    # SQL
    elif ext in {".sql"}:
        functions.extend(
            re.findall(
                r"(?im)\b(?:create\s+(?:or\s+replace\s+)?(?:function|procedure|view)\s+)([A-Za-z_][A-Za-z0-9_.]*)",
                text,
            )
        )
    # YAML / TOML / JSON / INI / CFG
    elif ext in {".yaml", ".yml", ".toml", ".json", ".ini", ".cfg"}:
        functions.extend(re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_.-]{2,})\s*:", text))
    # Shell
    elif ext in {".sh", ".bash", ".zsh"}:
        functions.extend(re.findall(r"(?m)^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", text))
    # RST / MD / TXT / HTML / CSS / XML
    elif ext in {".rst", ".md", ".txt", ".html", ".htm", ".css", ".xml"}:
        # Keep low-noise heading-like anchors as pseudo symbols.
        functions.extend(re.findall(r"(?m)^\s{0,3}#{1,6}\s+([A-Za-z0-9][^\n]{2,80})", text))
        functions.extend(re.findall(r"(?m)^\s*([A-Za-z0-9][A-Za-z0-9 _./:-]{2,80})\n[=~-]{3,}\s*$", text))

    # Generic fallback if no extractor hit: use filename stem as a weak anchor.
    if not classes and not functions:
        stem = file_path.stem.strip()
        if stem and stem not in {"index", "__init__", "conftest"}:
            functions = [stem]

    return {
        "classes": _dedup_keep_order(classes),
        "functions": _dedup_keep_order(functions),
    }


# ============================================================================
# FILE DISCOVERY
# ============================================================================

# Directories to skip (generated/vendor/cache paths).
SKIP_DIRS = {
    ".git", "__pycache__", ".tox", ".eggs", "*.egg-info",
    "node_modules", "dist", "build", "venv", ".venv",
    "licenses",
    "kubernetes",  # vendored test assets in upstream mirrors
    "scripts",     # generated helper scripts are noisy anchors
    "newsfragments",
}

# Index many code/config/doc extensions so retrieval candidates map more often.
INDEX_FILE_EXTENSIONS = {
    # Source code
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".cs",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".php", ".sql",
    # Config / docs / templates
    ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg",
    ".rst", ".md", ".txt",
    ".html", ".htm", ".css", ".xml",
    ".sh", ".bash", ".zsh",
}


def should_include_file(file_path: Path, repo_root: Path) -> bool:
    """Decide whether to include a file in the index."""
    try:
        rel = file_path.relative_to(repo_root)
    except ValueError:
        return False

    parts = rel.parts

    # Skip if any directory component is in SKIP_DIRS
    for part in parts:
        if part in SKIP_DIRS or part.endswith(".egg-info"):
            return False

    # Keep tests and migrations: they are frequent GraphRAG candidates.

    # Skip __init__.py files that are just imports — low signal.
    if file_path.suffix.lower() == ".py" and file_path.name == "__init__.py":
        try:
            content = file_path.read_text(errors="ignore")
            # Include __init__.py only if it has actual class/function definitions
            if "class " not in content and "def " not in content:
                return False
        except Exception:
            return False

    # Include only supported extensions.
    if file_path.suffix.lower() not in INDEX_FILE_EXTENSIONS:
        return False

    return True


def discover_files(repo_root: Path) -> list:
    """Discover all files worth indexing."""
    files = []
    for fp in repo_root.rglob("*"):
        if not fp.is_file():
            continue
        if should_include_file(fp, repo_root):
            files.append(fp)
    return sorted(files)


# ============================================================================
# KEYWORD EXTRACTION (used at training time)
# ============================================================================

def extract_keywords_from_issue(issue_text: str) -> set:
    """
    Extract keywords from issue text to match against the index.
    Called at training/inference time to get relevant file subset.
    """
    import re

    # Lowercase for matching
    text = issue_text.lower()

    keywords = set()

    # Extract CamelCase identifiers (class names, operator names)
    camel = re.findall(r'\b[A-Z][a-zA-Z0-9]+\b', issue_text)
    keywords.update(w.lower() for w in camel)

    # Extract snake_case identifiers
    snake = re.findall(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b', text)
    keywords.update(snake)

    # Extract file-path-like mentions
    paths = re.findall(r'airflow/[a-zA-Z0-9_/]+\.py', issue_text)
    keywords.update(p.lower() for p in paths)

    # Common Airflow component keywords worth matching
    component_keywords = [
        "scheduler", "executor", "dag", "task", "operator", "sensor",
        "hook", "provider", "trigger", "xcom", "variable", "connection",
        "webserver", "api", "model", "database", "migration",
    ]
    for kw in component_keywords:
        if kw in text:
            keywords.add(kw)

    return keywords


def get_relevant_index_subset(index: dict, issue_text: str, max_files: int = 40) -> dict:
    """
    Given the full index and issue text, return the relevant subset of entries.
    Matches file paths and symbol names against keywords from the issue.
    Called once per training example and at inference time.
    """
    keywords = extract_keywords_from_issue(issue_text)
    if not keywords:
        return {}

    scored = []
    for file_path, symbols in index.items():
        score = 0
        file_lower = file_path.lower()

        # Score by keyword matches in file path
        for kw in keywords:
            if kw in file_lower:
                score += 2

        # Score by keyword matches in class/function names
        all_symbols = symbols.get("classes", []) + symbols.get("functions", [])
        for sym in all_symbols:
            sym_lower = sym.lower()
            for kw in keywords:
                if kw in sym_lower:
                    score += 1

        if score > 0:
            scored.append((score, file_path, symbols))

    # Sort by relevance, take top max_files
    scored.sort(reverse=True, key=lambda x: x[0])
    return {fp: syms for _, fp, syms in scored[:max_files]}


def format_index_for_prompt(subset: dict) -> str:
    """
    Convert an index subset to a compact string for injection into model prompt.
    Keeps it concise — typically 300-600 tokens.
    """
    lines = []
    for file_path, symbols in subset.items():
        # Use short relative path
        short_path = file_path
        parts = []
        if symbols.get("classes"):
            parts.append(f"classes: {', '.join(symbols['classes'][:5])}")
        if symbols.get("functions"):
            # Limit to first 8 functions to control token count
            parts.append(f"functions: {', '.join(symbols['functions'][:8])}")
        if parts:
            lines.append(f"{short_path} | {' | '.join(parts)}")

    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser_args = argparse.ArgumentParser(description="Build Tree-sitter symbol index for Airflow")
    parser_args.add_argument(
        "--repo",
        type=str,
        required=True,
        help="Path to cloned Airflow repo (e.g. /Users/you/airflow)"
    )
    parser_args.add_argument(
        "--output",
        type=str,
        default="treesitter_index.json",
        help="Output path for the index JSON file"
    )
    args = parser_args.parse_args()

    repo_root = Path(args.repo).resolve()
    if not repo_root.exists():
        raise FileNotFoundError(f"Repo not found: {repo_root}")

    logging.info(f"Building Tree-sitter index for: {repo_root}")
    logging.info("Discovering indexable files...")

    ts_parser = build_parser()
    files = discover_files(repo_root)
    logging.info(f"Found {len(files)} files to index")

    index = {}
    start = time.time()
    errors = 0

    for i, file_path in enumerate(files):
        try:
            rel_path = str(file_path.relative_to(repo_root))
            if file_path.suffix.lower() == ".py":
                symbols = extract_symbols_python(file_path, ts_parser)
            else:
                symbols = extract_symbols_textual(file_path)

            # Only include files that actually have symbols
            if symbols["classes"] or symbols["functions"]:
                index[rel_path] = symbols

            if (i + 1) % 500 == 0:
                elapsed = time.time() - start
                rate = (i + 1) / elapsed
                remaining = (len(files) - i - 1) / rate
                logging.info(
                    f"  {i+1}/{len(files)} files | "
                    f"{len(index)} indexed | "
                    f"{elapsed:.0f}s elapsed | "
                    f"~{remaining:.0f}s remaining"
                )
        except Exception as e:
            errors += 1
            logging.debug(f"Error on {file_path}: {e}")

    elapsed = time.time() - start
    logging.info(f"Indexed {len(index)} files in {elapsed:.1f}s ({errors} errors skipped)")

    # Save index
    output_path = Path(args.output)
    with open(output_path, "w") as f:
        json.dump(index, f)

    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    logging.info(f"Saved index to {output_path} ({file_size_mb:.1f} MB)")

    # Stats
    total_classes = sum(len(v["classes"]) for v in index.values())
    total_functions = sum(len(v["functions"]) for v in index.values())
    logging.info(f"Index stats: {total_classes} classes, {total_functions} functions across {len(index)} files")

    # Quick sanity test
    test_query = "scheduler job execute task DAG run"
    subset = get_relevant_index_subset(index, test_query, max_files=10)
    logging.info(f"\nSanity test — query: '{test_query}'")
    logging.info(f"Top matching files: {list(subset.keys())[:5]}")
    sample_prompt = format_index_for_prompt(subset)
    token_estimate = len(sample_prompt) // 4
    logging.info(f"Sample prompt snippet ({token_estimate} tokens est.):\n{sample_prompt[:400]}")


if __name__ == "__main__":
    main()
