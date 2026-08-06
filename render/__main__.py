from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import yaml

from common.media import (
    MediaError,
    has_audio_stream,
    probe_audio_stream_count,
    probe_duration,
    probe_video_stream,
    require_ffmpeg,
)
from common.schema import (
    AudioAssetManifest,
    EditPlan,
    EdlPlacement,
    EdlSourceMap,
    PlacementEdit,
    RenderMeta,
    validate_edl,
    write_json,
)
from render.audio import render_enhanced_audio
from render.cache import RenderCache
from render.compose import concat_video, mux_master_audio, mux_voiceover, pad_video_by_tail, pad_video_to_duration
from render.cut import RenderParams, clamp_source, cut_temp_clip, required_enhanced_source_duration, temp_cache_key
from render.quantize import FramePlacement, quantize_placements


class RenderError(RuntimeError):
    pass


def valid_cached_temp(path: Path, *, frame: FramePlacement, params: RenderParams) -> bool:
    try:
        info = probe_video_stream(path)
    except MediaError as exc:
        logging.warning("ignore invalid cached temp clip %s: %s", path.name, exc)
        return False
    expected_duration = frame.frame_count / params.fps
    duration_tolerance = max(0.1, 2.0 / params.fps)
    valid = (
        int(info["width"]) == params.width
        and int(info["height"]) == params.height
        and abs(float(info["fps"]) - params.fps) <= 0.05
        and abs(float(info["duration"]) - expected_duration) <= duration_tolerance
    )
    frame_count = info.get("frame_count")
    if frame_count is not None:
        valid = valid and int(frame_count) == frame.frame_count
    if not valid:
        logging.warning("ignore cached temp clip with unexpected media properties: %s", path.name)
    return valid


