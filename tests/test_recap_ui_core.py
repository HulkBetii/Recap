from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from recap_ui.app import _media_duration_checks, _provider_checks
from recap_ui.database import Database
from recap_ui.planning import PlanningError, PlanningService
from recap_ui.qa import build_delivery_qa
from recap_ui.repository import Repository
from recap_ui.runs import RunService
from recap_ui.schemas import DeliveryStatus, EventType, ExecutionPlan, JobKind, JobStatus, PlanDagNode, SeriesPlanRequest, SinglePlanRequest
from recap_ui.security import PathAccessError, PathRegistry


def _plan(tmp_path: Path, plan_id: str) -> ExecutionPlan:
    config = tmp_path / f"{plan_id}.json"
    config.write_text("{}", encoding="utf-8")
    command = ["python", "run.py", "--run-dir", str(tmp_path / plan_id), "--config", str(config)]
    return ExecutionPlan(
        plan_id=plan_id,
        kind=JobKind.SINGLE,
        command=command,
        dry_run_command=[*command, "--dry-run"],
        run_dir=str(tmp_path / plan_id),
        config_path=str(config),
        config_snapshot={},
        dag=[],
        command_hash=PlanningService._command_hash(command),
    )


def test_repository_fifo_events_and_locks(tmp_path: Path) -> None:
    repository = Repository(Database(tmp_path / "ui.db"))
    first = repository.create_job(_plan(tmp_path, "first"))
    second = repository.create_job(_plan(tmp_path, "second"))
    assert Path(first.config_path).name == f"{first.id}.json"
    assert first.argv[first.argv.index("--config") + 1] == first.config_path

    claimed = repository.claim_next_job("worker-a")
    assert claimed is not None
    assert claimed.id == first.id
    assert repository.claim_next_job("worker-b") is None
    assert repository.acquire_lock("pipeline", claimed.id)
    assert not repository.acquire_lock("pipeline", second.id)

    event = repository.append_event(claimed.id, EventType.LOG, {"lines": ["hello"]})
    assert repository.list_events(claimed.id, after_seq=event.seq - 1)[-1].payload == {"lines": ["hello"]}

    repository.update_job(claimed.id, status=JobStatus.SUCCEEDED)
    repository.release_locks(claimed.id)
    next_job = repository.claim_next_job("worker-a")
    assert next_job is not None
    assert next_job.id == second.id


def test_path_registry_rejects_tampering_and_outside_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "file.json"
    child.write_text("{}", encoding="utf-8")
    registry = PathRegistry({"root": ("Root", root)}, b"x" * 32)

    token = registry.token_for(child)
    assert registry.resolve(token, expect="file") == child.resolve()
    with pytest.raises(PathAccessError):
        registry.resolve(token[:-1] + ("0" if token[-1] != "0" else "1"))
    with pytest.raises(PathAccessError):
        registry.token_for(tmp_path / "outside.json")


def test_planning_supports_safe_new_run_child_and_rejects_unsafe_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runs = repo / "runs"
    runs.mkdir()
    source = repo / "episode.mp4"
    source.write_bytes(b"video")
    config = repo / "config.yaml"
    config.write_text("tts:\n  voice_id: test-voice\n", encoding="utf-8")
    registry = PathRegistry({"repo": ("Repo", repo)}, b"y" * 32)

    def fake_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert command[-1] == "--dry-run"
        assert cwd == repo.resolve()
        return subprocess.CompletedProcess(command, 0, "[planned] ingest\n[planned] render\n", "")

    service = PlanningService(repo, repo / "state", registry, runner=fake_runner)
    request = SinglePlanRequest(
        source_token=registry.token_for(source),
        run_parent_token=registry.token_for(runs),
        run_name="episode-01",
        config_token=registry.token_for(config),
        overrides={"render": {"crf": 22}, "tts": {"speed": 1.1}},
    )
    plan = service.plan_single(request)

    assert plan.can_start
    assert Path(plan.run_dir) == (runs / "episode-01").resolve()
    assert Path(plan.config_path).is_file()
    assert plan.config_snapshot["render"]["crf"] == 22
    assert service.get_plan(plan.plan_id) == plan

    bad = request.model_copy(update={"overrides": {"review": {"llm_backend": "openai_api"}}})
    with pytest.raises(PlanningError, match="unsafe override"):
        service.plan_single(bad)


