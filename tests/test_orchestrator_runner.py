from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from orchestrator.config import load_config
from run import run_pipeline
from visual_index.integrity import PREPROCESSING_VERSION, media_identity_hash, sha256_file, visual_index_config_hash

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
        output.with_name("edl.meta.json").write_text(json.dumps({"total_duration_s":2,"n_placements":1,"n_beats_widened":0,"n_reused":0,"n_speedfit":0,"avg_clip_len":2,"coverage_ok":True,"warnings":[],"seed":1234,"created_at":NOW,"cache_hits":[],"algorithm_version":"2"}), encoding="utf-8")
        output.with_name("edl.qa.json").write_text(json.dumps({"version":1,"semantic_enabled":True,"min_semantic_score":0.12,"beats":[]}), encoding="utf-8")
        flag(command, "--output-sync-qa").write_text(json.dumps({"version":1,"summary":{},"beats":[]}), encoding="utf-8")
        flag(command, "--output-visual-qa").write_text(json.dumps({"version":1,"visual_mode":"off","visual_enabled":False,"beats":[]}), encoding="utf-8")
        flag(command, "--output-review-html").write_text("<html>review</html>", encoding="utf-8")
    elif stage == "visual_index":
        output = flag(command, "--output")
        film = flag(command, "--film")
        shots_path = flag(command, "--shots")
        asset_dir = flag(command, "--asset-dir")
        pooled_embedding = asset_dir / "emb" / "shot_000000_pool.f16.npy"
        embedding_mode = command[command.index("--embedding-mode") + 1]
        embedding_model = command[command.index("--embedding-model") + 1]
        keyframes_per_shot = int(command[command.index("--keyframes-per-shot") + 1])
        frame_sampling = command[command.index("--frame-sampling") + 1]
        keyframes = []
        for index in range(keyframes_per_shot):
            frame_path = asset_dir / "frames" / f"shot_000000_k{index}.jpg"
            keyframe_embedding = asset_dir / "emb" / f"shot_000000_k{index}.f16.npy"
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            keyframe_embedding.parent.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(b"jpg")
            np.save(keyframe_embedding, np.asarray([1.0, 0.0], dtype=np.float16))
            keyframes.append({"frame_path":frame_path.relative_to(output.parent).as_posix(),"tc":0.5 + index,"role":f"k{index}","embedding_ref":keyframe_embedding.relative_to(output.parent).as_posix(),"embedding_sha256":sha256_file(keyframe_embedding)})
        np.save(pooled_embedding, np.asarray([1.0, 0.0], dtype=np.float16))
        config_hash = visual_index_config_hash(
            film_path=film,
            shots_path=shots_path,
            embedding_mode=embedding_mode,
            embedding_model=embedding_model,
            keyframes_per_shot=keyframes_per_shot,
            frame_sampling=frame_sampling,
        )
        output.write_text(json.dumps({"meta":{"version":"1.1","src":str(film),"embedding_mode":embedding_mode,"embedding_model":embedding_model,"device":"cpu","embedding_dim":2,"keyframes_per_shot":keyframes_per_shot,"n_shots":1,"created_at":NOW,"cache_hits":[],"warnings":[],"film_hash":media_identity_hash(film),"shots_hash":sha256_file(shots_path),"config_hash":config_hash,"preprocessing_version":PREPROCESSING_VERSION,"logit_scale":10.0,"logit_bias":-5.0},"shots":[{"shot_index":0,"tc_start":0,"tc_end":2,"duration":2,"is_story":True,"is_usable":True,"keyframes":keyframes,"shot_embedding_ref":pooled_embedding.relative_to(output.parent).as_posix(),"shot_embedding_sha256":sha256_file(pooled_embedding)}]}), encoding="utf-8")
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


def test_stale_match_algorithm_reruns_match_and_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    meta_path = tmp_path / "run" / "edl.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["algorithm_version"] = "1"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    calls: list[str] = []

    run_pipeline(args, executor=lambda command, log_path: (calls.append(stage_name(command)), write_stage_outputs(command)))

    assert calls == ["match", "render"]


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

def test_shots_command_passes_frame_sampling(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    config["shots"]["frame_sampling"] = "batch"

    command = build_command("shots", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert command[command.index("--frame-sampling") + 1] == "batch"

def test_match_command_uses_movie_chronological_defaults(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    command = build_command("match", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--match-strategy") + 1] == "chronological"
    assert command[command.index("--w-semantic") + 1] == "0.15"
    assert command[command.index("--max-source-drift-s") + 1] == "12.0"
    assert "--opening-story-visual-start" in command
    assert "--ordered-fill-by-audio-progress" in command
    assert command[command.index("--visual-mode") + 1] == "off"

def test_visual_config_runs_visual_index_and_passes_to_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    path = args.config
    data = json.loads(path.read_text(encoding="utf-8"))
    data["visual_index"] = {"enabled": True, "embedding_model": "mock", "device": "cpu"}
    data["match"] = {"visual_mode": "rerank"}
    path.write_text(json.dumps(data), encoding="utf-8")
    calls: list[str] = []

    def fake_executor(command: list[str], log_path: Path) -> None:
        calls.append(stage_name(command))
        if stage_name(command) == "match":
            assert "--visual-index" in command
            assert command[command.index("--visual-mode") + 1] == "rerank"
        write_stage_outputs(command)

    assert run_pipeline(args, executor=fake_executor) == 0
    assert "visual_index" in calls
    assert calls.index("visual_index") < calls.index("match")


def test_corrupt_visual_sidecar_reruns_visual_index_and_downstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    data = json.loads(args.config.read_text(encoding="utf-8"))
    data["visual_index"] = {"enabled": True, "embedding_model": "mock", "device": "cpu"}
    data["match"] = {"visual_mode": "rerank"}
    args.config.write_text(json.dumps(data), encoding="utf-8")
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    pooled = tmp_path / "run" / "visual_index" / "emb" / "shot_000000_pool.f16.npy"
    np.save(pooled, np.asarray([0.0, 1.0], dtype=np.float16))
    calls: list[str] = []

    def fake_executor(command: list[str], log_path: Path) -> None:
        calls.append(stage_name(command))
        write_stage_outputs(command)

    run_pipeline(args, executor=fake_executor)

    assert calls == ["visual_index", "match", "render"]


def test_visual_model_change_reruns_visual_index_and_downstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    data = json.loads(args.config.read_text(encoding="utf-8"))
    data["visual_index"] = {"enabled": True, "embedding_model": "mock-v1", "device": "cpu"}
    data["match"] = {"visual_mode": "rerank"}
    args.config.write_text(json.dumps(data), encoding="utf-8")
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    data["visual_index"]["embedding_model"] = "mock-v2"
    args.config.write_text(json.dumps(data), encoding="utf-8")
    calls: list[str] = []

    def fake_executor(command: list[str], log_path: Path) -> None:
        calls.append(stage_name(command))
        write_stage_outputs(command)

    run_pipeline(args, executor=fake_executor)

    assert calls == ["visual_index", "match", "render"]

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
