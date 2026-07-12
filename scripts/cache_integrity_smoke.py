from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from common.media import probe_duration, require_ffmpeg
from scripts.release_helpers import (
    ROOT,
    artifact_rebuilt,
    artifact_unchanged,
    resolve_release_work_dir,
    snapshot_ingest_cache,
)

API_ENV_VARS = ("OPENAI_API_KEY", "VIVOO_API_KEY", "GENMAX_API_KEY")


def run_command(command: list[str], *, env: dict[str, str], log_path: Path) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
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
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}")


def require(condition: bool, message: str, assertions: list[str]) -> None:
    if not condition:
        raise AssertionError(message)
    assertions.append(message)


def build_ingest_command(
    *,
    clip: Path,
    profile: Path,
    transcript: Path,
    output: Path,
    work_dir: Path,
    glossary: Path | None = None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "ingest",
        "--input",
        str(clip),
        "--output",
        str(output),
        "--video-profile",
        str(profile),
        "--asr-provider",
        "manual",
        "--transcript-input",
        str(transcript),
        "--source-language",
        "vi",
        "--translate-mode",
        "none",
        "--aligner",
        "none",
        "--timecode-quality",
        "approximate",
        "--max-vision-frames",
        "0",
        "--drop-non-korean-intro-s",
        "0",
        "--work-dir",
        str(work_dir),
    ]
    if glossary is not None:
        command += ["--transcript-correction", "glossary", "--glossary", str(glossary)]
    else:
        command += ["--transcript-correction", "off"]
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a no-API real-media G0/G1 cache integrity smoke")
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    media = args.media.expanduser().resolve()
    if not media.is_file():
        raise FileNotFoundError(f"media does not exist: {media}")
    require_ffmpeg()
    work_dir = resolve_release_work_dir(args.work_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    log_path = work_dir / "commands.log"
    env = os.environ.copy()
    for name in API_ENV_VARS:
        env.pop(name, None)

    clip = work_dir / "clip.mp4"
    run_command(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "0",
            "-t",
            "30",
            "-i",
            str(media),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(clip),
        ],
        env=env,
        log_path=log_path,
    )
    duration = probe_duration(clip)
    if duration < 5:
        raise RuntimeError(f"release smoke clip is too short: {duration:.3f}s")

    transcript = work_dir / "manual_transcript.json"
    midpoint = min(10.0, duration / 2)
    transcript.write_text(
        json.dumps(
            [
                {"id": 0, "tc_start": 0.0, "tc_end": midpoint, "ko": "Nhan vat Alpha xuat hien."},
                {"id": 1, "tc_start": midpoint, "tc_end": duration, "ko": "Cau chuyen tiep tuc."},
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    profile = work_dir / "video_profile.json"
    preflight_work = work_dir / "preflight"
    preflight_command = [
        sys.executable,
        "-m",
        "preflight",
        "--input",
        str(clip),
        "--output",
        str(profile),
        "--classifier",
        "heuristic",
        "--max-intro-s",
        str(min(30.0, duration)),
        "--sample-every-s",
        "5",
        "--work-dir",
        str(preflight_work),
    ]
    run_command(preflight_command, env=env, log_path=log_path)

    ingest_output = work_dir / "film_map.json"
    ingest_work = work_dir / "ingest"
    initial_command = build_ingest_command(
        clip=clip,
        profile=profile,
        transcript=transcript,
        output=ingest_output,
        work_dir=ingest_work,
    )
    assertions: list[str] = []
    run_command(initial_command, env=env, log_path=log_path)
    initial = snapshot_ingest_cache(ingest_work)

    time.sleep(0.05)
    run_command(initial_command, env=env, log_path=log_path)
    unchanged = snapshot_ingest_cache(ingest_work)
    require(initial["keys"] == unchanged["keys"], "unchanged rerun preserves every manifest key", assertions)
    for name in ("audio.wav", "transcript_aligned.json", "transcript_quality.json", "translated.json", "vision.json"):
        require(artifact_unchanged(initial, unchanged, name), f"unchanged rerun reuses {name}", assertions)

    profile_payload = json.loads(profile.read_text(encoding="utf-8"))
    profile_payload.setdefault("warnings", []).append("release-gate-profile-change")
    profile.write_text(json.dumps(profile_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    time.sleep(0.05)
    run_command(initial_command, env=env, log_path=log_path)
    profile_changed = snapshot_ingest_cache(ingest_work)
    for stage in ("audio", "transcript", "correction", "translation"):
        require(unchanged["keys"][stage] == profile_changed["keys"][stage], f"profile change preserves {stage} key", assertions)
    require(unchanged["keys"]["vision"] != profile_changed["keys"]["vision"], "profile change invalidates only vision key", assertions)
    for name in ("audio.wav", "transcript_aligned.json", "transcript_quality.json", "translated.json"):
        require(artifact_unchanged(unchanged, profile_changed, name), f"profile change reuses {name}", assertions)
    require(artifact_rebuilt(unchanged, profile_changed, "vision.json"), "profile change rebuilds vision.json", assertions)

    glossary = work_dir / "glossary.txt"
    glossary.write_text("Alpha => Omega\n", encoding="utf-8")
    correction_command = build_ingest_command(
        clip=clip,
        profile=profile,
        transcript=transcript,
        output=ingest_output,
        work_dir=ingest_work,
        glossary=glossary,
    )
    time.sleep(0.05)
    run_command(correction_command, env=env, log_path=log_path)
    correction_changed = snapshot_ingest_cache(ingest_work)
    require(profile_changed["keys"]["audio"] == correction_changed["keys"]["audio"], "glossary change preserves audio key", assertions)
    require(profile_changed["keys"]["transcript"] == correction_changed["keys"]["transcript"], "glossary change preserves aligned transcript key", assertions)
    for stage in ("correction", "translation", "vision"):
        require(profile_changed["keys"][stage] != correction_changed["keys"][stage], f"glossary change invalidates {stage} key", assertions)
    require(artifact_unchanged(profile_changed, correction_changed, "transcript_aligned.json"), "glossary change reuses transcript_aligned.json", assertions)
    translated_text = (ingest_work / "translated.json").read_text(encoding="utf-8")
    require("Omega" in translated_text and "Alpha" not in translated_text, "glossary correction reaches translated.json", assertions)

    clip_stat = clip.stat()
    os.utime(clip, ns=(clip_stat.st_atime_ns, clip_stat.st_mtime_ns + 2_000_000_000))
    time.sleep(0.05)
    run_command(preflight_command, env=env, log_path=log_path)
    run_command(correction_command, env=env, log_path=log_path)
    film_changed = snapshot_ingest_cache(ingest_work)
    require(correction_changed["keys"]["audio"] != film_changed["keys"]["audio"], "film identity change invalidates audio key", assertions)
    require(correction_changed["keys"]["transcript"] != film_changed["keys"]["transcript"], "film identity change invalidates transcript key", assertions)
    require(correction_changed["keys"]["vision"] != film_changed["keys"]["vision"], "film identity change invalidates vision key", assertions)
    for name in ("audio.wav", "transcript_aligned.json", "transcript_quality.json", "transcript_corrected.json", "transcript_correction.meta.json", "translated.json", "vision.json"):
        require(artifact_rebuilt(correction_changed, film_changed, name), f"film identity change rebuilds {name}", assertions)

    report = {
        "media_name": media.name,
        "clip_duration_s": round(duration, 3),
        "api_environment_removed": list(API_ENV_VARS),
        "assertions": assertions,
        "assertion_count": len(assertions),
        "final_cache_version": film_changed["cache_version"],
        "status": "passed",
    }
    report_path = args.report.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
