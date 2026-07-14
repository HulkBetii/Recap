from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from threading import Barrier, Lock

import pytest

from common.integrity import atomic_write_json, file_hash, media_identity_hash
from common.schema import CommentaryFitRequests, RemixExcludedBlock, RemixRepairRequests, RemixSemanticAnnotation
from reaction_remix.compose.composer import compose_remix
from reaction_remix.orchestrator.commands import build_command
from reaction_remix.orchestrator.config import ReactionConfigError, load_config
from reaction_remix.orchestrator.graph import forced_stages, stage_range
from reaction_remix.orchestrator.paths import build_paths, primary_outputs
from reaction_remix.orchestrator.repair_state import (
    load_accepted_repair_request,
    merge_active_with_accepted,
    persist_accepted_repairs,
)
from reaction_remix.orchestrator.runtime import ReactionOrchestratorError, StageExecutionError
from reaction_remix.orchestrator.runner import _build_qa_repairs, _pass_force_flag, run_pipeline
from reaction_remix.orchestrator.validation import outputs_valid
from reaction_remix.segment.__main__ import run_segment
from tests.reaction_factories import (
    make_blocks,
    make_commentary_audio,
    make_commentary_script,
    make_plan,
    make_source,
    make_transcript,
)


def reaction_args(
    film: Path,
    run_dir: Path,
    *,
    from_stage: str | None = None,
    to_stage: str | None = None,
    only: str | None = None,
    dry_run: bool = False,
) -> Namespace:
    return Namespace(
        input=film,
        run_dir=run_dir,
        from_stage=from_stage,
        to_stage=to_stage,
        only=only,
        force=False,
        force_stage=[],
        dry_run=dry_run,
    )


def passing_qa_payload(*, source_hash: str, edl_hash: str, output_path: Path) -> dict:
    return {
        "schema_version": "reaction-remix.v1",
        "source_hash": source_hash,
        "edl_hash": edl_hash,
        "output_path": output_path.as_posix(),
        "status": "pass",
        "duration": {
            "source_s": 100.0,
            "output_s": 95.0,
            "output_ratio": 0.95,
            "hard_min_ratio": 0.8,
            "preferred_range": [0.85, 0.9],
            "status": "pass",
        },
        "reaction_preservation": {
            "placements_checked": 2,
            "speed_mismatches": 0,
            "gain_mismatches": 0,
            "span_mismatches": 0,
            "failed_placement_ids": [],
            "max_gain_delta_db": 0.1,
            "min_audio_correlation": 0.999,
            "max_av_drift_ms": 10.0,
            "min_sample_frame_similarity": 0.999,
            "status": "pass",
        },
        "commentary": {
            "slots_checked": 1,
            "provider_mismatches": 0,
            "voice_mismatches": 0,
            "old_narrator_leakage_count": 0,
            "old_narrator_leakage_slot_ids": [],
            "protected_narrator_overlap_block_ids": [],
            "min_asr_text_match": 1.0,
            "status": "pass",
        },
        "visual_policy": {
            "mask_operations": 0,
            "subtitle_additions": 0,
            "text_overlays": 0,
            "blur_operations": 0,
            "other_overlays": 0,
            "status": "pass",
        },
        "audio": {
            "unexpected_silence_count": 0,
            "boundary_click_count": 0,
            "max_commentary_true_peak_dbfs": -1.5,
            "full_output_true_peak_dbfs": -2.0,
            "source_true_peak_dbfs": -2.0,
            "peak_increase_db": 0.0,
            "status": "pass",
        },
        "timeline": {
            "gap_count": 0,
            "overlap_count": 0,
            "decode_ok": True,
            "status": "pass",
        },
        "repairs": [],
        "created_at": "2026-01-01T00:00:00Z",
        "warnings": [],
    }


def test_reaction_stage_range_and_only() -> None:
    assert stage_range(only="compose") == {"compose"}
    assert stage_range("segment", "tts") == {"segment", "plan", "write", "tts"}
    with pytest.raises(ValueError, match="cannot be combined"):
        stage_range("plan", None, "tts")


