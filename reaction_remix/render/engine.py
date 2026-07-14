from __future__ import annotations

import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.integrity import media_identity_hash
from common.media import probe_duration
from common.schema import RemixEdl

from reaction_remix.render.cache import RemixRenderCache
from reaction_remix.render.commands import RemixRenderError, concat_manifest, ffmpeg_filter_names, run_media_command
from reaction_remix.render.quantize import QuantizedPlacement, quantize_remix_placements

RENDER_ALGORITHM_VERSION = "reaction-render-v4"
MAX_COMMENTARY_LIMIT_DB = -1.5
COMMENTARY_CODEC_HEADROOM_DB = 0.3
FORBIDDEN_VISUAL_FILTERS = [
    "subtitles=",
    "ass=",
    "drawtext=",
    "overlay=",
    "delogo=",
    "boxblur=",
    "gblur=",
    "maskedmerge=",
    "alphamerge=",
]


def _gain_filter(value: float | None) -> str:
    return f"volume={float(value or 0.0):.3f}dB"


def _seconds_from_samples(sample_count: int, sample_rate: int) -> float:
    return sample_count / sample_rate


def _seconds_from_frames(frame_count: int, fps_num: int, fps_den: int) -> float:
    return frame_count * fps_den / fps_num


def _path_identity(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def probe_source_true_peak_dbfs(path: Path) -> float:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-vn",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RemixRenderError(result.stderr.strip() or "could not measure source true peak")
    matches = re.findall(r"Peak:\s+(-?\d+(?:\.\d+)?)\s+dBFS", result.stderr)
    if not matches:
        raise RemixRenderError("could not measure a finite source true peak")
    return float(matches[-1])


def _commentary_limiter_filter(limit_db: float) -> str:
    limit_linear = math.pow(10.0, limit_db / 20.0)
    return f"alimiter=limit={limit_linear:.9f}:level=false:latency=true"


def _cut_video(
    *,
    film_path: Path,
    item: QuantizedPlacement,
    output_path: Path,
    edl: RemixEdl,
    crf: int,
    preset: str,
    commands: list[list[str]],
) -> None:
    source_start_s = _seconds_from_frames(
        item.source_frame_start,
        edl.output.fps_num,
        edl.output.fps_den,
    )
    run_media_command(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{source_start_s:.12f}",
            "-i",
            str(film_path),
            "-an",
            "-frames:v",
            str(item.frame_count),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ],
        commands,
    )


def _cut_audio(
    *,
    film_path: Path,
    item: QuantizedPlacement,
    output_path: Path,
    edl: RemixEdl,
    commentary_limit_db: float,
    commands: list[list[str]],
) -> None:
    audio = item.placement.audio
    duration_s = _seconds_from_samples(item.sample_count, edl.output.audio_sample_rate)
    common_output = [
        "-ac",
        str(edl.output.audio_channels),
        "-ar",
        str(edl.output.audio_sample_rate),
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    if audio.mode == "source":
        source_start_s = _seconds_from_samples(item.source_sample_start, edl.output.audio_sample_rate)
        run_media_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{source_start_s:.12f}",
                "-i",
                str(film_path),
                "-t",
                f"{duration_s:.12f}",
                "-vn",
                *common_output,
            ],
            commands,
        )
        return
    if audio.mode == "silence":
        run_media_command(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r={edl.output.audio_sample_rate}:cl={'mono' if edl.output.audio_channels == 1 else 'stereo'}",
                "-t",
                f"{duration_s:.6f}",
                *common_output,
            ],
            commands,
        )
        return

    if not audio.tts_audio_path:
        raise RemixRenderError(f"{item.placement.placement_id} is missing TTS audio")
    tts_path = Path(audio.tts_audio_path).expanduser().resolve()
    limiter_filter = _commentary_limiter_filter(commentary_limit_db)
    fade_s = min(0.05, duration_s / 4)
    tts_chain = (
        f"aresample={edl.output.audio_sample_rate},{_gain_filter(audio.tts_gain_db)},"
        f"apad,atrim=end_sample={item.sample_count},"
        f"afade=t=in:st=0:d={fade_s:.6f},afade=t=out:st={max(0.0, duration_s - fade_s):.6f}:d={fade_s:.6f}"
    )
    if audio.mode == "tts_bed":
        if not audio.bed_audio_path or audio.bed_in is None:
            raise RemixRenderError(f"{item.placement.placement_id} is missing approved bed audio")
        bed_path = Path(audio.bed_audio_path).expanduser().resolve()
        bed_chain = (
            f"aresample={edl.output.audio_sample_rate},"
            f"atrim=start_sample={item.source_sample_start}:end_sample={item.source_sample_end},"
            "asetpts=PTS-STARTPTS,"
            f"{_gain_filter(audio.bed_gain_db)},"
            f"afade=t=in:st=0:d={min(0.18, duration_s / 4):.6f},"
            f"afade=t=out:st={max(0.0, duration_s - min(0.18, duration_s / 4)):.6f}:d={min(0.18, duration_s / 4):.6f}"
        )
        filter_complex = (
            f"[0:a]{tts_chain}[tts];[1:a]{bed_chain}[bed];"
            f"[tts][bed]amix=inputs=2:duration=first:normalize=0,{limiter_filter},"
            f"apad,atrim=end_sample={item.sample_count}[outa]"
        )
        run_media_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(tts_path),
                "-i",
                str(bed_path),
                "-filter_complex",
                filter_complex,
                "-map",
                "[outa]",
                *common_output,
            ],
            commands,
        )
        return

    run_media_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(tts_path),
            "-af",
            f"{tts_chain},{limiter_filter},apad,atrim=end_sample={item.sample_count}",
            *common_output,
        ],
        commands,
    )


