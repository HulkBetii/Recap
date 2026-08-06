from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from array import array
from pathlib import Path
from typing import Any

import yaml

from common.media import probe_audio_stream_count, probe_duration, probe_video_stream, require_ffmpeg
from common.schema import AudioAssetManifest, EditPlan, EdlPlacement, PlacementEdit, write_json
from scripts.release_helpers import ROOT, resolve_release_work_dir

SAMPLE_RATE = 48_000
OUTPUT_DURATION_S = 4.0
OUTPUT_FPS = 30
OUTPUT_FRAMES = round(OUTPUT_DURATION_S * OUTPUT_FPS)
ANALYSIS_WIDTH = 96
ANALYSIS_HEIGHT = 54
SOURCE_TONE_HZ = 997.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline enhanced-render synthetic FFmpeg smoke")
    parser.add_argument("--work-dir", type=Path, default=Path("work") / "enhanced-render-smoke")
    parser.add_argument("--report", type=Path, default=None)
    return parser


def run_logged(command: list[str], *, log_path: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n")
        handle.write(result.stdout)
        handle.write(result.stderr)
        handle.write("\n")
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"command failed ({result.returncode}): {message}")


def capture_bytes(command: list[str]) -> bytes:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip() or "unknown error"
        raise RuntimeError(f"command failed ({result.returncode}): {message}")
    return result.stdout


def require(condition: bool, message: str, assertions: list[str]) -> None:
    if not condition:
        raise AssertionError(message)
    assertions.append(message)


def generate_media(work_dir: Path, *, log_path: Path, env: dict[str, str]) -> dict[str, Path]:
    source = work_dir / "source.mp4"
    voiceover = work_dir / "voiceover.mp3"
    music = work_dir / "music-default.mp3"
    whoosh = work_dir / "whoosh.wav"
    impact = work_dir / "impact.wav"
    run_logged(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={SOURCE_TONE_HZ:g}:sample_rate={SAMPLE_RATE}",
            "-t",
            "5.5",
            "-vf",
            "drawgrid=w=80:h=60:t=2:c=white@0.65",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        log_path=log_path,
        env=env,
    )
    tone_specs = [
        (voiceover, 440, OUTPUT_DURATION_S, "libmp3lame"),
        (music, 220, 1.0, "libmp3lame"),
        (whoosh, 660, 0.18, "pcm_s16le"),
        (impact, 880, 0.22, "pcm_s16le"),
    ]
    for path, frequency, duration, codec in tone_specs:
        run_logged(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate={SAMPLE_RATE}",
                "-t",
                f"{duration:.3f}",
                "-c:a",
                codec,
                str(path),
            ],
            log_path=log_path,
            env=env,
        )
    return {"source": source, "voiceover": voiceover, "music": music, "whoosh": whoosh, "impact": impact}


def write_inputs(work_dir: Path, media: dict[str, Path]) -> tuple[Path, Path, Path]:
    edl_path = work_dir / "edl.json"
    edit_plan_path = work_dir / "edit_plan.json"
    audio_assets_path = work_dir / "audio_assets.yaml"
    placements = [
        EdlPlacement(
            tl_start=0,
            tl_end=2,
            src="source.mp4",
            src_in=0,
            src_out=2,
            beat_id=0,
            shot_index=0,
            reused=False,
            speed=1,
        ),
        EdlPlacement(
            tl_start=2,
            tl_end=4,
            src="source.mp4",
            src_in=2.5,
            src_out=4.5,
            beat_id=1,
            shot_index=1,
            reused=False,
            speed=1,
        ),
    ]
    write_json(edl_path, placements)
    edits = [
        PlacementEdit(
            placement_index=0,
            beat_id=0,
            mood="action",
            role="transition",
            src_in=0,
            src_out=2,
            source_extension_s=0.2,
            zoom_start=1.03,
            zoom_end=1.03,
            pan_x=0.02,
            aspect_ratio=2.35,
            contrast=1.1,
            saturation=1.12,
            speed_ramp=[
                {"start_ratio": 0, "end_ratio": 0.733333, "speed": 1},
                {"start_ratio": 0.733333, "end_ratio": 0.866667, "speed": 1.15},
                {"start_ratio": 0.866667, "end_ratio": 1, "speed": 1.35},
            ],
        ),
        PlacementEdit(
            placement_index=1,
            beat_id=1,
            mood="suspense",
            role="reveal",
            src_in=2.5,
            src_out=4.5,
            zoom_start=1.02,
            zoom_end=1.08,
            contrast=1,
            saturation=1,
            freeze_duration_s=0.45,
        ),
    ]
    plan = EditPlan(
        seed=20260730,
        total_duration_s=OUTPUT_DURATION_S,
        placements=edits,
        music_cues=[
            {
                "asset_id": "music-default",
                "tl_start": 0,
                "tl_end": OUTPUT_DURATION_S,
                "mood": "default",
                "gain_db": -24,
            }
        ],
        sfx_cues=[
            {"asset_id": "whoosh", "kind": "whoosh", "tl_start": 1.5, "gain_db": -14},
            {"asset_id": "impact", "kind": "impact", "tl_start": 3.55, "gain_db": -10},
        ],
    )
    write_json(edit_plan_path, plan)
    manifest = AudioAssetManifest.model_validate(
        {
            "version": 1,
            "assets": [
                {
                    "asset_id": "music-default",
                    "path": media["music"].name,
                    "kind": "music",
                    "mood": "default",
                    "loopable": True,
                    "license_source": "synthetic smoke asset",
                },
                {
                    "asset_id": "whoosh",
                    "path": media["whoosh"].name,
                    "kind": "sfx",
                    "sfx_kind": "whoosh",
                    "license_source": "synthetic smoke asset",
                },
                {
                    "asset_id": "impact",
                    "path": media["impact"].name,
                    "kind": "sfx",
                    "sfx_kind": "impact",
                    "license_source": "synthetic smoke asset",
                },
            ],
        }
    )
    audio_assets_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return edl_path, edit_plan_path, audio_assets_path


