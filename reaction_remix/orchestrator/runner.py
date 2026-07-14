from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from common.integrity import atomic_write_json
from common.schema import CommentaryAudio, ReactionSource, RemixEdl, RemixPlan, RemixRepairRequests
from reaction_remix.orchestrator.commands import build_command
from reaction_remix.orchestrator.graph import STAGES, dependency_closure, forced_stages, stage_range
from reaction_remix.orchestrator.paths import ReactionRunPaths, build_paths, primary_outputs
from reaction_remix.orchestrator.repair_state import (
    accepted_request_matches_source,
    load_accepted_repair_request,
    merge_active_with_accepted,
    persist_accepted_repairs,
    request_applies_to_stage,
)
from reaction_remix.orchestrator.runtime import (
    ReactionOrchestratorError,
    StageExecutionError,
    execute,
    validate_runtime_requirements,
)
from reaction_remix.orchestrator.summary import ReactionRunSummary, ReactionStageSummary
from reaction_remix.orchestrator.validation import outputs_valid


Executor = Callable[[list[str], Path], None]
PARALLEL_STAGES = ("analyze", "shots", "stems")
SEQUENTIAL_STAGES = ("segment", "plan", "write", "tts", "compose", "render", "qa")


def _output_signature(paths: ReactionRunPaths, stage: str) -> tuple[tuple[str, int, int], ...] | None:
    outputs = primary_outputs(paths, stage)
    if not all(path.is_file() for path in outputs):
        return None
    return tuple((path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in outputs)


def _fit_requests_pending(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("requests"))


def _qa_status(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("status")
    return str(value) if value in {"pass", "fail"} else None


def _load_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _current_source_hash(paths: ReactionRunPaths) -> str | None:
    if not paths.reaction_source.is_file():
        return None
    try:
        return ReactionSource.model_validate_json(
            paths.reaction_source.read_text(encoding="utf-8")
        ).input_hash
    except (OSError, ValueError):
        return None


def _duration_restore_block_ids(plan: RemixPlan, *, rendered_duration_s: float) -> list[str]:
    hard_min_s = plan.original_duration_s * plan.duration_policy.hard_min_output_ratio
    hard_max_s = plan.original_duration_s * plan.duration_policy.hard_max_output_ratio
    if rendered_duration_s >= hard_min_s - 1e-6:
        return []
    annotations = {item.block_id: item for item in plan.semantic_annotations}
    candidates = [item for item in plan.excluded_blocks if item.unique_reaction_speech_s > 0]
    candidates.sort(
        key=lambda item: (
            -float(getattr(annotations.get(item.block_id), "novelty", None) or 0.0),
            -float(getattr(annotations.get(item.block_id), "intensity", None) or 0.0),
            -item.unique_reaction_speech_s,
            item.block_id,
        )
    )
    selected: list[str] = []
    rendered_cursor = rendered_duration_s
    predicted_cursor = plan.predicted_duration_s
    for item in candidates:
        if rendered_cursor >= hard_min_s - 1e-6:
            break
        if rendered_cursor + item.source_duration_s > hard_max_s + 1e-6:
            continue
        if predicted_cursor + item.source_duration_s > hard_max_s + 1e-6:
            continue
        selected.append(item.block_id)
        rendered_cursor += item.source_duration_s
        predicted_cursor += item.source_duration_s
    return selected


def _build_qa_repairs(paths: ReactionRunPaths, *, repair_round: int) -> tuple[RemixRepairRequests, set[str]]:
    qa = _load_payload(paths.remix_qa)
    edl = RemixEdl.model_validate_json(paths.remix_edl.read_text(encoding="utf-8"))
    plan = RemixPlan.model_validate_json(paths.remix_plan.read_text(encoding="utf-8"))
    commentary_audio = CommentaryAudio.model_validate_json(paths.commentary_audio.read_text(encoding="utf-8"))
    items: list[dict] = []
    required_stages: set[str] = set()
    if qa["duration"]["status"] == "fail":
        affected = _duration_restore_block_ids(
            plan,
            rendered_duration_s=float(qa["duration"]["output_s"]),
        )
        if affected:
            items.append(
                {
                    "repair_id": f"repair-{len(items):04d}",
                    "kind": "duration_restore",
                    "affected_ids": affected,
                    "reason": "Rendered duration is below the reaction-remix hard floor.",
                    "attempt": repair_round,
                    "requested_stage": "plan",
                }
            )
            required_stages.update({"plan", "write", "tts", "compose", "render", "qa"})
    if qa["commentary"]["old_narrator_leakage_count"] > 0:
        affected_slots = [str(value) for value in qa["commentary"].get("old_narrator_leakage_slot_ids", [])]
        if not affected_slots:
            raise ReactionOrchestratorError(
                "QA detected narrator leakage but did not localize the affected commentary slots; rerun QA"
            )
        items.append(
            {
                "repair_id": f"repair-{len(items):04d}",
                "kind": "bed_leakage",
                "affected_ids": affected_slots,
                "reason": "Narrator leakage was detected; switch only affected commentary slots to TTS-only.",
                "attempt": repair_round,
                "requested_stage": "compose",
            }
        )
        required_stages.update({"compose", "render", "qa"})
    if qa["reaction_preservation"]["status"] == "fail":
        affected_placements = [
            str(value) for value in qa["reaction_preservation"].get("failed_placement_ids", [])
        ]
        if not affected_placements:
            raise ReactionOrchestratorError(
                "QA reaction preservation failed without localized placement IDs; cache bypass cannot repair it"
            )
        protected_placement_ids = {
            item.placement_id for item in edl.placements if item.kind in {"reaction", "mixed", "unknown"}
        }
        invalid_placement_ids = sorted(set(affected_placements) - protected_placement_ids)
        if invalid_placement_ids:
            raise ReactionOrchestratorError(
                "QA localized reaction preservation failure to non-protected or unknown placement(s): "
                + ", ".join(invalid_placement_ids)
            )
        items.append(
            {
                "repair_id": f"repair-{len(items):04d}",
                "kind": "reaction_media_mismatch",
                "affected_ids": affected_placements,
                "reason": "Reaction media preservation failed; bypass only localized placement caches.",
                "attempt": repair_round,
                "requested_stage": "render",
            }
        )
        required_stages.update({"render", "qa"})
    repairs = RemixRepairRequests.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": edl.source_hash,
            "items": items,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": [],
        }
    )
    return repairs, required_stages


