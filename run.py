from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from orchestrator.config import ConfigError, load_config
from orchestrator.cost_policy import build_cost_summary, resolve_cost_policy
from orchestrator.graph import STAGES, build_paths, forced_stages, stage_range
from orchestrator.runner import OrchestratorError, outputs_valid, preflight, run_stage
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


def should_fallback_timecode(paths, config: dict) -> tuple[bool, list[str]]:  # type: ignore[no-untyped-def]
    orchestrator = config.get("orchestrator", {})
    if not orchestrator.get("auto_fallback", False):
        return False, ["auto_fallback=false"]
    if not orchestrator.get("fallback_on_timecode_warn", True):
        return False, ["fallback_on_timecode_warn=false"]
    if not paths.film_map_meta.is_file():
        return False, ["film_map.meta.json missing"]
    meta = json.loads(paths.film_map_meta.read_text(encoding="utf-8"))
    reasons: list[str] = []
    if meta.get("timecode_quality") != "strict":
        reasons.append(f"timecode_quality={meta.get('timecode_quality')}")
    if meta.get("approximate_timecodes"):
        reasons.append("approximate_timecodes=true")
    warnings = meta.get("asr_warnings") or meta.get("warnings") or []
    if isinstance(warnings, list):
        for warning in warnings:
            lowered = str(warning).lower()
            if any(token in lowered for token in ("align", "fallback", "hallucination", "timecode")):
                reasons.append(f"warning={warning}")
                break
    if meta.get("asr_provider") == config.get("orchestrator", {}).get("fallback_ingest_asr_provider", "openai-gpt4o-hybrid"):
        return False, ["already using fallback ASR provider"]
    return bool(reasons), reasons or ["timecode QA passed"]

def build_fallback_config(config: dict) -> dict:
    fallback = deepcopy(config)
    orchestrator = fallback.get("orchestrator", {})
    ingest = fallback.setdefault("ingest", {})
    ingest["asr_policy"] = "openai_hybrid"
    ingest["asr_provider"] = orchestrator.get("fallback_ingest_asr_provider", "openai-gpt4o-hybrid")
    ingest["max_vision_frames"] = orchestrator.get("fallback_max_vision_frames", 0)
    return fallback

def write_fallback_artifacts(paths, plan: dict, summary: dict | None = None) -> None:  # type: ignore[no-untyped-def]
    paths.fallback_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if summary is not None:
        paths.fallback_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