def render_remix(
    *,
    film_path: Path,
    edl: RemixEdl,
    output_path: Path,
    work_dir: Path,
    force: bool = False,
    crf: int = 18,
    preset: str = "medium",
    audio_bitrate: str = "192k",
    edl_hash: str,
    bypass_placement_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    bypass_placement_ids = bypass_placement_ids or set()
    if edl.visual_policy.mask_subtitles or edl.visual_policy.add_subtitles or edl.visual_policy.add_text:
        raise RemixRenderError("visual policy requests forbidden subtitle or text processing")
    if edl.visual_policy.blur or edl.visual_policy.overlay or not edl.visual_policy.preserve_burned_in_pixels:
        raise RemixRenderError("visual policy requests forbidden pixel edits")

    quantized = quantize_remix_placements(
        edl.placements,
        fps_num=edl.output.fps_num,
        fps_den=edl.output.fps_den,
        sample_rate=edl.output.audio_sample_rate,
    )
    source_true_peak_dbfs = probe_source_true_peak_dbfs(film_path)
    commentary_limit_db = min(
        MAX_COMMENTARY_LIMIT_DB,
        source_true_peak_dbfs - COMMENTARY_CODEC_HEADROOM_DB,
    )
    limiter_warning = (
        f"commentary limiter ceiling {commentary_limit_db:.3f} dBFS selected from "
        f"source true peak {source_true_peak_dbfs:.3f} dBFS with "
        f"{COMMENTARY_CODEC_HEADROOM_DB:.3f} dB codec headroom"
    )
    cache = RemixRenderCache(work_dir, force=force)
    cache.prepare()
    commands: list[list[str]] = []
    video_paths: list[Path] = []
    audio_paths: list[Path] = []
    for item in quantized:
        placement = item.placement
        if Path(placement.video.src).expanduser().resolve() != film_path.resolve():
            raise RemixRenderError(f"{placement.placement_id} references a different video source")
        if placement.audio.mode == "source" and (
            placement.audio.source_src is None
            or Path(placement.audio.source_src).expanduser().resolve() != film_path.resolve()
        ):
            raise RemixRenderError(f"{placement.placement_id} references a different source audio file")
        _video_key, video_path = cache.video_path(
            {
                "algorithm": RENDER_ALGORITHM_VERSION,
                "film": media_identity_hash(film_path),
                "source_frames": [item.source_frame_start, item.source_frame_end],
                "frames": item.frame_count,
                "fps": [edl.output.fps_num, edl.output.fps_den],
                "crf": crf,
                "preset": preset,
            }
        )
        _audio_key, audio_path = cache.audio_path(
            {
                "algorithm": RENDER_ALGORITHM_VERSION,
                "film": media_identity_hash(film_path),
                "mode": placement.audio.mode,
                "source_samples": [item.source_sample_start, item.source_sample_end],
                "tts": _path_identity(Path(placement.audio.tts_audio_path).expanduser().resolve()) if placement.audio.tts_audio_path else None,
                "bed": _path_identity(Path(placement.audio.bed_audio_path).expanduser().resolve()) if placement.audio.bed_audio_path else None,
                "bed_span": [placement.audio.bed_in, placement.audio.bed_out],
                "gains": [placement.audio.source_gain_db, placement.audio.tts_gain_db, placement.audio.bed_gain_db],
                "samples": item.sample_count,
                "sample_rate": edl.output.audio_sample_rate,
                "channels": edl.output.audio_channels,
                "commentary_limit_db": commentary_limit_db if placement.audio.mode in {"tts", "tts_bed"} else None,
            }
        )
        bypass_cache = placement.placement_id in bypass_placement_ids
        if bypass_cache:
            video_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
        cached_video_commands = cache.use(video_path)
        if cached_video_commands is None:
            command_start = len(commands)
            _cut_video(
                film_path=film_path,
                item=item,
                output_path=video_path,
                edl=edl,
                crf=crf,
                preset=preset,
                commands=commands,
            )
            cache.record_commands(video_path, commands[command_start:])
        else:
            commands.extend(cached_video_commands)
        cached_audio_commands = cache.use(audio_path)
        if cached_audio_commands is None:
            command_start = len(commands)
            _cut_audio(
                film_path=film_path,
                item=item,
                output_path=audio_path,
                edl=edl,
                commentary_limit_db=commentary_limit_db,
                commands=commands,
            )
            cache.record_commands(audio_path, commands[command_start:])
        else:
            commands.extend(cached_audio_commands)
        video_paths.append(video_path)
        audio_paths.append(audio_path)

    video_manifest = work_dir / "video.concat.txt"
    audio_manifest = work_dir / "audio.concat.txt"
    concat_manifest(video_paths, video_manifest)
    concat_manifest(audio_paths, audio_manifest)
    video_timeline = work_dir / "video_timeline.mp4"
    audio_timeline = work_dir / "audio_timeline.wav"
    run_media_command(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(video_manifest), "-c", "copy", str(video_timeline)],
        commands,
    )
    run_media_command(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(audio_manifest), "-c", "copy", str(audio_timeline)],
        commands,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_media_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_timeline),
            "-i",
            str(audio_timeline),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        commands,
    )
    run_media_command(
        ["ffmpeg", "-v", "error", "-i", str(output_path), "-f", "null", "-"],
        commands,
    )
    forbidden = sorted(ffmpeg_filter_names(commands))
    if forbidden:
        raise RemixRenderError(f"render command contains forbidden visual filters: {forbidden}")

    created_at = datetime.now(timezone.utc).isoformat()
    timeline = {
        "schema_version": "reaction-remix.v1",
        "source_hash": edl.source_hash,
        "edl_hash": edl_hash,
        "fps_num": edl.output.fps_num,
        "fps_den": edl.output.fps_den,
        "audio_sample_rate": edl.output.audio_sample_rate,
        "placements": [
            {
                "placement_id": item.placement.placement_id,
                "tl_start_frame": item.frame_start,
                "tl_end_frame": item.frame_end,
                "tl_start_sample": item.sample_start,
                "tl_end_sample": item.sample_end,
                "src_start_frame": item.source_frame_start,
                "src_end_frame": item.source_frame_end,
                "src_start_sample": item.source_sample_start,
                "src_end_sample": item.source_sample_end,
            }
            for item in quantized
        ],
        "total_frames": quantized[-1].frame_end if quantized else 0,
        "total_samples": quantized[-1].sample_end if quantized else 0,
        "created_at": created_at,
        "warnings": [limiter_warning],
    }
    command_manifest = {
        "schema_version": "reaction-remix.v1",
        "source_hash": edl.source_hash,
        "edl_hash": edl_hash,
        "denylist": FORBIDDEN_VISUAL_FILTERS,
        "commands": [
            {"command_id": f"command-{index:04d}", "purpose": _command_purpose(command), "args": command}
            for index, command in enumerate(commands)
        ],
        "created_at": created_at,
        "warnings": [limiter_warning],
    }
    meta = {
        "schema_version": "reaction-remix.v1",
        "source_hash": edl.source_hash,
        "edl_hash": edl_hash,
        "output_path": output_path.as_posix(),
        "video_codec": "h264",
        "audio_codec": "aac",
        "crf": crf,
        "audio_bitrate": audio_bitrate,
        "width": edl.output.width,
        "height": edl.output.height,
        "fps_num": edl.output.fps_num,
        "fps_den": edl.output.fps_den,
        "audio_sample_rate": edl.output.audio_sample_rate,
        "audio_channels": edl.output.audio_channels,
        "duration_s": probe_duration(output_path),
        "n_placements": len(edl.placements),
        "decode_ok": True,
        "timeline_hash": "0" * 64,
        "command_manifest_hash": "0" * 64,
        "created_at": created_at,
        "cache_hits": cache.cache_hits,
        "warnings": [limiter_warning],
    }
    return timeline, command_manifest, meta


def _command_purpose(command: list[str]) -> str:
    joined = " ".join(command)
    if "video_timeline.mp4" in joined and "-f concat" in joined:
        return "concat_video"
    if "audio_timeline.wav" in joined and "-f concat" in joined:
        return "concat_audio"
    if "-c:a aac" in joined:
        return "mux_output"
    if "-an" in command:
        return "render_video_clip"
    return "render_audio_clip"
