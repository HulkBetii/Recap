from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from common.integrity import file_hash
from common.schema import VideoProfile
from ingest.__main__ import build_parser as build_ingest_parser
from ingest.integrity import INGEST_CACHE_VERSION, ingest_config_hash
from orchestrator.config import load_config
from orchestrator.graph import build_paths
from orchestrator.runner import OrchestratorError, build_command, outputs_valid, preflight, validate_runtime_requirements
from preflight.__main__ import build_parser as build_preflight_parser
from preflight.integrity import PREFLIGHT_CACHE_VERSION, preflight_identity
from review.__main__ import build_parser as build_review_parser
from review.integrity import REVIEW_CACHE_VERSION, build_review_identity
from review.style import DEFAULT_STYLE_SAMPLE
from match.version import MATCH_ALGORITHM_VERSION
from run import run_pipeline, should_fallback_timecode, sync_review_fallback_reporting
from storymap.cache import stable_hash as storymap_stable_hash
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
    if not film.exists():
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


def stage_args(command: list[str], parser) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    return parser.parse_args(command[command.index(stage_name(command)) + 1:])


def write_stage_outputs(command: list[str]) -> None:
    stage = stage_name(command)
    if stage == "preflight":
        output = flag(command, "--output")
        args = stage_args(command, build_preflight_parser())
        input_hash, config_hash = preflight_identity(args.input, classifier=args.classifier, max_intro_s=args.max_intro_s, sample_every_s=args.sample_every_s, confidence_threshold=args.confidence_threshold, uncertain_threshold=args.uncertain_threshold)
        output.write_text(json.dumps({"input_path":str(args.input),"duration_s":2,"intro":{"detected":False,"confidence":0,"reasons":[]},"non_story_ranges":[],"classifier":args.classifier,"created_at":NOW,"warnings":[],"cache_hits":[],"input_hash":input_hash,"config_hash":config_hash,"cache_version":PREFLIGHT_CACHE_VERSION}), encoding="utf-8")
    elif stage == "ingest":
        output = flag(command, "--output")
        args = stage_args(command, build_ingest_parser())
        output.write_text(json.dumps([{"id":0,"type":"speech","tc_start":0,"tc_end":2,"ko":"ì•ˆë…•","en":"hello","scene_desc":None}]), encoding="utf-8")
        output.with_name("film_map.meta.json").write_text(json.dumps({"input_path":str(args.input),"duration":2,"created_at":NOW,"whisper_model":args.whisper_model,"translate_model":args.translate_model,"vision_model":args.vision_model,"gap_threshold":args.gap_threshold,"max_vision_frames":args.max_vision_frames,"speech_count":1,"visual_count":0,"cache_hits":[],"warnings_count":0,"input_hash":media_identity_hash(args.input),"config_hash":ingest_config_hash(args),"video_profile_hash":file_hash(args.video_profile),"cache_version":INGEST_CACHE_VERSION}), encoding="utf-8")
    elif stage == "storymap":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"section_id":0,"type":"setup","tc_start":0,"tc_end":2,"segment_ids":[0],"summary":"setup","characters":[],"locations":[],"events":["setup"],"confidence":0.8,"warnings":[]}]), encoding="utf-8")
        film_map_path = flag(command, "--film-map")
        profile_path = flag(command, "--video-profile") if "--video-profile" in command else None
        profile_payload = VideoProfile.model_validate_json(profile_path.read_text(encoding="utf-8")).model_dump(mode="json") if profile_path else None
        content_type = command[command.index("--content-type") + 1]
        target_sections = int(command[command.index("--target-story-sections") + 1])
        config_hash = storymap_stable_hash({"film_map":storymap_stable_hash(json.loads(film_map_path.read_text(encoding="utf-8"))),"video_profile":storymap_stable_hash(profile_payload),"content_type":content_type,"target_story_sections":target_sections})
        output.with_name("story_map.meta.json").write_text(json.dumps({"film_map_path":str(film_map_path),"video_profile_path":str(profile_path) if profile_path else None,"content_type":content_type,"duration_s":2,"n_sections":1,"n_non_story":0,"created_at":NOW,"cache_hits":[],"warnings":[],"film_map_hash":file_hash(film_map_path),"video_profile_hash":file_hash(profile_path),"config_hash":config_hash,"cache_version":"storymap-v1"}), encoding="utf-8")
        flag(command, "--output-qa").write_text(json.dumps({"n_sections":1,"n_non_story":0,"warnings":[],"section_warnings":[]}), encoding="utf-8")
    elif stage == "review":
        output = flag(command, "--output")
        args = stage_args(command, build_review_parser())
        output.write_text(json.dumps([{"beat_id":0,"narration":"Má»Ÿ Ä‘áº§u","from_seg_id":0,"to_seg_id":0,"src_tc_start":0,"src_tc_end":2,"is_hook":True}]), encoding="utf-8")
        style_path = Path(args.style_sample).expanduser().resolve() if args.style_sample else DEFAULT_STYLE_SAMPLE
        identity = build_review_identity(film_map_path=args.film_map,settings=args,style_sample_path=style_path,story_map_path=args.story_map,video_profile_path=args.video_profile)
        output.with_name("review_script.meta.json").write_text(json.dumps({"glossary":[],"target_video_s":2,"char_budget":30,"est_total_chars":6,"coverage_pct":1,"qa_report":[],"n_qa_iterations":0,"model_versions":{},"created_at":NOW,"warnings":[],"cache_hits":[],"film_map_hash":identity.film_map_hash,"film_map_meta_hash":identity.film_map_meta_hash,"story_map_hash":identity.story_map_hash,"video_profile_hash":identity.video_profile_hash,"config_hash":identity.config_hash,"cache_version":REVIEW_CACHE_VERSION}), encoding="utf-8")
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
        feature_config = {"end_credit_guard": "--end-credit-guard" in command}
        if feature_config["end_credit_guard"]:
            feature_config["end_credit_tail_s"] = float(command[command.index("--end-credit-tail-s") + 1])
            feature_config["end_credit_threshold"] = float(command[command.index("--end-credit-threshold") + 1])
        output.with_name("shots.meta.json").write_text(json.dumps({"src":"film.mp4","duration_s":2,"n_shots":1,"n_usable":1,"detector":"adaptive","feature_config":feature_config,"model_versions":{},"created_at":NOW,"cache_hits":[],"warnings":[]}), encoding="utf-8")
    elif stage == "match":
        output = flag(command, "--output")
        output.write_text(json.dumps([{"tl_start":0,"tl_end":2,"src":"film.mp4","src_in":0,"src_out":2,"beat_id":0,"shot_index":0,"reused":False,"speed":1}]), encoding="utf-8")
        output.with_name("edl.meta.json").write_text(json.dumps({"total_duration_s":2,"n_placements":1,"n_beats_widened":0,"n_reused":0,"n_speedfit":0,"avg_clip_len":2,"coverage_ok":True,"warnings":[],"seed":1234,"created_at":NOW,"cache_hits":[],"algorithm_version":MATCH_ALGORITHM_VERSION}), encoding="utf-8")
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