def render_output(
    *,
    work_dir: Path,
    media: dict[str, Path],
    edl_path: Path,
    edit_plan_path: Path,
    audio_assets_path: Path,
    log_path: Path,
    env: dict[str, str],
) -> Path:
    output = work_dir / "enhanced-smoke.mp4"
    run_logged(
        [
            sys.executable,
            "-m",
            "render",
            "--edl",
            str(edl_path),
            "--voiceover",
            str(media["voiceover"]),
            "--film",
            str(media["source"]),
            "--edit-plan",
            str(edit_plan_path),
            "--audio-assets",
            str(audio_assets_path),
            "--output",
            str(output),
            "--width",
            "1920",
            "--height",
            "1080",
            "--fps",
            str(OUTPUT_FPS),
            "--crf",
            "28",
            "--preset",
            "ultrafast",
            "--concurrency",
            "1",
            "--work-dir",
            str(work_dir / "render-work"),
            "--log-level",
            "INFO",
        ],
        log_path=log_path,
        env=env,
    )
    return output


def pcm_samples(path: Path) -> array[int]:
    raw = capture_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    samples = array("h")
    samples.frombytes(raw)
    return samples


def tone_amplitude(samples: array[int], frequency_hz: float) -> float:
    if not samples:
        return 0.0
    angular = 2.0 * math.pi * frequency_hz / SAMPLE_RATE
    real = 0.0
    imaginary = 0.0
    sample_count = len(samples)
    for index, sample in enumerate(samples):
        window = 0.5 - 0.5 * math.cos(2.0 * math.pi * index / max(1, sample_count - 1))
        normalized = sample / 32768.0 * window
        real += normalized * math.cos(angular * index)
        imaginary -= normalized * math.sin(angular * index)
    return 2.0 * math.hypot(real, imaginary) / sample_count


def video_frames(path: Path) -> list[bytes]:
    frame_size = ANALYSIS_WIDTH * ANALYSIS_HEIGHT
    raw = capture_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT},format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    if len(raw) % frame_size:
        raise AssertionError("decoded raw video has a partial frame")
    return [raw[offset : offset + frame_size] for offset in range(0, len(raw), frame_size)]


def single_frame(path: Path, timestamp_s: float) -> bytes:
    raw = capture_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{timestamp_s:.6f}",
            "-frames:v",
            "1",
            "-vf",
            f"scale={ANALYSIS_WIDTH}:{ANALYSIS_HEIGHT},format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    expected = ANALYSIS_WIDTH * ANALYSIS_HEIGHT
    if len(raw) != expected:
        raise AssertionError(f"expected one decoded frame, got {len(raw)} bytes")
    return raw


def mean_absolute_difference(first: bytes, second: bytes) -> float:
    if len(first) != len(second):
        raise ValueError("frame sizes do not match")
    return sum(abs(left - right) for left, right in zip(first, second)) / len(first)