def run_pipeline(args: argparse.Namespace, executor: Callable[[list[str], Path], None] | None = None) -> int:
    film = args.input.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    config = load_config(args.config.expanduser().resolve() if args.config else None)
    config, cost_policy = resolve_cost_policy(config)
    paths = build_paths(run_dir)
    selected = stage_range(args.from_stage, args.to_stage, args.only)
    if not config.get("preflight", {}).get("enabled", True):
        selected.discard("preflight")
    if not config.get("visual_index", {}).get("enabled", False):
        selected.discard("visual_index")
    forced = forced_stages(selected, args.force, args.force_stage)
    python_exe = config.get("orchestrator", {}).get("python")
    will_run = {stage for stage in selected if stage in forced or not outputs_valid(paths, stage)}
    openai_fallback_possible = bool(config.get("orchestrator", {}).get("auto_fallback", False) and "ingest" in selected)
    cost_summary = build_cost_summary(cost_policy, selected, will_run, openai_fallback_possible=openai_fallback_possible)
    preflight(film=film, selected=selected, forced=forced, paths=paths, config=config, dry_run=args.dry_run, cost_policy=cost_policy)

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
        print(json.dumps({"cost_policy": cost_policy.to_json(), "cost_summary": cost_summary, "fallback_plan": {"possible": openai_fallback_possible, "triggered": False, "dry_run": True}}, ensure_ascii=False, indent=2))
        for stage in STAGES:
            if stage in selected:
                summaries.append(execute(stage))
        print_plan(summaries)
        return 0

    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.cost_policy.write_text(json.dumps(cost_policy.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    paths.cost_summary.write_text(json.dumps(cost_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_fallback_artifacts(paths, {"possible": openai_fallback_possible, "triggered": False, "reasons": []})

    fallback_triggered = False

    if "preflight" in selected:
        summaries.append(execute("preflight"))

    if "shots" in selected:
        with ThreadPoolExecutor(max_workers=1) as pool:
            shots_future = pool.submit(execute, "shots")
            for stage in ("ingest", "storymap", "review", "tts"):
                if stage in selected:
                    summaries.append(execute(stage))
                    if stage == "ingest":
                        fallback_triggered = maybe_run_ingest_fallback(
                            paths=paths,
                            film=film,
                            config=config,
                            selected=selected,
                            summaries=summaries,
                            forced=forced,
                            python_exe=python_exe,
                            executor=executor,
                        )
            summaries.append(shots_future.result())
            if "visual_index" in selected:
                summaries.append(execute("visual_index"))
    else:
        for stage in ("ingest", "storymap", "review", "tts"):
            if stage in selected:
                summaries.append(execute(stage))
                if stage == "ingest":
                    fallback_triggered = maybe_run_ingest_fallback(
                        paths=paths,
                        film=film,
                        config=config,
                        selected=selected,
                        summaries=summaries,
                        forced=forced,
                        python_exe=python_exe,
                        executor=executor,
                    )
        if "visual_index" in selected:
            summaries.append(execute("visual_index"))

    for stage in ("match", "render"):
        if stage in selected:
            summaries.append(execute(stage))

    cost_summary = build_cost_summary(cost_policy, selected, will_run, openai_fallback_possible=openai_fallback_possible, openai_fallback_triggered=fallback_triggered)
    paths.cost_summary.write_text(json.dumps(cost_summary, ensure_ascii=False, indent=2), encoding="utf-8")

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
            "visual_index": paths.shot_visual_index,
            "match": paths.edl_meta,
            "render": paths.render_meta,
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def maybe_run_ingest_fallback(
    *,
    paths,
    film: Path,
    config: dict,
    selected: set[str],
    summaries: list[StageSummary],
    forced: set[str],
    python_exe: str | None,
    executor: Callable[[list[str], Path], None] | None,
) -> bool:
    if "ingest" not in selected:
        return False
    trigger, reasons = should_fallback_timecode(paths, config)
    if not trigger:
        write_fallback_artifacts(paths, {"possible": bool(config.get("orchestrator", {}).get("auto_fallback", False)), "triggered": False, "reasons": reasons})
        return False
    if config.get("orchestrator", {}).get("api_budget_guard") == "block":
        write_fallback_artifacts(paths, {"possible": True, "triggered": False, "blocked": True, "reasons": reasons})
        raise OrchestratorError("OpenAI fallback required but blocked by api_budget_guard=block: " + "; ".join(reasons))
    if not os.getenv("OPENAI_API_KEY"):
        write_fallback_artifacts(paths, {"possible": True, "triggered": False, "blocked": True, "reasons": reasons, "error": "OPENAI_API_KEY missing"})
        raise OrchestratorError("OpenAI fallback required but OPENAI_API_KEY is not set: " + "; ".join(reasons))
    fallback_config = build_fallback_config(config)
    forced.update(stage for stage in ("storymap", "review", "tts", "match", "render") if stage in selected)
    plan = {
        "possible": True,
        "triggered": True,
        "reasons": reasons,
        "fallback_asr_provider": fallback_config["ingest"].get("asr_provider"),
        "fallback_max_vision_frames": fallback_config["ingest"].get("max_vision_frames"),
        "rerun_stages": [stage for stage in ("ingest", "storymap", "review", "tts", "match", "render") if stage in selected],
    }
    write_fallback_artifacts(paths, plan)
    stage_summary = run_stage(
        stage="ingest",
        paths=paths,
        film=film,
        config=fallback_config,
        force=True,
        dry_run=False,
        python_exe=python_exe,
        executor=executor if executor is not None else run_stage.__globals__["run_subprocess"],
    )
    summaries.append(stage_summary)
    after_meta = json.loads(paths.film_map_meta.read_text(encoding="utf-8")) if paths.film_map_meta.is_file() else {}
    write_fallback_artifacts(paths, plan, {"triggered": True, "reasons": reasons, "after_timecode_quality": after_meta.get("timecode_quality"), "after_approximate_timecodes": after_meta.get("approximate_timecodes"), "after_asr_provider": after_meta.get("asr_provider")})
    return True

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_pipeline(args)
    except (OrchestratorError, ConfigError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"run.py: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