def validate_concat_output(
    *,
    info: dict[str, object],
    actual_duration: float,
    frames: Sequence[FramePlacement],
    fps: float,
) -> None:
    expected_frame_count = sum(frame.frame_count for frame in frames)
    expected_duration = expected_frame_count / fps
    actual_frame_count = info.get("frame_count")
    if actual_frame_count is not None and int(actual_frame_count) != expected_frame_count:
        raise RenderError(
            "video-only concat frame count mismatch: "
            f"actual={actual_frame_count} expected={expected_frame_count}; refusing tail padding"
        )
    duration_tolerance = max(0.1, 2.0 / fps, len(frames) / fps)
    if abs(actual_duration - expected_duration) > duration_tolerance:
        raise RenderError(
            "video-only concat duration mismatch: "
            f"actual={actual_duration:.3f}s expected={expected_duration:.3f}s; refusing tail padding"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 6: render recap video from EDL and voiceover.")
    parser.add_argument("--edl", type=Path, required=True)
    parser.add_argument("--voiceover", type=Path, required=True)
    parser.add_argument("--film", type=Path, default=None)
    parser.add_argument("--source-map", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--fit", choices=["cover"], default="cover")
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--audio-delay-s",
        type=float,
        default=0.0,
        help="Delay voiceover audio at mux time; use when audio subjectively leads video",
    )
    parser.add_argument("--edit-plan", type=Path, default=None)
    parser.add_argument("--audio-assets", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=Path("work") / "render")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def load_edl(path: Path) -> list[EdlPlacement]:
    if not path.is_file():
        raise RenderError(f"edl file does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    placements = [EdlPlacement.model_validate(item) for item in data]
    return validate_edl(placements)


def ensure_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise RenderError(f"{label} file does not exist: {path}")


def load_edit_plan(path: Path, placements: list[EdlPlacement]) -> EditPlan:
    ensure_file(path, "edit plan")
    plan = EditPlan.model_validate_json(path.read_text(encoding="utf-8"))
    if len(plan.placements) != len(placements):
        raise RenderError(f"edit plan placement count does not match EDL: {len(plan.placements)} != {len(placements)}")
    for index, (edit, placement) in enumerate(zip(plan.placements, placements)):
        if edit.placement_index != index or edit.beat_id != placement.beat_id:
            raise RenderError(f"edit plan placement #{index} does not match EDL beat identity")
        if abs(edit.src_in - placement.src_in) > 1e-6 or abs(edit.src_out - placement.src_out) > 1e-6:
            raise RenderError(f"edit plan placement #{index} source span does not match EDL base placement")
    edl_duration = placements[-1].tl_end
    if abs(plan.total_duration_s - edl_duration) > 0.05:
        raise RenderError(f"edit plan duration does not match EDL: {plan.total_duration_s:.3f}s != {edl_duration:.3f}s")
    return plan


def load_audio_manifest(path: Path) -> AudioAssetManifest:
    ensure_file(path, "audio assets")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RenderError("audio assets manifest must contain a mapping")
    return AudioAssetManifest.model_validate(payload)


def load_source_map(path: Path | None, film_path: Path | None, placements: list[EdlPlacement]) -> dict[str, Path]:
    if path is None:
        if film_path is None:
            raise RenderError("--film is required when --source-map is not provided")
        return {placement.src: film_path for placement in placements}
    if not path.is_file():
        raise RenderError(f"source map file does not exist: {path}")
    source_map = EdlSourceMap.model_validate_json(path.read_text(encoding="utf-8"))
    resolved = {key: Path(value).expanduser().resolve() for key, value in source_map.sources.items()}
    missing = sorted({placement.src for placement in placements if placement.src not in resolved})
    if missing:
        raise RenderError(f"source map is missing EDL source(s): {missing[:10]}")
    for key, source_path in resolved.items():
        if not source_path.is_file():
            raise RenderError(f"source map entry does not exist for {key}: {source_path}")
    return resolved


def render_temp_clips(
    *,
    source_paths: dict[str, Path],
    source_durations: dict[str, float],
    frames: Sequence[FramePlacement],
    params: RenderParams,
    cache: RenderCache,
    concurrency: int,
    edits: Sequence[PlacementEdit] | None = None,
) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    temp_paths: list[Path | None] = [None] * len(frames)
    jobs = []
    for frame in frames:
        film_path = source_paths[frame.placement.src]
        edit = edits[frame.index] if edits is not None else None
        source_placement = frame.placement
        if edit is not None and not edit.disabled:
            source_placement = frame.placement.model_copy(
                update={"src_in": edit.src_in, "src_out": edit.src_out + edit.source_extension_s}
            )
        source = clamp_source(source_placement, source_durations[frame.placement.src])
        warnings.extend(source.warnings)
        if edit is not None and not edit.disabled and edit.speed_ramp:
            required_source = required_enhanced_source_duration(
                frame_count=frame.frame_count,
                fps=params.fps,
                edit=edit,
                fallback_speed=frame.placement.speed,
            )
            if source.src_out - source.src_in + 2.0 / params.fps < required_source:
                warnings.append(
                    f"placement #{frame.index} speed ramp was skipped because source headroom is unavailable"
                )
                edit = edit.model_copy(update={"speed_ramp": [], "source_extension_s": 0.0})
                source_placement = frame.placement.model_copy(update={"src_in": edit.src_in, "src_out": edit.src_out})
                source = clamp_source(source_placement, source_durations[frame.placement.src])
        cache_key = temp_cache_key(film_path=film_path, frame=frame, source=source, params=params, edit=edit)
        cached = cache.get_cached_temp(
            cache_key,
            validator=lambda path, frame=frame: valid_cached_temp(path, frame=frame, params=params),
        )
        if cached is not None:
            temp_paths[frame.index] = cached
            continue
        output_path = cache.temp_path(cache_key)
        jobs.append((frame, source, edit, output_path))
    if jobs:
        workers = max(1, concurrency)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(
                    cut_temp_clip,
                    film_path=source_paths[frame.placement.src],
                    output_path=output_path,
                    frame=frame,
                    source=source,
                    params=params,
                    edit=edit,
                ): (frame, output_path)
                for frame, source, edit, output_path in jobs
            }
            for completed, future in enumerate(as_completed(future_map), start=1):
                frame, output_path = future_map[future]
                logging.info("cut temp clip %s/%s", completed, len(jobs))
                future.result()
                temp_paths[frame.index] = output_path
    return [path for path in temp_paths if path is not None], warnings


def run_render(args: argparse.Namespace) -> int:
    require_ffmpeg()
    ensure_file(args.voiceover, "voiceover")
    if args.film is not None:
        ensure_file(args.film, "film")
    if args.width <= 0 or args.height <= 0 or args.fps <= 0:
        raise RenderError("width, height and fps must be greater than zero")
    if args.concurrency <= 0:
        raise RenderError("concurrency must be greater than zero")
    if args.audio_delay_s < 0:
        raise RenderError("--audio-delay-s must be >= 0")
    edit_plan_path = getattr(args, "edit_plan", None)
    audio_assets_path = getattr(args, "audio_assets", None)
    if (edit_plan_path is None) != (audio_assets_path is None):
        raise RenderError("--edit-plan and --audio-assets must be provided together")
    placements = load_edl(args.edl)
    if not placements:
        raise RenderError("edl cannot be empty")
    edit_plan = load_edit_plan(edit_plan_path, placements) if edit_plan_path is not None else None
    audio_manifest = load_audio_manifest(audio_assets_path) if audio_assets_path is not None else None
    source_map_arg = getattr(args, "source_map", None)
    source_paths = load_source_map(source_map_arg, args.film.expanduser().resolve() if args.film else None, placements)
    source_infos = {key: probe_video_stream(path) for key, path in source_paths.items()}
    source_durations = {key: float(info["duration"]) for key, info in source_infos.items()}
    audio_duration = probe_duration(args.voiceover)
    frames = quantize_placements(placements, args.fps)
    frame_duration = sum(frame.frame_count for frame in frames) / args.fps
    mux_audio_duration = (
        max(frame_duration, audio_duration + args.audio_delay_s) if edit_plan else audio_duration + args.audio_delay_s
    )
    params = RenderParams(
        width=args.width, height=args.height, fps=args.fps, fit=args.fit, crf=args.crf, preset=args.preset
    )
    cache = RenderCache(args.work_dir, force=args.force)
    cache.prepare()
    temp_paths, warnings = render_temp_clips(
        source_paths=source_paths,
        source_durations=source_durations,
        frames=frames,
        params=params,
        cache=cache,
        concurrency=args.concurrency,
        edits=edit_plan.placements if edit_plan is not None else None,
    )
    if len(temp_paths) != len(frames):
        raise RenderError("not all temp clips were rendered")
    if args.audio_delay_s > 0:
        warnings.append(f"voiceover audio delayed by {args.audio_delay_s:.3f}s at mux")
    video_only = args.work_dir / "video_only.mp4"
    logging.info("concat %s temp clips", len(temp_paths))
    concat_video(temp_paths, video_only, args.work_dir)
    video_for_mux = video_only
    video_only_duration = probe_duration(video_only)
    duration_tolerance = max(0.1, 2.0 / args.fps)
    video_only_info = probe_video_stream(video_only)
    validate_concat_output(info=video_only_info, actual_duration=video_only_duration, frames=frames, fps=args.fps)
    if video_only_duration + duration_tolerance < mux_audio_duration:
        padded_video = args.work_dir / "video_only_padded.mp4"
        target_label = "delayed audio duration" if args.audio_delay_s > 0 else "audio duration"
        shortage_s = mux_audio_duration - video_only_duration
        warnings.append(
            f"video-only concat was tail-padded from {video_only_duration:.3f}s to {target_label} {mux_audio_duration:.3f}s"
        )
        logging.info("tail-pad video-only concat to voiceover duration")
        try:
            pad_frames = pad_video_by_tail(
                video_path=video_only,
                output_path=padded_video,
                work_dir=args.work_dir,
                shortage_s=shortage_s,
                params=params,
            )
            padded_duration = probe_duration(padded_video)
            if padded_duration + duration_tolerance < mux_audio_duration:
                raise MediaError(
                    f"tail-padded video is still short: {padded_duration:.3f}s < {mux_audio_duration:.3f}s"
                )
            logging.info("tail-padded video-only concat by %s frame(s)", pad_frames)
        except MediaError as exc:
            warnings.append(f"tail padding failed; fell back to full re-encode padding: {exc}")
            logging.warning("tail padding failed; falling back to full re-encode padding: %s", exc)
            pad_video_to_duration(video_only, padded_video, mux_audio_duration)
            fallback_duration = probe_duration(padded_video)
            if fallback_duration + duration_tolerance < mux_audio_duration:
                raise RenderError(
                    f"full re-encode padding is still short: {fallback_duration:.3f}s < {mux_audio_duration:.3f}s"
                )
        video_for_mux = padded_video
    if edit_plan is not None:
        assert audio_manifest is not None and audio_assets_path is not None
        logging.info("build enhanced music, SFX and master audio")
        enhanced_audio = render_enhanced_audio(
            voiceover_path=args.voiceover,
            plan=edit_plan,
            manifest=audio_manifest,
            manifest_path=audio_assets_path,
            cache=cache,
            duration_s=mux_audio_duration,
            audio_delay_s=args.audio_delay_s,
        )
        logging.info("mux enhanced master audio")
        mux_master_audio(video_for_mux, enhanced_audio.master_path, args.output)
    else:
        logging.info("mux voiceover")
        mux_voiceover(video_for_mux, args.voiceover, args.output, audio_delay_s=args.audio_delay_s)
    output_info = probe_video_stream(args.output)
    video_duration = probe_duration(args.output)
    output_has_audio = has_audio_stream(args.output)
    audio_stream_count = 1 if output_has_audio else 0
    if edit_plan is not None:
        audio_stream_count = probe_audio_stream_count(args.output)
        if audio_stream_count != 1:
            raise RenderError(f"enhanced output must contain exactly one audio stream; found {audio_stream_count}")
    if not output_has_audio:
        warnings.append("output has no audio stream")
    duration_match = abs(video_duration - mux_audio_duration) <= duration_tolerance
    if not duration_match:
        warnings.append(
            f"video/audio duration mismatch: video={video_duration:.3f}s delayed_audio={mux_audio_duration:.3f}s"
        )
    if int(output_info["width"]) != args.width or int(output_info["height"]) != args.height:
        warnings.append("output resolution does not match requested size")
    if abs(float(output_info["fps"]) - args.fps) > 0.05:
        warnings.append("output fps does not match requested fps")
    grade_moods: dict[str, int] = {}
    n_zoom = n_aspect = n_speed_ramp = n_freeze = n_music_cues = n_sfx_cues = None
    loudness: dict[str, float] = {}
    if edit_plan is not None:
        active_edits = [edit for edit in edit_plan.placements if not edit.disabled]
        n_zoom = sum(
            abs(edit.zoom_start - 1.0) > 1e-6
            or abs(edit.zoom_end - 1.0) > 1e-6
            or abs(edit.pan_x) > 1e-6
            or abs(edit.pan_y) > 1e-6
            for edit in active_edits
        )
        n_aspect = sum(edit.aspect_ratio is not None for edit in active_edits)
        n_speed_ramp = sum(any(abs(segment.speed - 1.0) > 1e-6 for segment in edit.speed_ramp) for edit in active_edits)
        n_freeze = sum(edit.freeze_duration_s > 0 for edit in active_edits)
        n_music_cues = len(edit_plan.music_cues)
        n_sfx_cues = len(edit_plan.sfx_cues)
        grade_moods = dict(Counter(edit.mood for edit in active_edits))
        loudness = {
            "music_lufs": edit_plan.audio_mix.music_lufs,
            "duck_db": edit_plan.audio_mix.duck_db,
            "whoosh_gain_db": edit_plan.audio_mix.whoosh_gain_db,
            "impact_gain_db": edit_plan.audio_mix.impact_gain_db,
            "master_lufs": edit_plan.audio_mix.master_lufs,
            "true_peak_db": edit_plan.audio_mix.true_peak_db,
            "loudness_range": edit_plan.audio_mix.loudness_range,
        }
    meta = RenderMeta(
        width=args.width,
        height=args.height,
        fps=args.fps,
        codec=str(output_info.get("codec") or "h264"),
        video_duration_s=round(video_duration, 3),
        audio_duration_s=round(mux_audio_duration, 3),
        audio_delay_s=round(args.audio_delay_s, 3),
        duration_match=duration_match,
        n_placements=len(placements),
        n_temp_clips=len(temp_paths),
        source_count=len(source_paths),
        source_names=sorted(source_paths),
        n_zoom=n_zoom,
        n_aspect=n_aspect,
        n_speed_ramp=n_speed_ramp,
        n_freeze=n_freeze,
        n_music_cues=n_music_cues,
        n_sfx_cues=n_sfx_cues,
        grade_moods=grade_moods,
        loudness=loudness,
        audio_stream_count=audio_stream_count,
        original_audio_included=False,
        warnings=warnings,
        created_at=datetime.now(timezone.utc),
        cache_hits=cache.cache_hits,
    )
    write_json(args.output.with_name("render.meta.json"), meta)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_render(args)
    except (RenderError, MediaError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"render: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
