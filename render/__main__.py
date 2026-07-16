from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from common.media import MediaError, has_audio_stream, probe_duration, probe_video_stream, require_ffmpeg
from common.schema import EdlPlacement, RenderMeta, validate_edl, write_json
from render.cache import RenderCache
from render.compose import concat_video, mux_voiceover, pad_video_by_tail, pad_video_to_duration
from render.cut import RenderParams, clamp_source, cut_temp_clip, temp_cache_key
from render.quantize import quantize_placements

TAIL_PAD_WARNING_THRESHOLD_S = 1.0

class RenderError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 6: render recap video from EDL and voiceover.")
    parser.add_argument("--edl", type=Path, required=True)
    parser.add_argument("--voiceover", type=Path, required=True)
    parser.add_argument("--film", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--fit", choices=["cover"], default="cover")
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--audio-delay-s", type=float, default=0.0, help="Delay voiceover audio at mux time; use when audio subjectively leads video")
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


def render_temp_clips(*, film_path: Path, frames, film_duration: float, params: RenderParams, cache: RenderCache, concurrency: int) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    temp_paths: list[Path | None] = [None] * len(frames)
    jobs = []
    for frame in frames:
        source = clamp_source(frame.placement, film_duration)
        warnings.extend(source.warnings)
        cache_key = temp_cache_key(film_path=film_path, frame=frame, source=source, params=params)
        cached = cache.get_cached_temp(cache_key)
        if cached is not None:
            temp_paths[frame.index] = cached
            continue
        output_path = cache.temp_path(cache_key)
        jobs.append((frame, source, output_path))
    if jobs:
        workers = max(1, concurrency)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(cut_temp_clip, film_path=film_path, output_path=output_path, frame=frame, source=source, params=params): (frame, output_path)
                for frame, source, output_path in jobs
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
    ensure_file(args.film, "film")
    if args.width <= 0 or args.height <= 0 or args.fps <= 0:
        raise RenderError("width, height and fps must be greater than zero")
    if args.concurrency <= 0:
        raise RenderError("concurrency must be greater than zero")
    if args.audio_delay_s < 0:
        raise RenderError("--audio-delay-s must be >= 0")
    placements = load_edl(args.edl)
    if not placements:
        raise RenderError("edl cannot be empty")
    film_info = probe_video_stream(args.film)
    film_duration = float(film_info["duration"])
    audio_duration = probe_duration(args.voiceover)
    mux_audio_duration = audio_duration + args.audio_delay_s
    frames = quantize_placements(placements, args.fps)
    params = RenderParams(width=args.width, height=args.height, fps=args.fps, fit=args.fit, crf=args.crf, preset=args.preset)
    cache = RenderCache(args.work_dir, force=args.force)
    cache.prepare()
    temp_paths, warnings = render_temp_clips(
        film_path=args.film,
        frames=frames,
        film_duration=film_duration,
        params=params,
        cache=cache,
        concurrency=args.concurrency,
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
    if video_only_duration + duration_tolerance < mux_audio_duration:
        padded_video = args.work_dir / "video_only_padded.mp4"
        target_label = "delayed audio duration" if args.audio_delay_s > 0 else "audio duration"
        shortage_s = mux_audio_duration - video_only_duration
        tail_pad_message = f"video-only concat was tail-padded from {video_only_duration:.3f}s to {target_label} {mux_audio_duration:.3f}s"
        if shortage_s > TAIL_PAD_WARNING_THRESHOLD_S:
            warnings.append(tail_pad_message)
        logging.info("%s", tail_pad_message)
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
    logging.info("mux voiceover")
    mux_voiceover(video_for_mux, args.voiceover, args.output, audio_delay_s=args.audio_delay_s)
    output_info = probe_video_stream(args.output)
    video_duration = probe_duration(args.output)
    if not has_audio_stream(args.output):
        warnings.append("output has no audio stream")
    duration_match = abs(video_duration - mux_audio_duration) <= duration_tolerance
    if not duration_match:
        warnings.append(f"video/audio duration mismatch: video={video_duration:.3f}s delayed_audio={mux_audio_duration:.3f}s")
    if int(output_info["width"]) != args.width or int(output_info["height"]) != args.height:
        warnings.append("output resolution does not match requested size")
    if abs(float(output_info["fps"]) - args.fps) > 0.05:
        warnings.append("output fps does not match requested fps")
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