def test_series_plan_validates_duplicate_sources(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runs = repo / "runs"
    runs.mkdir()
    source = repo / "episode.mp4"
    source.write_bytes(b"video")
    manifest = repo / "series_manifest.yaml"
    manifest.write_text(
        "series_id: demo\nepisodes:\n"
        "  - episode_key: s01e01\n    episode_number: 1\n    source_path: episode.mp4\n"
        "  - episode_key: s01e02\n    episode_number: 2\n    source_path: episode.mp4\n",
        encoding="utf-8",
    )
    config = repo / "config.yaml"
    config.write_text("tts:\n  voice_id: test-voice\n", encoding="utf-8")
    registry = PathRegistry({"repo": ("Repo", repo)}, b"z" * 32)
    service = PlanningService(
        repo,
        repo / "state",
        registry,
        runner=lambda command, cwd: subprocess.CompletedProcess(command, 0, "[planned] series_composer\n", ""),
    )

    plan = service.plan_series(
        SeriesPlanRequest(
            manifest_token=registry.token_for(manifest),
            run_parent_token=registry.token_for(runs),
            run_name="demo-season",
            config_token=registry.token_for(config),
        )
    )

    assert not plan.can_start
    duplicate = next(check for check in plan.checks if check.code == "duplicate_sources")
    assert duplicate.status == DeliveryStatus.BLOCK


def test_runtime_preflight_checks_tts_provider_and_media_duration(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"video")
    command = ["python", "run.py", "--input", str(source)]
    plan = ExecutionPlan(
        plan_id="runtime-preflight",
        kind=JobKind.SINGLE,
        command=command,
        dry_run_command=[*command, "--dry-run"],
        run_dir=str(tmp_path / "run"),
        config_path=str(tmp_path / "config.json"),
        config_snapshot={},
        dag=[PlanDagNode(key="tts", label="TTS", status="planned")],
        command_hash=PlanningService._command_hash(command),
    )
    config = {"tts": {"provider_mode": "auto", "voice_id": "voice", "openai_voice": "coral"}}

    blocked = next(check for check in _provider_checks(config, {"openai": False, "vivoo": False, "genmax": False}, plan) if check.code == "tts_provider_ready")
    assert blocked.status == DeliveryStatus.BLOCK
    ready = next(check for check in _provider_checks(config, {"openai": True, "vivoo": False, "genmax": False}, plan) if check.code == "tts_provider_ready")
    assert ready.status == DeliveryStatus.PASS

    monkeypatch.setattr("recap_ui.app.probe_duration", lambda _path: 123.456)
    media = _media_duration_checks(plan)[0]
    assert media.status == DeliveryStatus.PASS
    assert media.details["sources"] == [{"name": "episode.mp4", "duration_s": 123.456}]


def test_series_delivery_qa_accepts_actual_duration_over_hard_cap(tmp_path: Path) -> None:
    run, fake_probe = _series_delivery_fixture(tmp_path, duration=3100.0)

    report = build_delivery_qa(run, JobKind.SERIES, media_probe=fake_probe)

    assert report.status == DeliveryStatus.PASS
    assert report.metrics["minimum_duration_s"] == 2100.0
    assert report.metrics["maximum_duration_s"] == 2700.0
    assert report.metrics["hard_cap_s"] == 3000.0
    minimum = next(check for check in report.checks if check.code == "minimum_duration")
    maximum = next(check for check in report.checks if check.code == "maximum_duration")
    hard_cap = next(check for check in report.checks if check.code == "hard_cap")
    assert minimum.status == DeliveryStatus.PASS
    assert maximum.status == DeliveryStatus.PASS
    assert hard_cap.status == DeliveryStatus.PASS
    assert "overage is accepted" in maximum.message
    assert "overage is accepted" in hard_cap.message


def test_series_delivery_qa_blocks_actual_duration_below_minimum(tmp_path: Path) -> None:
    run, fake_probe = _series_delivery_fixture(tmp_path, duration=2000.0)

    report = build_delivery_qa(run, JobKind.SERIES, media_probe=fake_probe)

    assert report.status == DeliveryStatus.BLOCK
    minimum = next(check for check in report.checks if check.code == "minimum_duration")
    assert minimum.status == DeliveryStatus.BLOCK
    assert "below the required minimum" in minimum.message
    assert next(check for check in report.checks if check.code == "maximum_duration").status == DeliveryStatus.PASS
    assert next(check for check in report.checks if check.code == "hard_cap").status == DeliveryStatus.PASS


def _series_delivery_fixture(tmp_path: Path, *, duration: float):  # type: ignore[no-untyped-def]
    run = tmp_path / "series-run"
    episode = run / "s01e01"
    final = run / "series_recap"
    episode.mkdir(parents=True)
    final.mkdir()
    _write_json(
        episode / "film_map.meta.json",
        {
            "translation_required": True,
            "translation_min_success_ratio": 0.95,
            "translation_success_ratio": 1.0,
            "approximate_timecodes": False,
        },
    )
    _write_json(
        episode / "shots.json",
        [{"index": 0, "is_story": True, "is_usable": True, "is_end_credit": False}],
    )
    _write_json(
        final / "series_composer.qa.json",
        {
            "estimated_duration_s": 2900.0,
            "target_total_min_s": 2100.0,
            "target_total_max_s": 2700.0,
            "target_total_hard_cap_s": 3000.0,
            "n_events": 1,
            "prompt_count": 1,
            "revision_count": 0,
            "arc_count": 1,
            "qa_report": [],
        },
    )
    _write_json(
        final / "series_arc_plan.json",
        {
            "episode_count": 1,
            "target_total_min_s": 2100.0,
            "target_total_max_s": 2700.0,
            "target_total_hard_cap_s": 3000.0,
            "arcs": [{"episode_keys": ["s01e01"]}],
        },
    )
    _write_json(final / "series_chapters.json", [{"episode_key": "s01e01", "start_beat_id": 0, "title": "Episode 1"}])
    _write_json(final / "beats_timing.json", [{"beat_id": 0, "tl_start": 0.0, "tl_end": duration}])
    _write_json(
        final / "edl.json",
        [{"beat_id": 0, "shot_index": 0, "src": "s01e01/episode.mp4", "tl_start": 0.0, "tl_end": duration}],
    )
    _write_json(final / "edl.meta.json", {"coverage_ok": True, "total_duration_s": duration})
    _write_json(final / "edl.source_map.json", {"sources": {"s01e01/episode.mp4": "episode.mp4"}})
    _write_json(final / "tts_meta.json", {"total_duration_s": duration})
    _write_json(final / "render.meta.json", {"video_duration_s": duration, "audio_duration_s": duration})
    (final / "series_recap.mp4").write_bytes(b"video")
    (final / "voiceover.mp3").write_bytes(b"audio")

    def fake_probe(path: Path) -> dict[str, float | int]:
        if path.suffix == ".mp3":
            return {"width": 0, "height": 0, "fps": 0.0, "duration": duration, "audio_streams": 1, "audio_duration": duration}
        return {"width": 1920, "height": 1080, "fps": 30.0, "duration": duration, "audio_streams": 1, "audio_duration": duration}

    return run, fake_probe


def test_run_service_indexes_artifacts_with_opaque_ids(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run = repo / "runs" / "demo"
    run.mkdir(parents=True)
    (run / "recap.mp4").write_bytes(b"video")
    thumbs = run / "shots"
    thumbs.mkdir()
    for index in range(10):
        (thumbs / f"shot-{index:03d}.jpg").write_bytes(b"image")
    _write_json(run / "render.meta.json", {"width": 1920, "height": 1080, "fps": 30, "video_duration_s": 1, "audio_duration_s": 1})
    registry = PathRegistry({"repo": ("Repo", repo)}, b"r" * 32)
    service = RunService(repo, Repository(Database(tmp_path / "ui.db")), registry)

    records = service.discover_runs()
    assert len(records) == 1
    artifacts = service.list_artifacts(records[0].id)
    recap = next(item for item in artifacts if item.name == "recap.mp4")
    assert ":" not in recap.id and "/" not in recap.id
    assert service.resolve_artifact(records[0].id, recap.id) == (run / "recap.mp4").resolve()
    assert service.list_artifacts(records[0].id, limit=1)[0].name == "recap.mp4"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
