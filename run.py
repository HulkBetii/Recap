from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from orchestrator.config import ConfigError, load_config
from orchestrator.graph import STAGES, build_paths, forced_stages, stage_range
from orchestrator.runner import OrchestratorError, preflight, run_stage
from orchestrator.summary import StageSummary, write_summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run full Recap pipeline: film.mp4 -> recap.mp4")
    parser.add_argument("--input", required=True, type=Path, help="Input film.mp4")
    parser.add_argument("--run-dir", required=True, type=Path, help="Run output directory")
    parser.add_argument("--config", type=Path, default=None, help="config.yaml/config.json")
    parser.add_argument("--from", dest="from_stage", choices=STAGES, default=None)
    parser.add_argument("--to", dest="to_stage", choices=STAGES, default=None)
    parser.add_argument("--only", choices=STAGES, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-stage", action="append", default=[], choices=STAGES)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def print_plan(stages: list[StageSummary]) -> None:
    for stage in stages:
        print(f"[{stage.status}] {stage.stage}")
        print("  command: " + " ".join(stage.command))
        for output in stage.outputs:
            print(f"  output: {output}")


def run_pipeline(args: argparse.Namespace, executor: Callable[[list[str], Path], None] | None = None) -> int:
    film = args.input.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    config = load_config(args.config.expanduser().resolve() if args.config else None)
    paths = build_paths(run_dir)
    selected = stage_range(args.from_stage, args.to_stage, args.only)
    if not config.get("preflight", {}).get("enabled", True):
        selected.discard("preflight")
    forced = forced_stages(selected, args.force, args.force_stage)
    python_exe = config.get("orchestrator", {}).get("python")
    preflight(film=film, selected=selected, forced=forced, paths=paths, config=config, dry_run=args.dry_run)

    summaries: list[StageSummary] = []

    def execute(stage: str) -> StageSummary:
        return run_stage(
            stage=stage,
            paths=paths,
            film=film,
            config=config,
            force=stage in forced,
            dry_run=args.dry_run,
            python_exe=python_exe,
            executor=executor if executor is not None else run_stage.__globals__["run_subprocess"],
        )

    if args.dry_run:
        for stage in STAGES:
            if stage in selected:
                summaries.append(execute(stage))
        print_plan(summaries)
        return 0

    paths.run_dir.mkdir(parents=True, exist_ok=True)

    if "preflight" in selected:
        summaries.append(execute("preflight"))

    if "shots" in selected:
        with ThreadPoolExecutor(max_workers=1) as pool:
            shots_future = pool.submit(execute, "shots")
            for stage in ("ingest", "storymap", "review", "tts"):
                if stage in selected:
                    summaries.append(execute(stage))
            summaries.append(shots_future.result())
    else:
        for stage in ("ingest", "storymap", "review", "tts"):
            if stage in selected:
                summaries.append(execute(stage))

    for stage in ("match", "render"):
        if stage in selected:
            summaries.append(execute(stage))

    summary = write_summary(
        path=paths.summary,
        stages=summaries,
        meta_paths={
            "preflight": paths.video_profile,
            "ingest": paths.film_map_meta,
            "storymap": paths.story_map_meta,
            "review": paths.review_meta,
            "tts": paths.tts_meta,
            "shots": paths.shots_meta,
            "match": paths.edl_meta,
            "render": paths.render_meta,
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_pipeline(args)
    except (OrchestratorError, ConfigError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"run.py: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