def test_reaction_force_stage_invalidates_selected_downstream() -> None:
    selected = stage_range()

    assert forced_stages(selected, False, ["tts"]) == {"tts", "compose", "render", "qa"}
    assert forced_stages({"analyze", "segment", "plan"}, False, ["analyze"]) == {
        "analyze",
        "segment",
        "plan",
    }


def test_reaction_config_locks_voice_backend_and_ratio(tmp_path) -> None:
    assert load_config(None)["compose"]["commentary_visual_priority"] == ["commentary"]

    path = tmp_path / "reaction.json"
    path.write_text(json.dumps({"tts": {"voice_id": "other"}}), encoding="utf-8")
    with pytest.raises(ReactionConfigError, match="voice_id"):
        load_config(path)

    path.write_text(json.dumps({"plan": {"hard_min_output_ratio": 0.79}}), encoding="utf-8")
    with pytest.raises(ReactionConfigError, match="0.80-1.00"):
        load_config(path)

    path.write_text(
        json.dumps({"compose": {"commentary_visual_priority": ["broll", "transition", "commentary"]}}),
        encoding="utf-8",
    )
    with pytest.raises(ReactionConfigError, match="commentary_visual_priority"):
        load_config(path)


def test_reaction_config_rejects_unknown_keys(tmp_path) -> None:
    path = tmp_path / "reaction.json"
    path.write_text(json.dumps({"compose": {"unknown": True}}), encoding="utf-8")

    with pytest.raises(ReactionConfigError, match="compose.unknown"):
        load_config(path)


def test_reaction_paths_keep_artifacts_separate_from_recap(tmp_path) -> None:
    paths = build_paths(tmp_path / "run")

    assert paths.remix_edl.name == "remix_edl.json"
    assert paths.output_video.name == "reaction_remix.mp4"
    assert all(path.parent == paths.run_dir for path in primary_outputs(paths, "render"))


def test_reaction_commands_match_every_stage_parser(tmp_path) -> None:
    import importlib

    film = tmp_path / "source.mp4"
    paths = build_paths(tmp_path / "run")
    config = load_config(None)
    for stage in ("probe", "analyze", "shots", "stems", "segment", "plan", "write", "tts", "compose", "render", "qa"):
        module_name = "shots.__main__" if stage == "shots" else f"reaction_remix.{stage}.__main__"
        parser = importlib.import_module(module_name).build_parser()
        command = build_command(stage, film=film, paths=paths, config=config)

        parser.parse_args(command[3:])

    segment_command = build_command("segment", film=film, paths=paths, config=config)
    policy_index = segment_command.index("--commentary-boundary-policy")
    assert segment_command[policy_index + 1] == "strict-or-word-edge"


