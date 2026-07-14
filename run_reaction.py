from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reaction_remix.orchestrator.config import ReactionConfigError, load_config
from reaction_remix.orchestrator.graph import STAGES
from reaction_remix.orchestrator.runner import run_pipeline
from reaction_remix.orchestrator.runtime import ReactionOrchestratorError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Reaction Remix: preserve reactions and rewrite Japanese commentary"
    )
    parser.add_argument("--input", required=True, type=Path, help="Authorized source reaction video")
    parser.add_argument("--run-dir", required=True, type=Path, help="Run artifact directory")
    parser.add_argument("--config", type=Path, default=Path("config.reaction-remix.yaml"))
    parser.add_argument("--from", dest="from_stage", choices=STAGES, default=None)
    parser.add_argument("--to", dest="to_stage", choices=STAGES, default=None)
    parser.add_argument("--only", choices=STAGES, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-stage", action="append", default=[], choices=STAGES)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        config_path = args.config.expanduser().resolve() if args.config else None
        config = load_config(config_path)
        return run_pipeline(args, config=config)
    except (ReactionConfigError, ReactionOrchestratorError, ValueError, OSError) as exc:
        parser.exit(2, f"reaction-remix: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
