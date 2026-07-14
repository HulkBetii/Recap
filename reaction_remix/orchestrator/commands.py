from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from reaction_remix.orchestrator.paths import ReactionRunPaths


def _python(config: dict[str, Any]) -> str:
    return str(config["orchestrator"].get("python") or sys.executable)


def _option(command: list[str], name: str, value: object | None) -> None:
    if value is None:
        return
    command.extend([f"--{name.replace('_', '-')}", str(value)])


def _flag(command: list[str], name: str, enabled: bool) -> None:
    if enabled:
        command.append(f"--{name.replace('_', '-')}")


def build_command(
    stage: str,
    *,
    film: Path,
    paths: ReactionRunPaths,
    config: dict[str, Any],
    force: bool = False,
    fit_repair: bool = False,
    active_repair_request: Path | None = None,
    accepted_repair_request: Path | None = None,
) -> list[str]:
    command = [_python(config), "-m", f"reaction_remix.{stage}"]
    work_dir = paths.stage_work_dir(stage)
    log_level = config["orchestrator"]["log_level"]

    if stage == "probe":
        section = config["probe"]
        command += ["--input", str(film), "--output", str(paths.reaction_source), "--work-dir", str(work_dir)]
        _option(command, "soft_subtitle_policy", section["soft_subtitle_policy"])
        _option(command, "burned_subtitle_policy", section["burned_subtitle_policy"])
    elif stage == "analyze":
        section = config["analyze"]
        command += [
            "--input", str(film),
            "--source", str(paths.reaction_source),
            "--output", str(paths.reaction_transcript),
            "--work-dir", str(work_dir),
        ]
        _option(command, "model", section["whisper_model"])
        _option(command, "device", section["device"])
        _option(command, "compute_type", section["compute_type"])
        _option(command, "source_language", section["source_language"])
        _option(command, "max_region_s", section["max_region_s"])
        _option(command, "multilingual_window_s", section["multilingual_window_s"])
        _option(command, "overlap_s", section["region_overlap_s"])
        _option(command, "speech_padding_s", section["speech_padding_s"])
        _option(command, "max_attempts", section["max_attempts"])
        _option(command, "speaker_threshold", section["speaker_cluster_threshold"])
    elif stage == "shots":
        section = config["shots"]
        command = [_python(config), "-m", "shots"]
        command += [
            "--input", str(film),
            "--output", str(paths.shots),
            "--thumb-dir", str(work_dir / "thumbs"),
            "--work-dir", str(work_dir),
        ]
        for key in ("detector", "frame_sampling", "face_detection", "max_shot_len"):
            _option(command, key, section[key])
    elif stage == "stems":
        section = config["stems"]
        command += [
            "--film", str(film),
            "--source", str(paths.reaction_source),
            "--output", str(paths.audio_assets),
            "--work-dir", str(work_dir),
        ]
        _option(command, "provider", section["provider"] if section["enabled"] else "off")
        _option(command, "model", section["model"])
        _option(command, "device", section["device"])
    elif stage == "segment":
        section = config["segment"]
        command += [
            "--source", str(paths.reaction_source),
            "--transcript", str(paths.reaction_transcript),
            "--output", str(paths.reaction_blocks),
            "--review-html", str(paths.blocks_review_html),
            "--work-dir", str(work_dir),
        ]
        if paths.shots.is_file() or config["shots"]["enabled"]:
            command += ["--shots", str(paths.shots)]
        for key in (
            "min_silence_s", "speech_padding_s", "scene_cut_tolerance_s", "min_cut_spacing_s",
            "commentary_min_confidence", "narrator_min_regions", "narrator_min_japanese_ratio",
        ):
            _option(command, key, section[key])
        _option(
            command,
            "commentary_boundary_policy",
            str(section["commentary_boundary_policy"]).replace("_", "-"),
        )
    elif stage == "plan":
        section = config["plan"]
        command += [
            "--source", str(paths.reaction_source),
            "--transcript", str(paths.reaction_transcript),
            "--blocks", str(paths.reaction_blocks),
            "--output", str(paths.remix_plan),
            "--work-dir", str(work_dir),
            "--chat-session-meta", str(paths.work_dir / "editorial_chat_session.json"),
        ]
        for key in (
            "output_ratio", "hard_min_output_ratio", "preferred_min_output_ratio",
            "preferred_max_output_ratio", "hard_max_output_ratio", "min_unique_reaction_speech_ratio",
            "chatgpt_profile_dir", "chat_session_policy", "reply_timeout_s", "playwright_max_attempts",
            "playwright_recovery_timeout_s",
        ):
            _option(command, key, section[key])
        for block_id in section.get("manual_drop_block_ids", []):
            _option(command, "manual_drop_block_id", block_id)
        repair_request = active_repair_request or accepted_repair_request
        if repair_request is not None:
            command += ["--repair-request", str(repair_request)]
    elif stage == "write":
        section = config["write"]
        plan_section = config["plan"]
        command += [
            "--plan", str(paths.remix_plan),
            "--blocks", str(paths.reaction_blocks),
            "--transcript", str(paths.reaction_transcript),
            "--output", str(paths.commentary_script),
            "--work-dir", str(work_dir),
            "--chat-session-meta", str(paths.work_dir / "editorial_chat_session.json"),
        ]
        _option(command, "style_id", section["style_id"])
        _option(command, "max_qa_iterations", section["max_qa_iterations"])
        for key in (
            "chatgpt_profile_dir", "chat_session_policy", "reply_timeout_s", "playwright_max_attempts",
            "playwright_recovery_timeout_s",
        ):
            _option(command, key, plan_section[key])
        if fit_repair:
            command += ["--fit-request", str(paths.commentary_fit_requests)]
    elif stage == "tts":
        section = config["tts"]
        command += [
            "--script", str(paths.commentary_script),
            "--source", str(paths.reaction_source),
            "--output", str(paths.commentary_audio),
            "--fit-request-output", str(paths.commentary_fit_requests),
            "--work-dir", str(work_dir),
        ]
        _option(command, "trim_handle_ms", section["trim_handle_ms"])
        _option(command, "target_lufs", section["target_lufs"])
        _option(command, "max_true_peak_db", section["max_true_peak_db"])
        _option(command, "asr_model", section["asr_model"])
        _option(command, "min_asr_similarity", section["min_asr_similarity"])
        _option(command, "fit_tolerance_s", float(section["fit_tolerance_ms"]) / 1000.0)
        _option(command, "max_fit_iterations", section["max_fit_iterations"])
        if fit_repair:
            command += ["--fit-request", str(paths.commentary_fit_requests)]
    elif stage == "compose":
        section = config["compose"]
        command += [
            "--film", str(film),
            "--source", str(paths.reaction_source),
            "--blocks", str(paths.reaction_blocks),
            "--plan", str(paths.remix_plan),
            "--commentary-audio", str(paths.commentary_audio),
            "--audio-assets", str(paths.audio_assets),
            "--output", str(paths.remix_edl),
            "--work-dir", str(work_dir),
        ]
        _option(command, "tts_gain_db", section["commentary_tts_gain_db"])
        _option(command, "bed_gain_db", section["commentary_bed_gain_db"])
        _option(command, "bed_fade_ms", section["commentary_bed_fade_ms"])
        _option(command, "boundary_fade_ms", section["commentary_boundary_fade_ms"])
        if active_repair_request is not None:
            command += ["--repair-request", str(active_repair_request)]
            command += ["--repair-overrides", str(active_repair_request)]
        elif accepted_repair_request is not None:
            command += ["--repair-overrides", str(accepted_repair_request)]
    elif stage == "render":
        section = config["render"]
        command += [
            "--film", str(film),
            "--source", str(paths.reaction_source),
            "--edl", str(paths.remix_edl),
            "--output", str(paths.output_video),
            "--work-dir", str(work_dir),
            "--timeline-output", str(paths.render_timeline),
            "--command-manifest", str(paths.render_command_manifest),
            "--meta-output", str(paths.render_meta),
        ]
        for key in ("crf", "preset", "audio_bitrate"):
            _option(command, key, section[key])
        if active_repair_request is not None:
            command += ["--repair-request", str(active_repair_request)]
    elif stage == "qa":
        section = config["qa"]
        command += [
            "--film", str(film),
            "--source", str(paths.reaction_source),
            "--transcript", str(paths.reaction_transcript),
            "--blocks", str(paths.reaction_blocks),
            "--plan", str(paths.remix_plan),
            "--commentary-script", str(paths.commentary_script),
            "--commentary-audio", str(paths.commentary_audio),
            "--edl", str(paths.remix_edl),
            "--video", str(paths.output_video),
            "--render-meta", str(paths.render_meta),
            "--render-timeline", str(paths.render_timeline),
            "--command-manifest", str(paths.render_command_manifest),
            "--output", str(paths.remix_qa),
            "--review-html", str(paths.qa_review_html),
            "--qa-dir", str(paths.stage_work_dir("qa")),
        ]
        _option(command, "min_output_ratio", section["min_output_ratio"])
        _option(command, "preferred_min_ratio", section["preferred_min_output_ratio"])
        _option(command, "preferred_max_ratio", section["preferred_max_output_ratio"])
        _option(command, "min_correlation", section["min_reaction_audio_correlation"])
        _option(command, "min_frame_similarity", section["min_frame_similarity"])
        _option(command, "min_tts_asr_match", section["min_tts_asr_similarity"])
        if active_repair_request is not None:
            command += ["--repair-requests", str(active_repair_request)]
    else:
        raise ValueError(f"unknown reaction stage: {stage}")

    _flag(command, "force", force)
    _option(command, "log_level", log_level)
    return command