def test_stale_repair_artifact_is_only_passed_during_active_repair_round(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    paths.repair_requests.write_text("{}", encoding="utf-8")
    config = load_config(None)

    assert load_accepted_repair_request(paths) is None

    regular = {
        stage: build_command(stage, film=film, paths=paths, config=config)
        for stage in ("plan", "compose", "render", "qa")
    }
    assert "--repair-request" not in regular["plan"]
    assert "--repair-request" not in regular["compose"]
    assert "--repair-overrides" not in regular["compose"]
    assert "--repair-request" not in regular["render"]
    assert "--repair-requests" not in regular["qa"]

    active = {
        stage: build_command(
            stage,
            film=film,
            paths=paths,
            config=config,
            active_repair_request=paths.repair_requests,
        )
        for stage in ("plan", "compose", "render", "qa")
    }
    assert active["plan"][-4:-2] == ["--repair-request", str(paths.repair_requests)]
    assert "--repair-request" in active["compose"]
    assert "--repair-overrides" in active["compose"]
    assert "--repair-request" in active["render"]
    assert "--repair-requests" in active["qa"]


def test_accepted_repair_ledger_rejects_corruption_and_source_mismatch(tmp_path: Path) -> None:
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    source_hash = "a" * 64
    atomic_write_json(
        paths.remix_qa,
        passing_qa_payload(
            source_hash=source_hash,
            edl_hash="b" * 64,
            output_path=paths.output_video,
        ),
    )
    repairs = RemixRepairRequests.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "items": [
                {
                    "repair_id": "repair-0000",
                    "kind": "bed_leakage",
                    "affected_ids": ["slot-0001"],
                    "reason": "accepted after QA repair",
                    "attempt": 1,
                    "requested_stage": "compose",
                }
            ],
            "created_at": "2026-01-01T00:00:00Z",
            "warnings": [],
        }
    )
    accepted = persist_accepted_repairs(
        paths,
        qa_path=paths.remix_qa,
        current_repairs=[repairs],
        previous_request=None,
    )
    assert load_accepted_repair_request(paths) == accepted
    refreshed = persist_accepted_repairs(
        paths,
        qa_path=paths.remix_qa,
        current_repairs=[],
        previous_request=accepted,
    )
    assert refreshed == accepted
    assert load_accepted_repair_request(paths) == accepted

    active = RemixRepairRequests.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "items": [
                {
                    "repair_id": "repair-0001",
                    "kind": "reaction_media_mismatch",
                    "affected_ids": ["placement-0001"],
                    "reason": "new media mismatch",
                    "attempt": 1,
                    "requested_stage": "render",
                }
            ],
            "created_at": "2026-01-02T00:00:00Z",
            "warnings": [],
        }
    )
    merged = merge_active_with_accepted(active, accepted)
    assert {item.kind for item in merged.items} == {"bed_leakage", "reaction_media_mismatch"}

    ledger = json.loads(paths.accepted_repair_ledger.read_text(encoding="utf-8"))
    ledger["request_hash"] = "0" * 64
    atomic_write_json(paths.accepted_repair_ledger, ledger)
    assert load_accepted_repair_request(paths) is None

    persist_accepted_repairs(
        paths,
        qa_path=paths.remix_qa,
        current_repairs=[repairs],
        previous_request=None,
    )
    ledger = json.loads(paths.accepted_repair_ledger.read_text(encoding="utf-8"))
    ledger["source_hash"] = "c" * 64
    atomic_write_json(paths.accepted_repair_ledger, ledger)
    assert load_accepted_repair_request(paths) is None


def test_probe_completes_before_parallel_analysis_helpers_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    run_dir = tmp_path / "run"
    completed: set[str] = set()
    starts: list[str] = []
    lock = Lock()
    parallel_ready = Barrier(3, timeout=2.0)

    monkeypatch.setattr("reaction_remix.orchestrator.runner.validate_runtime_requirements", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "reaction_remix.orchestrator.runner.outputs_valid",
        lambda _paths, stage, **_kwargs: stage in completed,
    )

    def fake_executor(command: list[str], _log_path: Path) -> None:
        stage = command[command.index("-m") + 1].rsplit(".", 1)[-1]
        if stage == "probe":
            completed.add(stage)
            return
        assert stage in {"analyze", "shots", "stems"}
        with lock:
            assert "probe" in completed
            starts.append(stage)
        parallel_ready.wait()
        with lock:
            completed.add(stage)

    assert run_pipeline(
        reaction_args(film, run_dir, to_stage="stems"),
        config=load_config(None),
        executor=fake_executor,
    ) == 0
    assert set(starts) == {"analyze", "shots", "stems"}


