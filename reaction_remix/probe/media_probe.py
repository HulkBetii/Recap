from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from common.integrity import media_identity_hash, stable_hash
from common.schema import (
    ReactionAudioStream,
    ReactionSource,
    ReactionSubtitleStream,
    ReactionVideoStream,
    validate_reaction_source,
)


class ReactionProbeError(RuntimeError):
    pass


def _rational(value: object) -> Fraction:
    text = str(value or "0/1")
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise ReactionProbeError(f"invalid ffprobe frame rate: {text}") from exc
    if rate <= 0:
        raise ReactionProbeError(f"invalid non-positive frame rate: {text}")
    return rate


def read_ffprobe(input_path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(input_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ReactionProbeError(result.stderr.strip() or "ffprobe failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReactionProbeError("ffprobe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ReactionProbeError("ffprobe payload must be an object")
    return payload


def probe_reaction_source(
    input_path: Path,
    *,
    has_burned_in_subtitles: bool,
    ffprobe_payload: dict[str, Any] | None = None,
) -> ReactionSource:
    resolved = input_path.expanduser().resolve()
    if not resolved.is_file():
        raise ReactionProbeError(f"input video does not exist: {resolved}")
    payload = ffprobe_payload or read_ffprobe(resolved)
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ReactionProbeError("ffprobe payload has no streams list")
    videos = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")
    ]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    if len(videos) != 1:
        raise ReactionProbeError(f"reaction-remix.v1 requires exactly one video stream; found {len(videos)}")
    if len(audios) != 1:
        raise ReactionProbeError(f"reaction-remix.v1 requires exactly one audio stream; found {len(audios)}")

    video_payload = videos[0]
    audio_payload = audios[0]
    avg_rate = _rational(video_payload.get("avg_frame_rate") or video_payload.get("r_frame_rate"))
    real_rate = _rational(video_payload.get("r_frame_rate") or video_payload.get("avg_frame_rate"))
    duration_raw = payload.get("format", {}).get("duration") or video_payload.get("duration") or audio_payload.get("duration")
    try:
        duration_s = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise ReactionProbeError("ffprobe did not report a valid source duration") from exc

    subtitle_models = [
        ReactionSubtitleStream(
            stream_index=int(stream["index"]),
            codec=str(stream.get("codec_name") or "unknown"),
            language=(stream.get("tags") or {}).get("language"),
            title=(stream.get("tags") or {}).get("title"),
        )
        for stream in subtitles
    ]
    config_hash = stable_hash(
        {
            "soft_subtitle_policy": "fail",
            "burned_subtitle_policy": "preserve",
            "has_burned_in_subtitles": has_burned_in_subtitles,
        }
    )
    source = ReactionSource(
        input_path=resolved.as_posix(),
        input_hash=media_identity_hash(resolved),
        duration_s=duration_s,
        video=ReactionVideoStream(
            stream_index=int(video_payload["index"]),
            codec=str(video_payload.get("codec_name") or "unknown"),
            width=int(video_payload["width"]),
            height=int(video_payload["height"]),
            fps_num=avg_rate.numerator,
            fps_den=avg_rate.denominator,
            pixel_format=str(video_payload.get("pix_fmt") or "unknown"),
            frame_rate_mode="cfr" if avg_rate == real_rate else "vfr",
        ),
        audio=ReactionAudioStream(
            stream_index=int(audio_payload["index"]),
            codec=str(audio_payload.get("codec_name") or "unknown"),
            sample_rate=int(audio_payload["sample_rate"]),
            channels=int(audio_payload["channels"]),
            channel_layout=str(audio_payload.get("channel_layout") or "unknown"),
        ),
        subtitle_streams=subtitle_models,
        has_burned_in_subtitles=has_burned_in_subtitles,
        created_at=datetime.now(timezone.utc),
        config_hash=config_hash,
        warnings=[] if avg_rate == real_rate else ["source frame rate appears variable; R7 supports CFR only"],
    )
    return validate_reaction_source(source)