def _relocalize_reaction_media_repairs(
    repairs: RemixRepairRequests,
    *,
    before_edl: RemixEdl,
    after_edl: RemixEdl,
) -> RemixRepairRequests:
    before_by_id = {placement.placement_id: placement for placement in before_edl.placements}
    after_by_origin: dict[str, list[str]] = {}
    for placement in after_edl.placements:
        if placement.kind in {"reaction", "mixed", "unknown"}:
            after_by_origin.setdefault(placement.origin_block_id, []).append(placement.placement_id)

    updated_items = []
    for item in repairs.items:
        if item.kind != "reaction_media_mismatch":
            updated_items.append(item)
            continue
        origins: list[str] = []
        for placement_id in item.affected_ids:
            placement = before_by_id.get(placement_id)
            if placement is None or placement.kind not in {"reaction", "mixed", "unknown"}:
                raise ReactionOrchestratorError(
                    f"cannot relocalize reaction repair for unknown protected placement {placement_id}"
                )
            if placement.origin_block_id not in origins:
                origins.append(placement.origin_block_id)
        invalid_origins = [
            origin_block_id
            for origin_block_id in origins
            if len(after_by_origin.get(origin_block_id, [])) != 1
        ]
        if invalid_origins:
            raise ReactionOrchestratorError(
                "reaction repair origin block(s) must map to exactly one protected placement after compose: "
                + ", ".join(invalid_origins)
            )
        relocalized = [after_by_origin[origin_block_id][0] for origin_block_id in origins]
        updated_items.append(item.model_copy(update={"affected_ids": relocalized}))
    return repairs.model_copy(update={"items": updated_items})