def analyze_video(output: Path, source: Path, assertions: list[str]) -> dict[str, float]:
    frames = video_frames(output)
    require(len(frames) == OUTPUT_FRAMES, f"output contains exactly {OUTPUT_FRAMES} decoded frames", assertions)
    mean_luma = [sum(frame) / len(frame) for frame in frames]
    black_ratios = [sum(value < 8 for value in frame) / len(frame) for frame in frames]
    require(min(mean_luma) > 18.0, "output contains no black frames", assertions)
    require(max(black_ratios) < 0.55, "output contains no mostly-black frames", assertions)
    anomalies = [
        abs(mean_luma[index] - (mean_luma[index - 1] + mean_luma[index + 1]) / 2)
        for index in range(1, len(frames) - 1)
    ]
    require(max(anomalies) < 18.0, "output contains no single-frame luminance flash", assertions)
    moving_differences = [mean_absolute_difference(frames[index - 1], frames[index]) for index in range(91, 105)]
    frozen_differences = [mean_absolute_difference(frames[index - 1], frames[index]) for index in range(108, 120)]
    moving_difference = sum(moving_differences) / len(moving_differences)
    frozen_difference = sum(frozen_differences) / len(frozen_differences)
    require(
        frozen_difference < moving_difference * 0.65,
        "freeze window has materially less frame motion than the preceding moving window",
        assertions,
    )
    early_output = single_frame(output, 2.2)
    early_source = single_frame(source, 2.7)
    late_output = single_frame(output, 3.2)
    late_source = single_frame(source, 3.7)
    early_zoom_difference = mean_absolute_difference(early_output, early_source)
    late_zoom_difference = mean_absolute_difference(late_output, late_source)
    require(early_zoom_difference > 2.0, "zoomed output differs observably from the neutral source frame", assertions)
    require(
        late_zoom_difference > early_zoom_difference * 1.05,
        "push-in zoom grows observably across the reveal placement",
        assertions,
    )
    return {
        "min_mean_luma": round(min(mean_luma), 3),
        "max_black_pixel_ratio": round(max(black_ratios), 4),
        "max_flash_anomaly": round(max(anomalies), 3),
        "moving_frame_mad": round(moving_difference, 3),
        "freeze_frame_mad": round(frozen_difference, 3),
        "early_zoom_mad": round(early_zoom_difference, 3),
        "late_zoom_mad": round(late_zoom_difference, 3),
    }


def analyze_audio(output: Path, source: Path, assertions: list[str]) -> dict[str, float]:
    final_samples = pcm_samples(output)
    source_samples = pcm_samples(source)
    final_source_tone = tone_amplitude(final_samples, SOURCE_TONE_HZ)
    source_tone = tone_amplitude(source_samples, SOURCE_TONE_HZ)
    final_voice_tone = tone_amplitude(final_samples, 440)
    final_music_tone = tone_amplitude(final_samples, 220)
    reference_tone = max(final_voice_tone, final_music_tone)
    require(source_tone > 0.02, "synthetic source contains the distinctive 997 Hz source tone", assertions)
    require(reference_tone > 0.005, "final master contains the generated voiceover or music reference tone", assertions)
    require(
        final_source_tone < source_tone * 0.05,
        "final master suppresses the distinctive source tone by more than 26 dB",
        assertions,
    )
    require(
        final_source_tone < reference_tone * 0.08,
        "final master has no practical 997 Hz source-tone retention",
        assertions,
    )
    return {
        "source_997_amplitude": round(source_tone, 8),
        "final_997_amplitude": round(final_source_tone, 8),
        "final_440_amplitude": round(final_voice_tone, 8),
        "final_220_amplitude": round(final_music_tone, 8),
        "source_tone_suppression_db": round(20 * math.log10(max(final_source_tone, 1e-12) / source_tone), 3),
    }


def main() -> int:
    args = build_parser().parse_args()
    require_ffmpeg()
    work_dir = resolve_release_work_dir(args.work_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    report_path = args.report.expanduser().resolve() if args.report is not None else work_dir / "report.json"
    log_path = work_dir / "commands.log"
    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "VIVOO_API_KEY", "GENMAX_API_KEY"):
        env.pop(name, None)
    assertions: list[str] = []
    media = generate_media(work_dir, log_path=log_path, env=env)
    edl_path, edit_plan_path, audio_assets_path = write_inputs(work_dir, media)
    output = render_output(
        work_dir=work_dir,
        media=media,
        edl_path=edl_path,
        edit_plan_path=edit_plan_path,
        audio_assets_path=audio_assets_path,
        log_path=log_path,
        env=env,
    )
    stream = probe_video_stream(output)
    duration = probe_duration(output)
    audio_stream_count = probe_audio_stream_count(output)
    require(int(stream["width"]) == 1920 and int(stream["height"]) == 1080, "output is 1920x1080", assertions)
    require(abs(float(stream["fps"]) - OUTPUT_FPS) <= 0.01, "output is frame-locked at 30 fps", assertions)
    require(int(stream["frame_count"] or 0) == OUTPUT_FRAMES, "ffprobe reports exactly 120 output frames", assertions)
    require(abs(duration - OUTPUT_DURATION_S) <= 0.05, "output duration matches the four-second timeline", assertions)
    require(audio_stream_count == 1, "output contains exactly one audio stream", assertions)
    visual_metrics = analyze_video(output, media["source"], assertions)
    audio_metrics = analyze_audio(output, media["source"], assertions)
    render_meta: dict[str, Any] = json.loads((work_dir / "render.meta.json").read_text(encoding="utf-8"))
    require(render_meta["original_audio_included"] is False, "render metadata confirms original audio exclusion", assertions)
    report = {
        "status": "passed",
        "output": str(output),
        "duration_s": round(duration, 3),
        "frame_count": OUTPUT_FRAMES,
        "audio_stream_count": audio_stream_count,
        "visual_metrics": visual_metrics,
        "audio_metrics": audio_metrics,
        "assertion_count": len(assertions),
        "assertions": assertions,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
