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
    if stage == "preflight":
        output = flag(command, "--output")
        output.write_text(json.dumps({"input_path":"film.mp4","duration_s":2,"intro":{"detected":False,"confidence":0,"reasons":[]},"non_story_ranges":[],"classifier":"heuristic","created_at":NOW,"warnings":[],"cache_hits":[]}), encoding="utf-8")
    elif stage == "ingest":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"id":0,"type":"speech","tc_start":0,"tc_end":2,"ko":"ì•ˆë…•","en":"hello","scene_desc":None}]), encoding="utf-8")
        output.with_name("film_map.meta.json").write_text(json.dumps({"input_path":"film.mp4","duration":2,"created_at":NOW,"whisper_model":"large-v3","translate_model":"gpt-4.1-mini","vision_model":"gpt-4.1-mini","gap_threshold":4,"max_vision_frames":200,"speech_count":1,"visual_count":0,"cache_hits":[],"warnings_count":0}), encoding="utf-8")
    elif stage == "storymap":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"section_id":0,"type":"setup","tc_start":0,"tc_end":2,"segment_ids":[0],"summary":"setup","characters":[],"locations":[],"events":["setup"],"confidence":0.8,"warnings":[]}]), encoding="utf-8")
        output.with_name("story_map.meta.json").write_text(json.dumps({"film_map_path":"film_map.json","video_profile_path":None,"content_type":"movie","duration_s":2,"n_sections":1,"n_non_story":0,"created_at":NOW,"cache_hits":[],"warnings":[]}), encoding="utf-8")
        flag(command, "--output-qa").write_text(json.dumps({"n_sections":1,"n_non_story":0,"warnings":[],"section_warnings":[]}), encoding="utf-8")
    elif stage == "review":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"beat_id":0,"narration":"Má»Ÿ Ä‘áº§u","from_seg_id":0,"to_seg_id":0,"src_tc_start":0,"src_tc_end":2,"is_hook":True}]), encoding="utf-8")
        output.with_name("review_script.meta.json").write_text(json.dumps({"glossary":[],"target_video_s":2,"char_budget":30,"est_total_chars":6,"coverage_pct":1,"qa_report":[],"n_qa_iterations":0,"model_versions":{},"created_at":NOW,"warnings":[],"cache_hits":[]}), encoding="utf-8")
        if "--review-intent-output" in command:
            flag(command, "--review-intent-output").write_text(json.dumps([{"beat_id":0,"story_section_id":0,"story_section_type":"setup","visual_intent":"character_intro","chronology_mode":"ordered","warnings":[]}]), encoding="utf-8")
    elif stage == "tts":
        audio = flag(command, "--output-audio")
        timing = flag(command, "--output-timing")
        audio.write_bytes(b"voice")
        timing.write_text(json.dumps([{"beat_id":0,"audio_path":"audio/0.mp3","tl_start":0,"tl_end":2,"duration":2}]), encoding="utf-8")
        timing.with_name("tts_script.json").write_text(json.dumps([{"beat_id":0,"original_text":"M? ??u","tts_text":"M? ??u","changed":False,"rules_applied":[],"warnings":[]}]), encoding="utf-8")
        timing.with_name("tts_normalization_report.json").write_text(json.dumps({"mode":"vi","pronunciation_lexicon_path":None,"n_items":1,"n_changed":0,"warnings":[]}), encoding="utf-8")
        timing.with_name("tts_meta.json").write_text(json.dumps({"voice_id":"voice","provider_mode":"ai33","model":"eleven_multilingual_v2","speed":1,"inter_beat_pause_s":0.15,"total_duration_s":2,"film_duration_s":2,"real_ratio":1,"total_chars":6,"est_cost":0,"created_at":NOW,"cache_hits":[],"warnings":[]}), encoding="utf-8")
    elif stage == "shots":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"src":"film.mp4","index":0,"tc_start":0,"tc_end":2,"duration":2,"thumb":"shots/film-000.jpg","motion_score":0.5,"face_count":0,"face_area":0,"brightness":0.5,"is_usable":True}]), encoding="utf-8")
        output.with_name("shots.meta.json").write_text(json.dumps({"src":"film.mp4","duration_s":2,"n_shots":1,"n_usable":1,"detector":"adaptive","feature_config":{},"model_versions":{},"created_at":NOW,"cache_hits":[],"warnings":[]}), encoding="utf-8")
    elif stage == "match":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"tl_start":0,"tl_end":2,"src":"film.mp4","src_in":0,"src_out":2,"beat_id":0,"shot_index":0,"reused":False,"speed":1}]), encoding="utf-8")
        output.with_name("edl.meta.json").write_text(json.dumps({"total_duration_s":2,"n_placements":1,"n_beats_widened":0,"n_reused":0,"n_speedfit":0,"avg_clip_len":2,"coverage_ok":True,"warnings":[],"seed":1234,"created_at":NOW,"cache_hits":[]}), encoding="utf-8")
        output.with_name("edl.qa.json").write_text(json.dumps({"version":1,"semantic_enabled":True,"min_semantic_score":0.12,"beats":[]}), encoding="utf-8")
        flag(command, "--output-sync-qa").write_text(json.dumps({"version":1,"summary":{},"beats":[]}), encoding="utf-8")
        flag(command, "--output-review-html").write_text("<html>review</html>", encoding="utf-8")
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
    assert set(calls) == {"preflight", "ingest", "storymap", "review", "tts", "shots", "match", "render"}
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

