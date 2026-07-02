from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import pytest

from orchestrator.config import load_config
from run import run_pipeline

NOW = "2026-07-02T00:00:00Z"


def write_config(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "review": {"chatgpt_profile_dir": str(profile)},
        "tts": {"voice_id": "voice", "provider_mode": "ai33"},
    }), encoding="utf-8")
    return path


def argset(tmp_path: Path, **overrides):  # type: ignore[no-untyped-def]
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    args = argparse.Namespace(
        input=film,
        run_dir=tmp_path / "run",
        config=write_config(tmp_path),
        from_stage=None,
        to_stage=None,
        only=None,
        force=False,
        force_stage=[],
        dry_run=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def flag(command: list[str], name: str) -> Path:
    return Path(command[command.index(name) + 1])


def stage_name(command: list[str]) -> str:
    return command[command.index("-m") + 1]


def write_stage_outputs(command: list[str]) -> None:
    stage = stage_name(command)
    if stage == "ingest":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"id":0,"type":"speech","tc_start":0,"tc_end":2,"ko":"ì•ˆë…•","en":"hello","scene_desc":None}]), encoding="utf-8")
        output.with_name("film_map.meta.json").write_text(json.dumps({"input_path":"film.mp4","duration":2,"created_at":NOW,"whisper_model":"large-v3","translate_model":"gpt-4.1-mini","vision_model":"gpt-4.1-mini","gap_threshold":4,"max_vision_frames":200,"speech_count":1,"visual_count":0,"cache_hits":[],"warnings_count":0}), encoding="utf-8")
    elif stage == "review":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"beat_id":0,"narration":"Má»Ÿ Ä‘áº§u","from_seg_id":0,"to_seg_id":0,"src_tc_start":0,"src_tc_end":2,"is_hook":True}]), encoding="utf-8")
        output.with_name("review_script.meta.json").write_text(json.dumps({"glossary":[],"target_video_s":2,"char_budget":30,"est_total_chars":6,"coverage_pct":1,"qa_report":[],"n_qa_iterations":0,"model_versions":{},"created_at":NOW,"warnings":[],"cache_hits":[]}), encoding="utf-8")
    elif stage == "tts":
        audio = flag(command, "--output-audio")
        timing = flag(command, "--output-timing")
        audio.write_bytes(b"voice")
        timing.write_text(json.dumps([{"beat_id":0,"audio_path":"audio/0.mp3","tl_start":0,"tl_end":2,"duration":2}]), encoding="utf-8")
        timing.with_name("tts_meta.json").write_text(json.dumps({"voice_id":"voice","provider_mode":"ai33","model":"eleven_multilingual_v2","speed":1,"inter_beat_pause_s":0.15,"total_duration_s":2,"film_duration_s":2,"real_ratio":1,"total_chars":6,"est_cost":0,"created_at":NOW,"cache_hits":[],"warnings":[]}), encoding="utf-8")
    elif stage == "shots":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"src":"film.mp4","index":0,"tc_start":0,"tc_end":2,"duration":2,"thumb":"shots/film-000.jpg","motion_score":0.5,"face_count":0,"face_area":0,"brightness":0.5,"is_usable":True}]), encoding="utf-8")
        output.with_name("shots.meta.json").write_text(json.dumps({"src":"film.mp4","duration_s":2,"n_shots":1,"n_usable":1,"detector":"adaptive","feature_config":{},"model_versions":{},"created_at":NOW,"cache_hits":[],"warnings":[]}), encoding="utf-8")
    elif stage == "match":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"tl_start":0,"tl_end":2,"src":"film.mp4","src_in":0,"src_out":2,"beat_id":0,"shot_index":0,"reused":False,"speed":1}]), encoding="utf-8")
        output.with_name("edl.meta.json").write_text(json.dumps({"total_duration_s":2,"n_placements":1,"n_beats_widened":0,"n_reused":0,"n_speedfit":0,"avg_clip_len":2,"coverage_ok":True,"warnings":[],"seed":1234,"created_at":NOW,"cache_hits":[]}), encoding="utf-8")
    elif stage == "render":
        output = flag(command, "--output")
        output.write_bytes(b"recap")
        output.with_name("render.meta.json").write_text(json.dumps({"width":1920,"height":1080,"fps":30,"codec":"h264","video_duration_s":2,"audio_duration_s":2,"duration_match":True,"n_placements":1,"n_temp_clips":1,"warnings":[],"created_at":NOW,"cache_hits":[]}), encoding="utf-8")
    else:
        raise AssertionError(stage)


def test_full_pipeline_mock_writes_summary_and_runs_shots_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    started_shots = threading.Event()
    calls: list[str] = []

    def fake_executor(command: list[str], log_path: Path) -> None:
        stage = stage_name(command)
        calls.append(stage)
        if stage == "shots":
            started_shots.set()
            time.sleep(0.05)
        if stage == "ingest":
            assert started_shots.wait(1)
        write_stage_outputs(command)

    assert run_pipeline(argset(tmp_path), executor=fake_executor) == 0
    assert set(calls) == {"ingest", "review", "tts", "shots", "match", "render"}
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["calibrate"] == {"real_ratio": 1, "n_beats_widened": 0, "duration_match": True}
    assert (tmp_path / "run" / "review_meta.json").exists()


def test_rerun_skips_all_valid_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    calls: list[str] = []
    run_pipeline(args, executor=lambda command, log_path: calls.append(stage_name(command)))
    assert calls == []
    summary = json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8"))
    assert {stage["status"] for stage in summary["stages"]} == {"skipped"}


def test_force_stage_match_reruns_match_and_render_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    calls: list[str] = []
    forced = argset(tmp_path, force_stage=["match"])
    run_pipeline(forced, executor=lambda command, log_path: (calls.append(stage_name(command)), write_stage_outputs(command)))
    assert calls == ["match", "render"]


def test_dry_run_does_not_call_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    calls: list[str] = []
    assert run_pipeline(argset(tmp_path, dry_run=True, only="ingest"), executor=lambda command, log_path: calls.append("called")) == 0
    assert calls == []
    assert not (tmp_path / "run" / "summary.json").exists()


def test_missing_env_fails_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    with pytest.raises(Exception, match="OPENAI_API_KEY"):
        run_pipeline(argset(tmp_path, only="ingest"), executor=lambda command, log_path: None)
