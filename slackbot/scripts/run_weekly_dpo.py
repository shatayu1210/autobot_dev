#!/usr/bin/env python3
"""CLI to run weekly DPO batch directly from slackbot package."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dpo_weekly import run_weekly  # noqa: E402


def main() -> int:
    load_dotenv(os.getenv("ENV_FILE", str(ROOT / ".env")))

    parser = argparse.ArgumentParser(description="Run weekly DPO flow from slackbot.")
    parser.add_argument("--skip-teacher", action="store_true", help="Skip GPT teacher labeling stage")
    parser.add_argument("--skip-train", action="store_true", help="Skip train webhook stage")
    parser.add_argument("--skip-deploy", action="store_true", help="Skip deploy stage")
    parser.add_argument("--cumulative", action="store_true", help="Export cumulative accepted rows until week_end")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")
    args = parser.parse_args()

    result = run_weekly(
        skip_teacher=args.skip_teacher,
        skip_train=args.skip_train,
        skip_deploy=args.skip_deploy,
        cumulative=args.cumulative,
    )

    payload = {
        "run_id": result.run_id,
        "week_start": result.week_start,
        "week_end": result.week_end,
        "loaded_rows": result.loaded_rows,
        "labeled_rows": result.labeled_rows,
        "exported_rows": result.exported_rows,
        "jsonl_path": result.jsonl_path,
        "new_revision": result.new_revision,
        "validation_ok": result.validation_ok,
        "progress": result.progress,
        "errors": result.errors,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in result.progress:
            print(item)
        for item in result.errors:
            print(f"ERROR: {item}", file=sys.stderr)
        print(f"validation_ok={result.validation_ok}")

    if result.errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
