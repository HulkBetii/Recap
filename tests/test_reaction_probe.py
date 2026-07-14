from __future__ import annotations

from pathlib import Path

import pytest

from reaction_remix.probe.media_probe import ReactionProbeError, probe_reaction_source


def ffprobe_payload(*, subtitles: bool = False) -> dict:
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30000/1001",
            "pix_fmt": "yuv420p",
            "disposition": {"attached_pic": 0},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "44100",
            "channels": 2,
            "channel_layout": "stereo",
        },
    ]
    if subtitles:
        streams.append(
            {"index": 2, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "ja"}}
        )
    return {"streams": streams, "format": {"duration": "12.5"}}


def test_probe_preserves_rational_fps(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"media")
    source = probe_reaction_source(
        source_path,
        has_burned_in_subtitles=True,
        ffprobe_payload=ffprobe_payload(),
    )
    assert (source.video.fps_num, source.video.fps_den) == (30000, 1001)
    assert source.video.frame_rate_mode == "cfr"
    assert source.input_path == source_path.resolve().as_posix()


def test_probe_fails_soft_subtitle_stream(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"media")
    with pytest.raises(ValueError, match="soft subtitle"):
        probe_reaction_source(
            source_path,
            has_burned_in_subtitles=True,
            ffprobe_payload=ffprobe_payload(subtitles=True),
        )


def test_probe_requires_primary_audio_and_video(tmp_path: Path) -> None:
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"media")
    payload = ffprobe_payload()
    payload["streams"] = payload["streams"][:1]
    with pytest.raises(ReactionProbeError, match="audio stream"):
        probe_reaction_source(
            source_path,
            has_burned_in_subtitles=True,
            ffprobe_payload=payload,
        )