def test_review_command_disables_micro_beats_by_default(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    command = build_command("review", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert "--no-micro-beats" in command
    assert "--micro-beats" not in command

def test_tts_command_passes_normalization_options(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    args = argset(tmp_path)
    path = args.config
    data = json.loads(path.read_text(encoding="utf-8"))
    lexicon = tmp_path / "lexicon.json"
    lexicon.write_text("{}", encoding="utf-8")
    data["tts"].update({
        "text_normalization": "vi",
        "pronunciation_lexicon": str(lexicon),
        "normalized_script_output": str(tmp_path / "custom_tts_script.json"),
        "normalization_report": str(tmp_path / "custom_tts_report.json"),
    })
    path.write_text(json.dumps(data), encoding="utf-8")
    paths = build_paths(tmp_path / "run")
    config = load_config(path)

    command = build_command("tts", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert command[command.index("--tts-text-normalization") + 1] == "vi"
    assert command[command.index("--tts-pronunciation-lexicon") + 1] == str(lexicon)
    assert command[command.index("--tts-normalized-script-output") + 1] == str(tmp_path / "custom_tts_script.json")
    assert command[command.index("--tts-normalization-report") + 1] == str(tmp_path / "custom_tts_report.json")

def test_match_command_uses_movie_chronological_defaults(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    command = build_command("match", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--match-strategy") + 1] == "chronological"
    assert command[command.index("--w-semantic") + 1] == "0.15"
    assert command[command.index("--max-source-drift-s") + 1] == "12.0"
    assert command[command.index("--near-repeat-guard-s") + 1] == "6.0"
    assert command[command.index("--opening-near-repeat-guard-s") + 1] == "10.0"
    assert command[command.index("--near-repeat-min-alternative-score-ratio") + 1] == "0.65"
    assert "--opening-story-visual-start" in command
    assert "--ordered-fill-by-audio-progress" in command

def test_render_command_passes_bgm_and_caption_options(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    args = argset(tmp_path)
    path = args.config
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"bgm")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["render"] = {
        "bgm": {"enabled": True, "path": str(bgm), "gain_db": -18, "fade_in_s": 1, "fade_out_s": 2, "ducking": "none"},
        "captions": {"enabled": True, "font_name": "Arial", "font_size": 50, "margin_v": 60, "outline": 3, "max_chars_per_line": 40, "max_lines": 2},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    paths.review_micro.write_text("[]", encoding="utf-8")
    paths.tts_align.write_text("{}", encoding="utf-8")
    config = load_config(path)
    command = build_command("render", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--bgm") + 1] == str(bgm)
    assert "--captions" in command
    assert command[command.index("--review-script") + 1] == str(paths.review_script)
    assert command[command.index("--review-micro") + 1] == str(paths.review_micro)
    assert command[command.index("--caption-font-size") + 1] == "50"

def test_episode_config_keeps_hybrid_match_defaults(tmp_path: Path) -> None:
    args = argset(tmp_path)
    path = args.config
    data = json.loads(path.read_text(encoding="utf-8"))
    data["review"]["content_type"] = "episode"
    path.write_text(json.dumps(data), encoding="utf-8")
    config = load_config(path)
    assert config["match"]["match_strategy"] == "hybrid"
    assert config["match"]["w_semantic"] == 0.45



def test_vi_low_openai_preset_has_no_openai_uses() -> None:
    from orchestrator.config import load_config
    from orchestrator.cost_policy import resolve_cost_policy

    config = load_config(Path("config.vi.low_openai.yaml"))
    _resolved, policy = resolve_cost_policy(config)

    assert policy.quality_mode == "low_cost"
    assert policy.stages["ingest"]["openai_uses"] == []


def test_balanced_auto_fallback_reruns_ingest_on_approximate_timecodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    path = args.config
    data = json.loads(path.read_text(encoding="utf-8"))
    data["orchestrator"] = {"quality_mode": "balanced", "auto_fallback": True, "api_budget_guard": "warn"}
    data["ingest"] = {"source_language": "vi", "translate_mode": "none", "asr_policy": "local_first", "asr_provider": "faster-whisper", "aligner": "whisperx", "max_vision_frames": 0}
    path.write_text(json.dumps(data), encoding="utf-8")
    ingest_calls = 0
    calls: list[str] = []

    def fake_executor(command: list[str], log_path: Path) -> None:
        nonlocal ingest_calls
        stage = stage_name(command)
        calls.append(stage)
        if stage == "ingest":
            ingest_calls += 1
            output = flag(command, "--output")
            output.write_text(json.dumps([{"id":0,"type":"speech","tc_start":0,"tc_end":2,"ko":"hello","en":"hello","scene_desc":None}]), encoding="utf-8")
            meta = {"input_path":"film.mp4","duration":2,"created_at":NOW,"whisper_model":"large-v3","translate_model":"gpt-4.1-mini","vision_model":"gpt-4.1-mini","gap_threshold":4,"max_vision_frames":0,"speech_count":1,"visual_count":0,"cache_hits":[],"warnings_count":0,"timecode_quality":"approximate","approximate_timecodes":True,"asr_provider":"faster-whisper"}
            if ingest_calls == 2:
                meta.update({"timecode_quality":"strict","approximate_timecodes":False,"asr_provider":"openai-gpt4o-hybrid"})
            output.with_name("film_map.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        else:
            write_stage_outputs(command)

    assert run_pipeline(args, executor=fake_executor) == 0
    assert calls.count("ingest") == 2
    assert "storymap" in calls and "render" in calls
    fallback = json.loads((tmp_path / "run" / "fallback_plan.json").read_text(encoding="utf-8"))
    assert fallback["triggered"] is True
    cost = json.loads((tmp_path / "run" / "cost_summary.json").read_text(encoding="utf-8"))
    assert cost["openai_fallback_triggered"] is True


def test_low_cost_blocks_required_openai_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    path = args.config
    data = json.loads(path.read_text(encoding="utf-8"))
    data["orchestrator"] = {"quality_mode": "low_cost", "auto_fallback": True, "api_budget_guard": "block"}
    data["ingest"] = {"source_language": "vi", "translate_mode": "none", "asr_policy": "local_first", "asr_provider": "faster-whisper", "aligner": "whisperx", "max_vision_frames": 0}
    path.write_text(json.dumps(data), encoding="utf-8")

    def fake_executor(command: list[str], log_path: Path) -> None:
        if stage_name(command) == "ingest":
            output = flag(command, "--output")
            output.write_text(json.dumps([{"id":0,"type":"speech","tc_start":0,"tc_end":2,"ko":"hello","en":"hello","scene_desc":None}]), encoding="utf-8")
            output.with_name("film_map.meta.json").write_text(json.dumps({"input_path":"film.mp4","duration":2,"created_at":NOW,"whisper_model":"large-v3","translate_model":"gpt-4.1-mini","vision_model":"gpt-4.1-mini","gap_threshold":4,"max_vision_frames":0,"speech_count":1,"visual_count":0,"cache_hits":[],"warnings_count":0,"timecode_quality":"approximate","approximate_timecodes":True,"asr_provider":"faster-whisper"}), encoding="utf-8")
        else:
            write_stage_outputs(command)

    with pytest.raises(Exception, match="OpenAI fallback required but blocked"):
        run_pipeline(args, executor=fake_executor)


def test_balanced_auto_does_not_fallback_for_strict_timecodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    path = args.config
    data = json.loads(path.read_text(encoding="utf-8"))
    data["orchestrator"] = {"quality_mode": "balanced", "auto_fallback": True, "api_budget_guard": "warn"}
    data["ingest"] = {"source_language": "vi", "translate_mode": "none", "asr_policy": "local_first", "asr_provider": "faster-whisper", "aligner": "whisperx", "max_vision_frames": 0}
    path.write_text(json.dumps(data), encoding="utf-8")
    calls: list[str] = []

    def fake_executor(command: list[str], log_path: Path) -> None:
        calls.append(stage_name(command))
        write_stage_outputs(command)
        if stage_name(command) == "ingest":
            meta_path = flag(command, "--output").with_name("film_map.meta.json")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.update({"timecode_quality":"strict","approximate_timecodes":False,"asr_provider":"faster-whisper"})
            meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert run_pipeline(args, executor=fake_executor) == 0
    assert calls.count("ingest") == 1
    fallback = json.loads((tmp_path / "run" / "fallback_plan.json").read_text(encoding="utf-8"))
    assert fallback["triggered"] is False