def test_legacy_ingest_meta_is_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    meta_path = tmp_path / "run" / "film_map.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for name in ("input_hash", "config_hash", "video_profile_hash", "cache_version"):
        meta.pop(name, None)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert outputs_valid(build_paths(tmp_path / "run"), "ingest", film=args.input, config=load_config(args.config)) is False


def test_preflight_change_reruns_ingest_without_force_cache_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    config_payload = json.loads(args.config.read_text(encoding="utf-8"))
    config_payload["preflight"] = {"max_intro_s": 180}
    args.config.write_text(json.dumps(config_payload), encoding="utf-8")
    commands: list[list[str]] = []

    run_pipeline(args, executor=lambda command, log_path: (commands.append(command), write_stage_outputs(command)))

    ingest_command = next(command for command in commands if stage_name(command) == "ingest")
    assert "--video-profile" in ingest_command
    assert "--force" not in ingest_command


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


def test_end_credit_policy_change_reruns_shots_and_downstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)
    run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["shots"] = {"end_credit_guard": True, "end_credit_tail_s": 600, "end_credit_threshold": 0.6}
    config["match"] = {"exclude_end_credits": True}
    args.config.write_text(json.dumps(config), encoding="utf-8")
    calls: list[str] = []

    run_pipeline(args, executor=lambda command, log_path: (calls.append(stage_name(command)), write_stage_outputs(command)))

    assert calls == ["shots", "match", "render"]


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


