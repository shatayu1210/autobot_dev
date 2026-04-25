"""
Tree-sitter index builder (patch planner)
=========================================
Wrapper around the real implementation in `tree_sitter/build_treesitter_index.py`.

Why this wrapper exists:
- Keep the training/patch-planner tooling under `autobot_dev/cli/`
- Write generated `treesitter_index.json` into an `outputs/` directory that is
  expected to be gitignored.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Tree-sitter symbol index for Airflow (patch planner)."
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="Path to cloned Airflow repo (e.g. /path/to/airflow)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON path. Defaults to ./outputs/treesitter_index.json",
    )
    args = parser.parse_args()

    this_dir = Path(__file__).resolve().parent  # .../cli/patch_planner/treesitter
    outputs_dir = this_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = outputs_dir / "treesitter_index.json"

    # Original implementation lives in autobot_dev/tree_sitter/build_treesitter_index.py
    # Wrapper location: autobot_dev/cli/patch_planner/treesitter/build_treesitter_index.py
    autobot_dev_root = this_dir.parents[2]
    old_impl = autobot_dev_root / "tree_sitter" / "build_treesitter_index.py"
    if not old_impl.exists():
        raise FileNotFoundError(f"Expected implementation not found: {old_impl}")

    cmd = [
        sys.executable,
        str(old_impl),
        "--repo",
        args.repo,
        "--output",
        str(output_path),
    ]
    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()