def _disabled_stage(stage: str, config: dict) -> bool:
    return stage == "shots" and not config["shots"]["enabled"]


def _pass_force_flag(
    stage: str,
    *,
    requested_force: bool,
    global_force: bool,
    explicit_force_stages: list[str],
) -> bool:
    if stage == "qa":
        return False
    if stage == "tts":
        return global_force or stage in explicit_force_stages
    if stage == "render":
        return global_force
    return global_force or requested_force


def _require_prerequisites(paths: ReactionRunPaths, selected: set[str], config: dict, film: Path) -> None:
    required: set[str] = set()
    for stage in selected:
        required.update(dependency_closure(stage) - selected)
    required = {stage for stage in required if not _disabled_stage(stage, config)}
    missing = [
        stage
        for stage in STAGES
        if stage in required and not outputs_valid(paths, stage, config=config, film=film)
    ]
    if missing:
        raise ReactionOrchestratorError(
            "selected stage range requires valid existing artifact(s) from: " + ", ".join(missing)
        )


def _planned_summaries(
    *,
    film: Path,
    paths: ReactionRunPaths,
    config: dict,
    selected: set[str],
    forced: set[str],
    global_force: bool,
    explicit_force_stages: list[str],
    accepted_repair_request: Path | None,
) -> list[ReactionStageSummary]:
    summaries: list[ReactionStageSummary] = []
    for stage in STAGES:
        if stage not in selected or _disabled_stage(stage, config):
            continue
        is_forced = stage in forced
        current = outputs_valid(paths, stage, config=config, film=film)
        status = "forced" if is_forced else "skip" if current else "run"
        command = build_command(
            stage,
            film=film,
            paths=paths,
            config=config,
            force=_pass_force_flag(
                stage,
                requested_force=is_forced,
                global_force=global_force,
                explicit_force_stages=explicit_force_stages,
            ),
            accepted_repair_request=(
                accepted_repair_request
                if request_applies_to_stage(accepted_repair_request, stage)
                else None
            ),
        )
        summaries.append(
            ReactionStageSummary(
                stage=stage,
                status=status,
                command=command,
                outputs=[str(path) for path in primary_outputs(paths, stage)],
                cache_hit=current and not is_forced,
            )
        )
    return summaries


