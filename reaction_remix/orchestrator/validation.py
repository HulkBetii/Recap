from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from common.integrity import file_hash, media_identity_hash, stable_hash
from common.schema import ReactionStageMeta, Shot, validate_shots
from reaction_remix.orchestrator.paths import ReactionRunPaths, primary_outputs
from reaction_remix.segment.__main__ import SEGMENT_ALGORITHM_VERSION
from reaction_remix.segment.blocks import SegmentSettings


MODEL_NAMES = {
    "probe": "ReactionSource",
    "analyze": "ReactionTranscript",
    "stems": "AudioAssets",
    "segment": "ReactionBlocks",
    "plan": "RemixPlan",
    "write": "CommentaryScript",
    "tts": "CommentaryAudio",
    "compose": "RemixEdl",
    "qa": "RemixQa",
}


def _validate_model(path: Path, model_name: str) -> None:
    import common.schema as schema

    model = getattr(schema, model_name)
    model.model_validate_json(path.read_text(encoding="utf-8"))


def _meta_path(paths: ReactionRunPaths, stage: str) -> Path | None:
    return {
        "probe": paths.reaction_source.with_name("reaction_source.meta.json"),
        "analyze": paths.reaction_transcript.with_name("reaction_transcript.meta.json"),
        "shots": paths.shots.with_name("shots.meta.json"),
        "stems": paths.audio_assets.with_name("audio_assets.meta.json"),
        "segment": paths.reaction_blocks.with_name("reaction_blocks.meta.json"),
        "plan": paths.remix_plan.with_name("remix_plan.meta.json"),
        "write": paths.commentary_script.with_name("commentary_script.meta.json"),
        "tts": paths.commentary_audio.with_name("commentary_audio.manifest.json"),
        "compose": paths.remix_edl.with_name("remix_edl.meta.json"),
        "render": paths.render_meta,
        "qa": paths.remix_qa.with_name("remix_qa.meta.json"),
    }.get(stage)


def _hashes_match(meta: dict[str, Any], outputs: tuple[Path, ...]) -> bool:
    output_hashes = meta.get("output_hashes")
    if isinstance(output_hashes, dict):
        for output in outputs:
            declared = output_hashes.get(output.name) or output_hashes.get(str(output))
            if declared is not None and declared != file_hash(output):
                return False
        return True
    output_hash = meta.get("output_hash")
    json_outputs = [path for path in outputs if path.suffix.lower() == ".json"]
    if isinstance(output_hash, str) and json_outputs:
        return output_hash == file_hash(json_outputs[0])
    outputs_map = meta.get("outputs")
    if isinstance(outputs_map, dict):
        return all(outputs_map.get(path.name) == file_hash(path) for path in outputs)
    return True


def _segment_meta_is_current(paths: ReactionRunPaths, config: dict[str, Any] | None) -> bool:
    meta_path = _meta_path(paths, "segment")
    if meta_path is None or not meta_path.is_file():
        return False
    meta = ReactionStageMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    expected_input_hashes = {
        "reaction_source": file_hash(paths.reaction_source),
        "reaction_transcript": file_hash(paths.reaction_transcript),
    }
    if paths.shots.is_file():
        expected_input_hashes["shots"] = file_hash(paths.shots)
    if any(value is None for value in expected_input_hashes.values()):
        return False
    if meta.algorithm_version != SEGMENT_ALGORITHM_VERSION or meta.input_hashes != expected_input_hashes:
        return False
    if config is None:
        return True
    section = config["segment"]
    settings = SegmentSettings(
        min_silence_s=section["min_silence_s"],
        speech_padding_s=section["speech_padding_s"],
        scene_window_s=section["scene_cut_tolerance_s"],
        min_cut_spacing_s=section["min_cut_spacing_s"],
        commentary_min_confidence=section["commentary_min_confidence"],
        narrator_min_regions=section["narrator_min_regions"],
        narrator_min_japanese_ratio=section["narrator_min_japanese_ratio"],
        boundary_policy=str(section["commentary_boundary_policy"]).replace("_", "-"),
    )
    settings.validate()
    return meta.config_hash == stable_hash(asdict(settings))


