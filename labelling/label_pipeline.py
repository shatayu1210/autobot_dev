#!/usr/bin/env python3
"""AutoBot Label Pipeline — Stage 4 of the ETL pipeline.

Reads snapshot JSON files from data/snapshots/ and writes labeled training
examples to data/labeled/{model}/.

Usage:
    python label_pipeline.py --model scorer
    python label_pipeline.py --model all
    python label_pipeline.py --model scorer --limit 100 --dry-run
    python label_pipeline.py --model all --stats

Models:
    scorer    — float 0.0-1.0 bottleneck risk score (60% prog + 40% GPT-4o)
    reasoner  — 2-3 sentence narrative (100% GPT-4o)
    planner   — structured plan derived from PR metadata (100% programmatic)
    patcher   — unified diff labels from PR files (100% programmatic)
    critic    — ACCEPT/REVISE/REJECT verdict (real reviews + ~30% GPT-4o synthetic)
    all       — run all five in sequence

Environment:
    OPENAI_API_KEY — required for scorer (40%), reasoner (100%), critic (30%)

Output format (data/labeled/{model}/{issue_number}.json):
    {
      "issue_number": int,
      "model": str,
      "input":  { ... },   # snapshot fields used as model input
      "label":  { ... }    # ground truth label
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent
SNAPSHOTS_DIR = BASE_DIR / "data" / "snapshots"
OUTPUT_DIR = BASE_DIR / "data" / "labeled"

# Load .env from project root
from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env", override=True)

MODELS = ["scorer", "reasoner", "planner", "patcher", "critic"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("label_pipeline")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def check_openai_key(model: str) -> None:
    """Warn if OPENAI_API_KEY is not set for models that need it."""
    needs_key = {"scorer", "reasoner", "critic"}
    if model in needs_key and not os.environ.get("OPENAI_API_KEY"):
        logger.error(
            "OPENAI_API_KEY is not set. Model '%s' requires GPT-4o calls. "
            "Export the key or use --dry-run to skip API calls.",
            model,
        )
        sys.exit(1)


def print_stats(output_dir: Path) -> None:
    """Print label counts and a sample record for each model."""
    print("\n=== Label Pipeline Stats ===")
    for model in MODELS:
        model_dir = output_dir / model
        if not model_dir.exists():
            print(f"  {model:12s} — not yet labeled")
            continue
        files = list(model_dir.glob("*.json"))
        print(f"  {model:12s} — {len(files):>5} labeled examples")
        if files:
            sample = json.loads(files[0].read_text())
            label_preview = str(sample.get("label", {}))[:120]
            print(f"               sample label: {label_preview}")
    print()


def estimate_cost(snapshots_dir: Path) -> None:
    """Rough GPT-4o cost estimate before running."""
    n = len(list(snapshots_dir.glob("*.json")))
    if n == 0:
        logger.warning("No snapshot files found in %s", snapshots_dir)
        return

    # Rough token estimates based on architecture doc
    scorer_calls   = int(n * 0.40)
    reasoner_calls = n
    critic_calls   = int(n * 0.30)  # synthetic fraction

    # ~800 tokens/call for scorer, ~700 for reasoner, ~900 for critic (input+output)
    total_tokens = (scorer_calls * 800) + (reasoner_calls * 700) + (critic_calls * 900)
    cost_usd = (total_tokens / 1_000_000) * 5.0  # GPT-4o $5/M input tokens (rough)

    print(f"\n=== Cost Estimate ({n} snapshots) ===")
    print(f"  Scorer  GPT-4o calls : {scorer_calls:>5}  (~800 tok each)")
    print(f"  Reasoner GPT-4o calls: {reasoner_calls:>5}  (~700 tok each)")
    print(f"  Critic  GPT-4o calls : {critic_calls:>5}  (~900 tok each)")
    print(f"  Total tokens         : {total_tokens:>7,}")
    print(f"  Estimated cost       : ~${cost_usd:.2f} USD (rough upper bound)")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_labeler(
    model: str,
    snapshots_dir: Path,
    output_dir: Path,
    limit: int | None,
    dry_run: bool,
) -> dict[str, int]:
    from labelers import (
        ScorerLabeler, ReasonerLabeler, PlannerLabeler, PatcherLabeler, CriticLabeler
    )

    labeler_cls = {
        "scorer":   ScorerLabeler,
        "reasoner": ReasonerLabeler,
        "planner":  PlannerLabeler,
        "patcher":  PatcherLabeler,
        "critic":   CriticLabeler,
    }[model]

    labeler = labeler_cls(snapshots_dir=snapshots_dir, output_dir=output_dir, dry_run=dry_run)
    logger.info("Starting labeler: %s (dry_run=%s, limit=%s)", model, dry_run, limit)
    counts = labeler.label_all(limit=limit)
    logger.info(
        "Done [%s] — labeled: %d, skipped: %d, errors: %d",
        model, counts["labeled"], counts["skipped"], counts["errors"],
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AutoBot Label Pipeline — assign ground-truth labels to snapshot data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--model",
        choices=MODELS + ["all"],
        required=True,
        help="Which model's labels to generate ('all' runs all five in sequence).",
    )
    parser.add_argument(
        "--snapshots-dir",
        type=Path,
        default=SNAPSHOTS_DIR,
        help=f"Directory containing snapshot JSON files (default: {SNAPSHOTS_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Root output directory for labeled examples (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N snapshots (useful for testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run labeling logic but do NOT write output files. Useful for testing.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print label counts for each model and exit.",
    )
    parser.add_argument(
        "--estimate-cost",
        action="store_true",
        help="Print a GPT-4o cost estimate based on snapshot count and exit.",
    )

    args = parser.parse_args()

    if args.stats:
        print_stats(args.output_dir)
        return

    if args.estimate_cost:
        estimate_cost(args.snapshots_dir)
        return

    if not args.snapshots_dir.exists():
        logger.error("Snapshots directory not found: %s", args.snapshots_dir)
        sys.exit(1)

    models_to_run = MODELS if args.model == "all" else [args.model]

    # Check API key requirements upfront
    for m in models_to_run:
        if not args.dry_run:
            check_openai_key(m)

    total_counts: dict[str, int] = {"labeled": 0, "skipped": 0, "errors": 0}

    for m in models_to_run:
        counts = run_labeler(
            model=m,
            snapshots_dir=args.snapshots_dir,
            output_dir=args.output_dir,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        for k in total_counts:
            total_counts[k] += counts.get(k, 0)

    if len(models_to_run) > 1:
        logger.info(
            "All done — total labeled: %d, skipped: %d, errors: %d",
            total_counts["labeled"], total_counts["skipped"], total_counts["errors"],
        )


if __name__ == "__main__":
    main()