def run_pipeline(
    args,
    *,
    config: dict,
    executor: Executor | None = None,
) -> int:  # type: ignore[no-untyped-def]
    executor = executor or execute
    film = args.input.expanduser().resolve()
    paths = build_paths(args.run_dir)
    selected = stage_range(args.from_stage, args.to_stage, args.only)
    if not config["shots"]["enabled"]:
        selected.discard("shots")
    forced = forced_stages(selected, args.force, args.force_stage)
    summary = ReactionRunSummary(source=str(film), run_dir=str(paths.run_dir))
    accepted_repair_request = load_accepted_repair_request(paths)
    initial_source_hash = _current_source_hash(paths)
    if (
        accepted_repair_request is not None
        and initial_source_hash is not None
        and not accepted_request_matches_source(accepted_repair_request, initial_source_hash)
    ):
        accepted_repair_request = None
    pending_accepted_repairs: list[RemixRepairRequests] = []
    validate_runtime_requirements(film, selected, config, dry_run=args.dry_run)
    _require_prerequisites(paths, selected, config, film)
    if args.dry_run:
        summary.stages = _planned_summaries(
            film=film,
            paths=paths,
            config=config,
            selected=selected,
            forced=forced,
            global_force=args.force,
            explicit_force_stages=args.force_stage,
            accepted_repair_request=accepted_repair_request,
        )
        for item in summary.stages:
            print(f"[{item.status}] {item.stage}")
            print("  command: " + " ".join(item.command))
            for output in item.outputs:
                print(f"  output: {output}")
        return 0

    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.work_dir.mkdir(parents=True, exist_ok=True)

    def run_one(
        stage: str,
        *,
        bypass_skip: bool = False,
        pass_force: bool | None = None,
        fit_repair: bool = False,
        active_repair_request: Path | None = None,
    ) -> ReactionStageSummary:
        requested_force = stage in forced
        should_force = (
            _pass_force_flag(
                stage,
                requested_force=requested_force,
                global_force=args.force,
                explicit_force_stages=args.force_stage,
            )
            if pass_force is None
            else pass_force
        )
        if requested_force and stage == "render" and not args.force:
            (paths.stage_work_dir("render") / "render_cache_manifest.json").unlink(missing_ok=True)
        command = build_command(
            stage,
            film=film,
            paths=paths,
            config=config,
            force=should_force,
            fit_repair=fit_repair,
            active_repair_request=active_repair_request,
            accepted_repair_request=(
                accepted_repair_request
                if active_repair_request is None
                and request_applies_to_stage(accepted_repair_request, stage)
                and accepted_request_matches_source(
                    accepted_repair_request,
                    _current_source_hash(paths),
                )
                else None
            ),
        )
        outputs = [str(path) for path in primary_outputs(paths, stage)]
        was_valid = outputs_valid(paths, stage, config=config, film=film)
        before_signature = _output_signature(paths, stage) if was_valid else None
        started = time.perf_counter()
        try:
            executor(command, paths.logs_dir / f"{stage}.log")
        except StageExecutionError as exc:
            if stage == "tts" and exc.returncode == 3 and _fit_requests_pending(paths.commentary_fit_requests):
                return ReactionStageSummary(
                    stage,
                    "repair_required",
                    command,
                    outputs,
                    duration_s=time.perf_counter() - started,
                )
            if stage == "qa" and exc.returncode == 1 and _qa_status(paths.remix_qa) == "fail":
                return ReactionStageSummary(
                    stage,
                    "failed_gate",
                    command,
                    outputs,
                    duration_s=time.perf_counter() - started,
                    error=str(exc),
                )
            raise
        if not outputs_valid(paths, stage, config=config, film=film):
            raise ReactionOrchestratorError(f"stage {stage} completed without valid declared outputs")
        after_signature = _output_signature(paths, stage)
        cache_hit = bool(
            not bypass_skip
            and not requested_force
            and not should_force
            and before_signature is not None
            and before_signature == after_signature
        )
        return ReactionStageSummary(
            stage,
            "skipped" if cache_hit else "completed",
            command,
            outputs,
            cache_hit=cache_hit,
            duration_s=time.perf_counter() - started,
        )

    try:
        if "probe" in selected:
            summary.stages.append(run_one("probe"))
            if accepted_repair_request is not None and not accepted_request_matches_source(
                accepted_repair_request,
                _current_source_hash(paths),
            ):
                accepted_repair_request = None

        parallel = [stage for stage in PARALLEL_STAGES if stage in selected]
        if parallel:
            with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
                futures = {pool.submit(run_one, stage): stage for stage in parallel}
                completed: dict[str, ReactionStageSummary] = {}
                for future in as_completed(futures):
                    stage = futures[future]
                    completed[stage] = future.result()
                summary.stages.extend(completed[stage] for stage in PARALLEL_STAGES if stage in completed)

        def resolve_tts_fit(result: ReactionStageSummary) -> ReactionStageSummary:
            if result.status != "repair_required":
                return result
            max_rounds = int(config["orchestrator"]["max_repair_rounds"])
            for repair_round in range(1, max_rounds + 1):
                summary.repair_rounds.append(
                    {"round": repair_round, "kind": "tts_fit", "request": str(paths.commentary_fit_requests)}
                )
                summary.stages.append(
                    run_one("write", bypass_skip=True, pass_force=False, fit_repair=True)
                )
                result = run_one("tts", bypass_skip=True, pass_force=False, fit_repair=True)
                summary.stages.append(result)
                if result.status != "repair_required":
                    return result
            raise ReactionOrchestratorError("commentary TTS still does not fit after maximum repair rounds")

        for stage in SEQUENTIAL_STAGES:
            if stage not in selected:
                continue
            result = run_one(stage)
            summary.stages.append(result)
            if stage == "tts":
                result = resolve_tts_fit(result)
            if stage == "qa":
                summary.final_qa_status = _qa_status(paths.remix_qa)
                if result.status == "failed_gate":
                    max_rounds = int(config["orchestrator"]["max_repair_rounds"])
                    for repair_round in range(1, max_rounds + 1):
                        repairs, repair_stages = _build_qa_repairs(paths, repair_round=repair_round)
                        if not repairs.items:
                            break
                        missing_scope = repair_stages - selected
                        if missing_scope:
                            raise ReactionOrchestratorError(
                                "QA repair requires stage(s) outside the selected range: "
                                + ", ".join(sorted(missing_scope))
                            )
                        pending_accepted_repairs.append(repairs)
                        repairs = merge_active_with_accepted(repairs, accepted_repair_request)
                        atomic_write_json(paths.repair_requests, repairs.model_dump(mode="json"))
                        summary.repair_rounds.append(
                            {
                                "round": repair_round,
                                "kind": "qa_repair",
                                "request": str(paths.repair_requests),
                                "stages": sorted(repair_stages),
                            }
                        )
                        active_repair_request = paths.repair_requests
                        pre_repair_edl = RemixEdl.model_validate_json(
                            paths.remix_edl.read_text(encoding="utf-8")
                        )
                        if "plan" in repair_stages:
                            summary.stages.append(
                                run_one(
                                    "plan",
                                    bypass_skip=True,
                                    pass_force=True,
                                    active_repair_request=active_repair_request,
                                )
                            )
                            summary.stages.append(run_one("write", bypass_skip=True, pass_force=True))
                            tts_result = run_one("tts", bypass_skip=True, pass_force=False)
                            summary.stages.append(tts_result)
                            resolve_tts_fit(tts_result)
                        if "compose" in repair_stages:
                            summary.stages.append(
                                run_one(
                                    "compose",
                                    bypass_skip=True,
                                    pass_force=True,
                                    active_repair_request=active_repair_request,
                                )
                            )
                            if any(item.kind == "reaction_media_mismatch" for item in repairs.items):
                                post_repair_edl = RemixEdl.model_validate_json(
                                    paths.remix_edl.read_text(encoding="utf-8")
                                )
                                repairs = _relocalize_reaction_media_repairs(
                                    repairs,
                                    before_edl=pre_repair_edl,
                                    after_edl=post_repair_edl,
                                )
                                atomic_write_json(
                                    paths.repair_requests,
                                    repairs.model_dump(mode="json"),
                                )
                        summary.stages.append(
                            run_one(
                                "render",
                                bypass_skip=True,
                                pass_force=False,
                                active_repair_request=active_repair_request,
                            )
                        )
                        qa_result = run_one(
                            "qa",
                            bypass_skip=True,
                            pass_force=False,
                            active_repair_request=active_repair_request,
                        )
                        summary.stages.append(qa_result)
                        summary.final_qa_status = _qa_status(paths.remix_qa)
                        if qa_result.status != "failed_gate":
                            result = qa_result
                            break
                    if result.status == "failed_gate":
                        raise ReactionOrchestratorError("reaction-remix QA hard gate failed after repair rounds")

        if "qa" in selected and summary.final_qa_status == "pass":
            if accepted_repair_request is not None or pending_accepted_repairs:
                accepted_repair_request = persist_accepted_repairs(
                    paths,
                    qa_path=paths.remix_qa,
                    current_repairs=pending_accepted_repairs,
                    previous_request=accepted_repair_request,
                )
            summary.deliverable = str(paths.output_video)
        return 0
    finally:
        if paths.run_dir.is_dir():
            summary.write(paths.summary)
