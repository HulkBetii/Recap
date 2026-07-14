from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.integrity import media_identity_hash
from common.schema import RemixCommandManifest, RemixEdl, RemixRenderTimeline
from reaction_remix.qa.checks import (
    boundary_audio_defects,
    commentary_peak_dbfs,
    decoded_media_counts,
    program_peak_dbfs,
    sample_reaction_preservation,
)
from reaction_remix.render.engine import render_remix


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr or "media command failed")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required for the synthetic media acceptance test",
)
def test_synthetic_render_preserves_reaction_media_and_quantization(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    tts = tmp_path / "tts.mp3"
    output = tmp_path / "reaction_remix.mp4"
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-filter_complex",
            "[1:a]pan=stereo|c0=c0|c1=c0[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(source),
        ]
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100:duration=2",
            "-af",
            "volume=21dB",
            "-ac",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(tts),
        ]
    )
    source_path = source.resolve().as_posix()
    tts_path = tts.resolve().as_posix()
    edl = RemixEdl.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": media_identity_hash(source),
            "plan_hash": "d" * 64,
            "commentary_audio_hash": "e" * 64,
            "output": {
                "width": 320,
                "height": 180,
                "fps_num": 30,
                "fps_den": 1,
                "audio_sample_rate": 48000,
                "audio_channels": 2,
            },
            "visual_policy": {
                "mask_subtitles": False,
                "add_subtitles": False,
                "add_text": False,
                "blur": False,
                "overlay": False,
                "preserve_burned_in_pixels": True,
            },
            "placements": [
                {
                    "placement_id": "placement-0000",
                    "item_id": "item-0000",
                    "kind": "reaction",
                    "origin_block_id": "block-0001",
                    "tl_start": 0.0,
                    "tl_end": 2.0,
                    "video": {
                        "src": source_path,
                        "src_in": 0.12345,
                        "src_out": 2.12345,
                        "speed": 1.0,
                        "filters": [],
                    },
                    "audio": {
                        "mode": "source",
                        "source_src": source_path,
                        "source_in": 0.12345,
                        "source_out": 2.12345,
                        "source_gain_db": 0.0,
                        "filters": [],
                    },
                    "warnings": [],
                },
                {
                    "placement_id": "placement-0001",
                    "item_id": "item-0001",
                    "kind": "commentary",
                    "origin_block_id": "block-0002",
                    "tl_start": 2.0,
                    "tl_end": 4.0,
                    "video": {
                        "src": source_path,
                        "src_in": 2.0,
                        "src_out": 4.0,
                        "speed": 1.0,
                        "filters": [],
                    },
                    "audio": {
                        "mode": "tts",
                        "tts_audio_path": tts_path,
                        "tts_gain_db": 1.0,
                        "filters": ["boundary_fade_50ms", "commentary_limiter_-1.5db"],
                    },
                    "warnings": [],
                },
            ],
            "total_duration_s": 4.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": [],
        }
    )

    timeline_payload, manifest_payload, _meta = render_remix(
        film_path=source,
        edl=edl,
        edl_hash="f" * 64,
        output_path=output,
        work_dir=tmp_path / "render",
        crf=18,
        preset="medium",
    )

    timeline = RemixRenderTimeline.model_validate(timeline_payload)
    manifest = RemixCommandManifest.model_validate(manifest_payload)
    frames, samples = decoded_media_counts(output, audio_channels=2)
    correlation, lag_ms, similarity, gain_delta_db = sample_reaction_preservation(
        film_path=source,
        output_path=output,
        edl=edl,
    )
    clicks, silent_boundaries = boundary_audio_defects(output, edl)
    input_tts_peak = program_peak_dbfs(tts)
    output_commentary_peak = commentary_peak_dbfs(output, edl)
    source_peak = program_peak_dbfs(source)
    output_peak = program_peak_dbfs(output)
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"],
        capture_output=True,
        check=False,
    )
    command_text = " ".join(arg for command in manifest.commands for arg in command.args).lower()
    source_audio_command = next(
        command.args
        for command in manifest.commands
        if command.purpose == "render_audio_clip" and "-vn" in command.args
    )
    source_audio_clip = Path(source_audio_command[-1])
    commentary_audio_command = next(
        command.args
        for command in manifest.commands
        if command.purpose == "render_audio_clip" and "alimiter=" in " ".join(command.args)
    )
    commentary_audio_clip = Path(commentary_audio_command[-1])
    source_audio_pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source_audio_clip), "-f", "s16le", "-"],
        capture_output=True,
        check=False,
    )
    commentary_audio_pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(commentary_audio_clip), "-f", "s16le", "-"],
        capture_output=True,
        check=False,
    )

    assert decode.returncode == 0
    assert frames == timeline.total_frames == 120
    assert timeline.total_samples == 192000
    assert timeline.placements[0].src_start_frame == 4
    assert timeline.placements[0].src_end_frame == 64
    assert timeline.placements[0].src_start_sample == 5926
    assert timeline.placements[0].src_end_sample == 101926
    assert source_audio_pcm.returncode == 0
    assert len(source_audio_pcm.stdout) // (2 * 2) == 96000
    assert commentary_audio_pcm.returncode == 0
    assert len(commentary_audio_pcm.stdout) // (2 * 2) == 96000
    assert abs(samples - timeline.total_samples) <= 1024  # One final AAC packet, not per-placement priming.
    assert correlation >= 0.98
    assert lag_ms <= 1000 / 30
    assert similarity >= 0.995
    assert gain_delta_db <= 0.3
    assert clicks == 0
    assert silent_boundaries == 0
    assert input_tts_peak > -1.0
    assert output_commentary_peak is not None
    assert output_commentary_peak <= -1.2  # -1.5 dB ceiling plus the allowed AAC encode tolerance.
    assert output_commentary_peak <= source_peak + 0.3
    assert output_peak <= source_peak + 0.3
    video_commands = [command.args for command in manifest.commands if command.purpose == "render_video_clip"]
    assert video_commands
    assert all("-r" not in command for command in video_commands)
    assert not any(term in command_text for term in manifest.denylist)