def test_review_command_passes_playwright_retry_and_budget_fallback_gate(tmp_path: Path) -> None:
    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    config["orchestrator"]["api_budget_guard"] = "block"
    config["review"].update(
        {
            "playwright_max_attempts": 2,
            "playwright_recovery_timeout_s": 60,
            "openai_fallback_model": "gpt-test",
        }
    )

    command = build_command("review", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert command[command.index("--playwright-max-attempts") + 1] == "2"
    assert command[command.index("--playwright-recovery-timeout-s") + 1] == "60"
    assert "--block-openai-fallback" in command


def test_review_command_passes_auto_duration_policy(tmp_path: Path) -> None:
    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    config["review"].update(
        {
            "auto_max_ratio": 0.35,
            "auto_soft_cap_s": 1800,
            "auto_hard_cap_s": 2400,
            "auto_long_score_threshold": 0.8,
        }
    )

    command = build_command("review", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert command[command.index("--auto-max-ratio") + 1] == "0.35"
    assert command[command.index("--auto-soft-cap-s") + 1] == "1800"
    assert command[command.index("--auto-hard-cap-s") + 1] == "2400"
    assert command[command.index("--auto-long-score-threshold") + 1] == "0.8"


def test_review_fallback_does_not_require_openai_key_during_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    profile = tmp_path / "profile"
    profile.mkdir()
    config = load_config(None)
    config["review"].update({"chatgpt_profile_dir": str(profile), "openai_fallback_model": "gpt-test"})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    preflight(
        film=film,
        selected={"review"},
        forced={"review"},
        paths=build_paths(tmp_path / "run"),
        config=config,
    )


def test_blocked_review_fallback_reports_playwright_failure_when_stage_fails(tmp_path: Path) -> None:
    args = argset(tmp_path, only="review")
    data = json.loads(args.config.read_text(encoding="utf-8"))
    data["orchestrator"] = {"api_budget_guard": "block"}
    data["review"].update({"openai_fallback_model": "gpt-test"})
    args.config.write_text(json.dumps(data), encoding="utf-8")

    def failing_review(command: list[str], log_path: Path) -> None:
        usage_path = args.run_dir / "work" / "review" / "openai_usage.json"
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_path.write_text(
            json.dumps(
                {
                    "configured": True,
                    "allowed": False,
                    "blocked": True,
                    "block_reason": "api_budget_guard=block",
                    "triggered": False,
                    "trigger_reason": "assistant response timed out",
                    "playwright_attempts": 2,
                    "playwright_error_code": "response_timeout",
                    "model": "gpt-test",
                }
            ),
            encoding="utf-8",
        )
        raise OrchestratorError("review failed")

    with pytest.raises(OrchestratorError, match="review failed"):
        run_pipeline(args, executor=failing_review)

    fallback = json.loads((args.run_dir / "fallback_plan.json").read_text(encoding="utf-8"))
    assert fallback["triggered"] is False
    assert fallback["review"]["blocked"] is True
    assert fallback["review"]["playwright_attempts"] == 2
    assert fallback["review"]["error_code"] == "response_timeout"
    assert fallback["reasons"][0]["block_reason"] == "api_budget_guard=block"


def test_sync_restores_configured_review_status_without_usage_artifact(tmp_path: Path) -> None:
    from orchestrator.cost_policy import resolve_cost_policy

    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    paths.fallback_plan.write_text(
        json.dumps({"possible": False, "triggered": False, "reasons": ["timecode QA passed"]}),
        encoding="utf-8",
    )
    config = load_config(None)
    config["review"]["openai_fallback_model"] = "gpt-test"
    _resolved, cost_policy = resolve_cost_policy(config)

    possible, triggered, review = sync_review_fallback_reporting(
        paths=paths,
        cost_policy=cost_policy,
        selected={"ingest", "review"},
        will_run={"ingest"},
        openai_fallback_possible=True,
        fallback_triggered=False,
    )

    plan = json.loads(paths.fallback_plan.read_text(encoding="utf-8"))
    assert possible is True
    assert triggered is False
    assert review["configured"] is True
    assert plan["possible"] is True
    assert plan["review"]["configured"] is True
    assert plan["reasons"] == ["timecode QA passed"]

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


def test_visual_policy_wires_end_credit_guard(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    config["shots"].update({"end_credit_guard": True, "end_credit_tail_s": 600, "end_credit_threshold": 0.6})
    config["match"]["exclude_end_credits"] = True

    shots_command = build_command("shots", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")
    match_command = build_command("match", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert "--end-credit-guard" in shots_command
    assert shots_command[shots_command.index("--end-credit-tail-s") + 1] == "600"
    assert shots_command[shots_command.index("--end-credit-threshold") + 1] == "0.6"
    assert "--exclude-end-credits" in match_command

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
    assert "--no-opening-intra-beat-align" in command
    assert command[command.index("--sentence-refinement-mode") + 1] == "off"
    assert command[command.index("--hook-min-brightness") + 1] == "0.0"
    assert command[command.index("--visual-mode") + 1] == "off"


def test_match_command_enables_opening_intra_beat_alignment_when_configured(tmp_path: Path) -> None:
    from orchestrator.graph import build_paths
    from orchestrator.runner import build_command

    paths = build_paths(tmp_path / "run")
    config = load_config(write_config(tmp_path))
    config["match"]["opening_intra_beat_align"] = True
    config["match"]["sentence_refinement_mode"] = "guarded"
    config["match"]["hook_min_brightness"] = 0.1

    command = build_command("match", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert "--opening-intra-beat-align" in command
    assert command[command.index("--sentence-refinement-mode") + 1] == "guarded"
    assert command[command.index("--hook-min-brightness") + 1] == "0.1"

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


def test_production_movie_preset_builds_cuda_visual_and_resilient_tts_commands(tmp_path: Path) -> None:
    config = load_config(Path("config.movie.production.yaml"))
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    paths = build_paths(tmp_path / "run")

    ingest = build_command("ingest", paths, film, config, force=False)
    tts = build_command("tts", paths, film, config, force=False)
    visual = build_command("visual_index", paths, film, config, force=False)
    match = build_command("match", paths, film, config, force=False)

    assert ingest[ingest.index("--device") + 1] == "cuda"
    assert ingest[ingest.index("--aligner") + 1] == "whisperx"
    assert tts[tts.index("--provider-mode") + 1] == "auto"
    assert tts[tts.index("--genmax-voice-id") + 1] == "VU16byTywsWv5JpI8rbc"
    assert tts[tts.index("--openai-model") + 1] == "gpt-4o-mini-tts"
    assert tts[tts.index("--openai-voice") + 1] == "coral"
    assert tts[tts.index("--concurrency") + 1] == "1"
    assert visual[visual.index("--device") + 1] == "cuda"
    assert "--exclude-end-credits" in match
    assert match[match.index("--visual-mode") + 1] == "rerank"


def test_tts_auto_preflight_accepts_openai_as_only_available_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    config = load_config(None)
    config["tts"].update({"voice_id": "vbee", "provider_mode": "auto", "genmax_voice_id": None})
    monkeypatch.delenv("VIVOO_API_KEY", raising=False)
    monkeypatch.delenv("GENMAX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)

    preflight(
        film=film,
        selected={"tts"},
        forced=set(),
        paths=build_paths(tmp_path / "run"),
        config=config,
    )


def test_production_runtime_preflight_reports_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(Path("config.movie.production.yaml"))
    monkeypatch.setattr("orchestrator.runner.runtime_module_available", lambda module: module != "whisperx")

    with pytest.raises(OrchestratorError, match="whisperx"):
        validate_runtime_requirements({"ingest", "visual_index", "match"}, config)


def test_production_runtime_preflight_requires_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(Path("config.movie.production.yaml"))
    monkeypatch.setattr("orchestrator.runner.runtime_module_available", lambda _module: True)
    monkeypatch.setattr("orchestrator.runner.runtime_cuda_available", lambda: False)

    with pytest.raises(OrchestratorError, match="requires CUDA"):
        validate_runtime_requirements({"ingest", "visual_index", "match"}, config)



def test_vi_low_openai_preset_has_no_openai_uses() -> None:
    from orchestrator.config import load_config
    from orchestrator.cost_policy import resolve_cost_policy

    config = load_config(Path("config.vi.low_openai.yaml"))
    _resolved, policy = resolve_cost_policy(config)

    assert policy.quality_mode == "low_cost"
    assert policy.stages["ingest"]["openai_uses"] == []


def test_balanced_auto_fallback_reruns_ingest_on_severe_alignment_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            meta = {"input_path":"film.mp4","duration":2,"created_at":NOW,"whisper_model":"large-v3","translate_model":"gpt-4.1-mini","vision_model":"gpt-4.1-mini","gap_threshold":4,"max_vision_frames":0,"speech_count":1,"visual_count":0,"cache_hits":[],"warnings_count":1,"timecode_quality":"approximate","approximate_timecodes":True,"asr_provider":"faster-whisper","asr_warnings":["whisperx alignment failed; using approximate timestamps: test failure"]}
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
            output.with_name("film_map.meta.json").write_text(json.dumps({"input_path":"film.mp4","duration":2,"created_at":NOW,"whisper_model":"large-v3","translate_model":"gpt-4.1-mini","vision_model":"gpt-4.1-mini","gap_threshold":4,"max_vision_frames":0,"speech_count":1,"visual_count":0,"cache_hits":[],"warnings_count":1,"timecode_quality":"approximate","approximate_timecodes":True,"asr_provider":"faster-whisper","asr_warnings":["aligner 'whisperx' is configured but not available; using existing timestamps"]}), encoding="utf-8")
        else:
            write_stage_outputs(command)

    with pytest.raises(Exception, match="OpenAI fallback required but blocked"):
        run_pipeline(args, executor=fake_executor)


def test_approximate_timecodes_alone_do_not_trigger_paid_asr_fallback(tmp_path: Path) -> None:
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    paths.film_map_meta.write_text(
        json.dumps(
            {
                "timecode_quality": "approximate",
                "approximate_timecodes": True,
                "asr_provider": "faster-whisper",
                "asr_warnings": ["OpenAI chunked transcription uses rough chunk timestamps before alignment"],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(None)
    config["orchestrator"]["auto_fallback"] = True

    triggered, reasons = should_fallback_timecode(paths, config)

    assert triggered is False
    assert reasons == ["approximate timecodes without a severe alignment/timecode failure"]


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

def test_partial_rerun_preserves_triggered_review_fallback_reporting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("VIVOO_API_KEY", "x")
    monkeypatch.setattr("orchestrator.runner.require_ffmpeg", lambda: None)
    args = argset(tmp_path)

    assert run_pipeline(args, executor=lambda command, log_path: write_stage_outputs(command)) == 0
    usage_path = tmp_path / "run" / "work" / "review" / "openai_usage.json"
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(
        json.dumps(
            {
                "provider": "openai",
                "model": "gpt-test",
                "request_count": 2,
                "input_tokens": 100,
                "output_tokens": 20,
                "triggered": True,
                "trigger_reason": "browser timeout",
                "playwright_attempts": 2,
                "playwright_error_code": "response_timeout",
            }
        ),
        encoding="utf-8",
    )

    rerun_args = argset(tmp_path, only="render", force=True)
    assert run_pipeline(rerun_args, executor=lambda command, log_path: write_stage_outputs(command)) == 0

    fallback = json.loads((tmp_path / "run" / "fallback_plan.json").read_text(encoding="utf-8"))
    assert fallback["possible"] is True
    assert fallback["triggered"] is True
    assert fallback["reasons"] == [
        {
            "stage": "review",
            "reason": "browser timeout",
            "error_code": "response_timeout",
            "playwright_attempts": 2,
            "model": "gpt-test",
            "blocked": False,
            "block_reason": None,
        }
    ]
    assert fallback["review"]["configured"] is True
    assert fallback["review"]["triggered"] is True
    assert fallback["review"]["playwright_attempts"] == 2
    assert fallback["review"]["error_code"] == "response_timeout"
    cost = json.loads((tmp_path / "run" / "cost_summary.json").read_text(encoding="utf-8"))
    assert cost["openai_fallback_possible"] is True
    assert cost["openai_fallback_triggered"] is True
    assert cost["review_openai_fallback"]["request_count"] == 2