def test_dry_run_never_invokes_stage_executor(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    run_dir = tmp_path / "run"

    def forbidden_executor(_command: list[str], _log_path: Path) -> None:
        raise AssertionError("dry-run must not execute a stage subprocess")

    assert run_pipeline(
        reaction_args(film, run_dir, dry_run=True),
        config=load_config(None),
        executor=forbidden_executor,
    ) == 0
    assert not run_dir.exists()


def test_valid_probe_resume_reuses_unchanged_artifact(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    source = make_source(input_path=film.resolve().as_posix(), input_hash=media_identity_hash(film))
    atomic_write_json(paths.reaction_source, source.model_dump(mode="json"))
    original = paths.reaction_source.read_bytes()
    calls = 0

    def resume_executor(_command: list[str], _log_path: Path) -> None:
        nonlocal calls
        calls += 1

    assert run_pipeline(
        reaction_args(film, paths.run_dir, only="probe"),
        config=load_config(None),
        executor=resume_executor,
    ) == 0
    assert calls == 1
    assert paths.reaction_source.read_bytes() == original
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["stages"][0]["status"] == "skipped"
    assert summary["stages"][0]["cache_hit"] is True


def test_corrupt_probe_artifact_is_rerun(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    paths.reaction_source.write_text("{corrupt", encoding="utf-8")
    source = make_source(input_path=film.resolve().as_posix(), input_hash=media_identity_hash(film))
    calls = 0

    def rebuild_executor(_command: list[str], _log_path: Path) -> None:
        nonlocal calls
        calls += 1
        atomic_write_json(paths.reaction_source, source.model_dump(mode="json"))

    assert run_pipeline(
        reaction_args(film, paths.run_dir, only="probe"),
        config=load_config(None),
        executor=rebuild_executor,
    ) == 0
    assert calls == 1
    assert outputs_valid(paths, "probe", config=load_config(None), film=film)
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["stages"][0]["status"] == "completed"


def test_reaction_runner_repairs_only_tts_fit_slots(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    run_dir = tmp_path / "run"
    paths = build_paths(run_dir)
    args = Namespace(
        input=film,
        run_dir=run_dir,
        from_stage=None,
        to_stage=None,
        only="tts",
        force=False,
        force_stage=[],
        dry_run=False,
    )
    config = load_config(None)
    calls: list[tuple[str, bool]] = []
    tts_attempt = 0

    monkeypatch.setattr("reaction_remix.orchestrator.runner.validate_runtime_requirements", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "reaction_remix.orchestrator.runner.outputs_valid",
        lambda _paths, stage, **_kwargs: stage != "tts" or tts_attempt >= 2,
    )

    def fake_executor(command: list[str], log_path: Path) -> None:
        nonlocal tts_attempt
        stage = command[command.index("-m") + 1].rsplit(".", 1)[-1]
        calls.append((stage, "--fit-request" in command))
        if stage != "tts":
            return
        tts_attempt += 1
        paths.commentary_fit_requests.parent.mkdir(parents=True, exist_ok=True)
        requests = [{"slot_id": "commentary-slot-0001"}] if tts_attempt == 1 else []
        paths.commentary_fit_requests.write_text(json.dumps({"requests": requests}), encoding="utf-8")
        if requests:
            raise StageExecutionError("fit required", returncode=3, log_path=log_path)

    assert run_pipeline(args, config=config, executor=fake_executor) == 0
    assert calls == [("tts", False), ("write", True), ("tts", True)]
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["repair_rounds"][0]["kind"] == "tts_fit"


def test_tts_fit_repair_is_capped_at_two_rounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    paths = build_paths(tmp_path / "run")
    calls: list[str] = []

    monkeypatch.setattr("reaction_remix.orchestrator.runner.validate_runtime_requirements", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "reaction_remix.orchestrator.runner.outputs_valid",
        lambda _paths, stage, **_kwargs: stage != "tts",
    )

    def always_too_long(command: list[str], log_path: Path) -> None:
        stage = command[command.index("-m") + 1].rsplit(".", 1)[-1]
        calls.append(stage)
        if stage == "tts":
            paths.commentary_fit_requests.parent.mkdir(parents=True, exist_ok=True)
            paths.commentary_fit_requests.write_text(
                json.dumps({"requests": [{"slot_id": "commentary-slot-0001"}]}),
                encoding="utf-8",
            )
            raise StageExecutionError("fit required", returncode=3, log_path=log_path)

    with pytest.raises(ReactionOrchestratorError, match="maximum repair rounds"):
        run_pipeline(
            reaction_args(film, paths.run_dir, only="tts"),
            config=load_config(None),
            executor=always_too_long,
        )

    assert calls == ["tts", "write", "tts", "write", "tts"]
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert [item["round"] for item in summary["repair_rounds"]] == [1, 2]


def test_qa_repair_routes_duration_leakage_and_media_cache_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    tts_path = paths.run_dir / "audio" / "slot-0001.mp3"
    tts_path.parent.mkdir()
    tts_path.write_bytes(b"tts")

    source = make_source(input_path=film.resolve().as_posix())
    blocks = make_blocks()
    plan = make_plan().model_copy(
        update={
            "excluded_blocks": [
                *make_plan().excluded_blocks,
                RemixExcludedBlock(
                    block_id="block-0003",
                    reason="Candidate reaction for duration repair.",
                    category="duplicate_reaction",
                    source_duration_s=10.0,
                    unique_reaction_speech_s=10.0,
                ),
            ],
            "semantic_annotations": [
                RemixSemanticAnnotation(
                    block_id="block-0003",
                    summary_ja="復元候補の反応",
                    intensity=0.9,
                    novelty=0.9,
                )
            ],
        }
    )
    commentary_audio = make_commentary_audio(tts_path)
    second_audio_item = commentary_audio.items[0].model_copy(update={"slot_id": "slot-0002"})
    commentary_audio = commentary_audio.model_copy(
        update={
            "items": [*commentary_audio.items, second_audio_item],
            "total_commentary_duration_s": commentary_audio.total_commentary_duration_s
            + second_audio_item.duration_s,
        }
    )
    edl, compose_repair = compose_remix(
        film_path=film,
        source=source,
        blocks=blocks,
        plan=plan,
        commentary_audio=commentary_audio,
        commentary_audio_base=paths.run_dir,
        plan_hash="2" * 64,
        commentary_audio_hash="3" * 64,
    )
    assert compose_repair is None
    atomic_write_json(paths.reaction_source, source.model_dump(mode="json"))
    atomic_write_json(paths.remix_plan, plan.model_dump(mode="json"))
    atomic_write_json(paths.commentary_audio, commentary_audio.model_dump(mode="json"))
    atomic_write_json(paths.remix_edl, edl.model_dump(mode="json"))

    qa_attempt = 0
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("reaction_remix.orchestrator.runner.validate_runtime_requirements", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("reaction_remix.orchestrator.runner.outputs_valid", lambda *_args, **_kwargs: True)

    def repair_executor(command: list[str], log_path: Path) -> None:
        nonlocal qa_attempt
        stage = command[command.index("-m") + 1].rsplit(".", 1)[-1]
        calls.append((stage, command))
        if stage == "compose" and "--repair-overrides" in command:
            original_reaction = edl.placements[0]
            original_commentary = edl.placements[1]
            inserted = original_reaction.model_copy(
                update={
                    "placement_id": "placement-0000",
                    "item_id": "item-repaired-duration",
                    "origin_block_id": "block-0003",
                    "tl_start": 0.0,
                    "tl_end": 10.0,
                    "video": original_reaction.video.model_copy(update={"src_in": 90.0, "src_out": 100.0}),
                    "audio": original_reaction.audio.model_copy(
                        update={"source_in": 90.0, "source_out": 100.0}
                    ),
                }
            )
            shifted_reaction = original_reaction.model_copy(
                update={
                    "placement_id": "placement-0001",
                    "tl_start": 10.0,
                    "tl_end": 90.0,
                }
            )
            shifted_commentary = original_commentary.model_copy(
                update={
                    "placement_id": "placement-0002",
                    "tl_start": 90.0,
                    "tl_end": 95.0,
                }
            )
            repaired_edl = edl.model_copy(
                update={
                    "placements": [inserted, shifted_reaction, shifted_commentary],
                    "total_duration_s": 95.0,
                }
            )
            atomic_write_json(paths.remix_edl, repaired_edl.model_dump(mode="json"))
            return
        if stage != "qa":
            return
        qa_attempt += 1
        if qa_attempt == 1:
            paths.remix_qa.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "duration": {"status": "fail", "output_s": 70.0},
                        "commentary": {
                            "old_narrator_leakage_count": 1,
                            "old_narrator_leakage_slot_ids": ["slot-0001"],
                        },
                        "reaction_preservation": {
                            "status": "fail",
                            "failed_placement_ids": ["placement-0000"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            raise StageExecutionError("QA gate failed", returncode=1, log_path=log_path)
        paths.remix_qa.write_text(
            json.dumps(
                passing_qa_payload(
                    source_hash=source.input_hash,
                    edl_hash=file_hash(paths.remix_edl) or "",
                    output_path=paths.output_video,
                )
            ),
            encoding="utf-8",
        )

    assert run_pipeline(
        reaction_args(film, paths.run_dir),
        config=load_config(None),
        executor=repair_executor,
    ) == 0

    repairs = json.loads(paths.repair_requests.read_text(encoding="utf-8"))
    by_kind = {item["kind"]: item for item in repairs["items"]}
    assert set(by_kind) == {"duration_restore", "bed_leakage", "reaction_media_mismatch"}
    assert by_kind["duration_restore"]["affected_ids"] == ["block-0003"]
    assert by_kind["bed_leakage"]["affected_ids"] == ["slot-0001"]
    assert by_kind["reaction_media_mismatch"]["affected_ids"] == ["placement-0001"]

    repair_commands = {stage: command for stage, command in calls if "--repair-request" in command}
    assert "--repair-request" in repair_commands["plan"]
    assert "--repair-request" in repair_commands["render"]
    repaired_compose = next(command for stage, command in reversed(calls) if stage == "compose")
    assert "--repair-overrides" in repaired_compose
    repaired_qa = next(command for stage, command in reversed(calls) if stage == "qa")
    assert "--repair-requests" in repaired_qa
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["repair_rounds"] == [
        {
            "round": 1,
            "kind": "qa_repair",
            "request": str(paths.repair_requests),
            "stages": ["compose", "plan", "qa", "render", "tts", "write"],
        }
    ]

    accepted_request = load_accepted_repair_request(paths)
    assert accepted_request is not None
    accepted_payload = json.loads(accepted_request.read_text(encoding="utf-8"))
    assert {item["kind"] for item in accepted_payload["items"]} == {
        "duration_restore",
        "bed_leakage",
    }
    assert all(item["kind"] != "reaction_media_mismatch" for item in accepted_payload["items"])

    resumed_commands: list[list[str]] = []

    def resume_executor(command: list[str], _log_path: Path) -> None:
        resumed_commands.append(command)

    assert run_pipeline(
        reaction_args(film, paths.run_dir, only="plan"),
        config=load_config(None),
        executor=resume_executor,
    ) == 0
    assert run_pipeline(
        reaction_args(film, paths.run_dir, only="compose"),
        config=load_config(None),
        executor=resume_executor,
    ) == 0
    resumed_plan, resumed_compose = resumed_commands
    assert resumed_plan[resumed_plan.index("--repair-request") + 1] == str(accepted_request)
    assert "--repair-overrides" in resumed_compose
    assert resumed_compose[resumed_compose.index("--repair-overrides") + 1] == str(accepted_request)
    assert "--repair-request" not in resumed_compose

    paths.remix_qa.write_text(
        json.dumps(
            {
                "duration": {"status": "pass", "output_s": 85.0},
                "commentary": {"old_narrator_leakage_count": 0},
                "reaction_preservation": {"status": "fail", "failed_placement_ids": []},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReactionOrchestratorError, match="without localized placement IDs"):
        _build_qa_repairs(paths, repair_round=1)


def test_pending_fit_requests_invalidate_tts_resume(tmp_path) -> None:
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    script = make_commentary_script()
    atomic_write_json(paths.commentary_script, script.model_dump(mode="json"))
    public_audio = paths.run_dir / "audio" / "slot-0001.mp3"
    public_audio.parent.mkdir()
    public_audio.write_bytes(b"audio")
    script_hash = file_hash(paths.commentary_script)
    audio = make_commentary_audio(public_audio, script_hash=script_hash or "")
    audio = audio.model_copy(
        update={
            "items": [
                audio.items[0].model_copy(
                    update={
                        "audio_path": public_audio.relative_to(paths.run_dir).as_posix(),
                        "audio_sha256": file_hash(public_audio),
                    }
                )
            ]
        }
    )
    atomic_write_json(paths.commentary_audio, audio.model_dump(mode="json"))
    pending = CommentaryFitRequests.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": script.source_hash,
            "script_hash": script_hash,
            "requests": [
                {
                    "slot_id": "slot-0001",
                    "actual_duration_s": 7.0,
                    "target_duration_s": 5.0,
                    "max_duration_s": 6.0,
                    "tolerance_s": 0.1,
                    "direction": "shorten",
                    "attempt": 1,
                    "reason": "too long",
                }
            ],
            "created_at": "2026-01-01T00:00:00Z",
            "warnings": [],
        }
    )
    atomic_write_json(paths.commentary_fit_requests, pending.model_dump(mode="json"))

    assert outputs_valid(paths, "tts", config=load_config(None)) is False
    atomic_write_json(
        paths.commentary_fit_requests,
        pending.model_copy(update={"requests": []}).model_dump(mode="json"),
    )
    assert outputs_valid(paths, "tts", config=load_config(None)) is True


def test_probe_artifact_rejects_vfr_when_v1_config_requires_cfr(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"source")
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    source = make_source(input_path=film.resolve().as_posix(), input_hash=media_identity_hash(film))
    source = source.model_copy(update={"video": source.video.model_copy(update={"frame_rate_mode": "vfr"})})
    atomic_write_json(paths.reaction_source, source.model_dump(mode="json"))

    assert outputs_valid(paths, "probe", config=load_config(None), film=film) is False


def test_segment_outputs_invalidate_old_algorithm_and_changed_config(tmp_path: Path) -> None:
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    atomic_write_json(paths.reaction_source, make_source().model_dump(mode="json"))
    atomic_write_json(paths.reaction_transcript, make_transcript().model_dump(mode="json"))
    args = Namespace(
        source=paths.reaction_source,
        transcript=paths.reaction_transcript,
        shots=None,
        output=paths.reaction_blocks,
        review_html=paths.blocks_review_html,
        work_dir=paths.run_dir / "work" / "segment",
        min_silence_s=0.25,
        speech_padding_s=0.12,
        scene_window_s=0.5,
        min_cut_spacing_s=0.08,
        commentary_min_confidence=0.90,
        narrator_min_regions=3,
        narrator_min_japanese_ratio=0.90,
        broll_gap_s=1.5,
        boundary_policy="strict-or-word-edge",
        force=True,
    )
    assert run_segment(args) == 0
    config = load_config(None)
    assert outputs_valid(paths, "segment", config=config)

    meta_path = paths.reaction_blocks.with_name("reaction_blocks.meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["algorithm_version"] = "reaction-segment-v6"
    atomic_write_json(meta_path, meta)
    assert outputs_valid(paths, "segment", config=config) is False

    assert run_segment(args) == 0
    changed_config = load_config(None)
    changed_config["segment"]["speech_padding_s"] = 0.2
    assert outputs_valid(paths, "segment", config=changed_config) is False


def test_force_flags_preserve_item_caches_and_never_reach_qa_parser() -> None:
    assert _pass_force_flag("qa", requested_force=True, global_force=True, explicit_force_stages=["qa"]) is False
    assert _pass_force_flag("tts", requested_force=True, global_force=False, explicit_force_stages=["plan"]) is False
    assert _pass_force_flag("tts", requested_force=True, global_force=False, explicit_force_stages=["tts"]) is True
    assert _pass_force_flag("render", requested_force=True, global_force=False, explicit_force_stages=["render"]) is False