def outputs_valid(
    paths: ReactionRunPaths,
    stage: str,
    *,
    config: dict[str, Any] | None = None,
    film: Path | None = None,
) -> bool:
    outputs = primary_outputs(paths, stage)
    if not all(path.is_file() for path in outputs):
        return False
    if stage == "render" and paths.output_video.stat().st_size <= 0:
        return False
    try:
        if stage == "shots":
            payload = json.loads(paths.shots.read_text(encoding="utf-8"))
            validate_shots([Shot.model_validate(item) for item in payload])
        elif stage == "render":
            _validate_model(paths.render_timeline, "RemixRenderTimeline")
            _validate_model(paths.render_command_manifest, "RemixCommandManifest")
            _validate_model(paths.render_meta, "RemixRenderMeta")
        else:
            _validate_model(outputs[0], MODEL_NAMES[stage])
            if stage == "tts":
                _validate_model(paths.commentary_fit_requests, "CommentaryFitRequests")
        if stage == "probe":
            import common.schema as schema

            source = schema.ReactionSource.model_validate_json(paths.reaction_source.read_text(encoding="utf-8"))
            if film is not None:
                resolved_film = film.expanduser().resolve()
                if source.input_path != resolved_film.as_posix() or source.input_hash != media_identity_hash(resolved_film):
                    return False
            if config is not None and config["render"]["require_cfr_source"] and source.video.frame_rate_mode != "cfr":
                return False
        elif stage == "analyze":
            import common.schema as schema

            source = schema.ReactionSource.model_validate_json(paths.reaction_source.read_text(encoding="utf-8"))
            transcript = schema.ReactionTranscript.model_validate_json(
                paths.reaction_transcript.read_text(encoding="utf-8")
            )
            if transcript.source_hash != source.input_hash:
                return False
        elif stage == "stems":
            import common.schema as schema

            source = schema.ReactionSource.model_validate_json(paths.reaction_source.read_text(encoding="utf-8"))
            assets = schema.AudioAssets.model_validate_json(paths.audio_assets.read_text(encoding="utf-8"))
            if assets.source_hash != source.input_hash:
                return False
        elif stage == "segment":
            import common.schema as schema

            blocks = schema.ReactionBlocks.model_validate_json(paths.reaction_blocks.read_text(encoding="utf-8"))
            if blocks.transcript_hash != file_hash(paths.reaction_transcript):
                return False
            if not _segment_meta_is_current(paths, config):
                return False
        elif stage == "plan":
            import common.schema as schema

            plan = schema.RemixPlan.model_validate_json(paths.remix_plan.read_text(encoding="utf-8"))
            if plan.blocks_hash != file_hash(paths.reaction_blocks):
                return False
            if config is not None:
                section = config["plan"]
                policy = plan.duration_policy
                if any(
                    abs(actual - expected) > 1e-3
                    for actual, expected in (
                        (policy.hard_min_output_ratio, section["hard_min_output_ratio"]),
                        (policy.preferred_min_output_ratio, section["preferred_min_output_ratio"]),
                        (policy.preferred_max_output_ratio, section["preferred_max_output_ratio"]),
                        (policy.hard_max_output_ratio, section["hard_max_output_ratio"]),
                        (policy.target_duration_s, plan.original_duration_s * section["output_ratio"]),
                    )
                ):
                    return False
                manual_drop_ids = set(section.get("manual_drop_block_ids", []))
                excluded_manual = {
                    item.block_id
                    for item in plan.excluded_blocks
                    if item.category == "manual_drop"
                }
                selected_manual = {
                    item.block_id
                    for item in plan.items
                    if item.kind == "source_block" and item.block_id in manual_drop_ids
                }
                if excluded_manual != manual_drop_ids or selected_manual:
                    return False
        elif stage == "write":
            import common.schema as schema

            script = schema.CommentaryScript.model_validate_json(paths.commentary_script.read_text(encoding="utf-8"))
            if script.plan_hash != file_hash(paths.remix_plan):
                return False
            if config is not None and script.style_id != config["write"]["style_id"]:
                return False
        elif stage == "tts":
            import common.schema as schema

            audio = schema.CommentaryAudio.model_validate_json(paths.commentary_audio.read_text(encoding="utf-8"))
            fit = schema.CommentaryFitRequests.model_validate_json(
                paths.commentary_fit_requests.read_text(encoding="utf-8")
            )
            script_hash = file_hash(paths.commentary_script)
            if audio.script_hash != script_hash or fit.script_hash != script_hash or fit.requests:
                return False
            if config is not None:
                section = config["tts"]
                policy = audio.voice_policy
                if (
                    policy.provider != section["provider"]
                    or policy.voice_id != section["voice_id"]
                    or policy.model != section["model"]
                    or policy.speed != section["speed"]
                    or policy.fallback_provider != section["fallback_provider"]
                    or policy.text_normalization != section["text_normalization"]
                ):
                    return False
            for item in audio.items:
                audio_path = (paths.commentary_audio.parent / item.audio_path).resolve()
                if not item.audio_sha256 or file_hash(audio_path) != item.audio_sha256:
                    return False
        elif stage == "compose":
            import common.schema as schema

            edl = schema.RemixEdl.model_validate_json(paths.remix_edl.read_text(encoding="utf-8"))
            if edl.plan_hash != file_hash(paths.remix_plan) or edl.commentary_audio_hash != file_hash(
                paths.commentary_audio
            ):
                return False
        meta_path = _meta_path(paths, stage)
        if meta_path is not None and meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict) or not _hashes_match(meta, outputs):
                return False
    except (AttributeError, OSError, ValueError, TypeError, json.JSONDecodeError, ValidationError):
        return False
    return True
