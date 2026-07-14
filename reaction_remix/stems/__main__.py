from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from common.integrity import atomic_write_json, file_hash, media_identity_hash, stable_hash
from common.media import probe_duration, require_ffmpeg
from common.schema import AudioAssets, ReactionSource, ReactionStageMeta, validate_audio_assets


class StemsError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare optional local commentary beds with Demucs")
    parser.add_argument("--film", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-dir", default=Path("work/reaction-remix/stems"), type=Path)
    parser.add_argument("--provider", default="demucs", choices=["demucs", "off"])
    parser.add_argument("--model", default="htdemucs")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def _probe_audio(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise StemsError(result.stderr.strip() or "could not probe separated stem")
    try:
        stream = json.loads(result.stdout)["streams"][0]
        return int(stream["sample_rate"]), int(stream["channels"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StemsError("separated stem has no valid audio stream") from exc


def _empty_assets(source: ReactionSource, warning: str) -> AudioAssets:
    return AudioAssets.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source.input_hash,
            "items": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": [warning],
        }
    )


def run_stems(args: argparse.Namespace) -> int:
    logger = logging.getLogger("reaction_remix.stems")
    require_ffmpeg()
    film_path = args.film.expanduser().resolve()
    source_path = args.source.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    if not film_path.is_file() or not source_path.is_file():
        raise StemsError("film and reaction_source.json must exist")
    source = ReactionSource.model_validate_json(source_path.read_text(encoding="utf-8"))
    if source.input_hash != media_identity_hash(film_path):
        raise StemsError("source media identity does not match reaction_source.json")
    input_hashes = {"film_identity": source.input_hash, source_path.name: file_hash(source_path)}
    config_hash = stable_hash(
        {"provider": args.provider, "model": args.model, "device": args.device, "algorithm": "reaction-stems-v2"}
    )
    meta_path = output_path.with_name("audio_assets.meta.json")
    if not args.force and output_path.is_file() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if (
                meta.get("input_hashes") == input_hashes
                and meta.get("config_hash") == config_hash
                and meta.get("output_hashes", {}).get(output_path.name) == file_hash(output_path)
            ):
                validate_audio_assets(AudioAssets.model_validate_json(output_path.read_text(encoding="utf-8")), source=source)
                logger.info("Stem assets are current; skipping")
                return 0
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    if args.provider == "off":
        assets = _empty_assets(source, "local stem separation is disabled; compose will use TTS-only commentary")
    else:
        stem_root = work_dir / "demucs"
        command = [
            sys.executable,
            "-m",
            "demucs",
            "--two-stems",
            "vocals",
            "-n",
            args.model,
            "-d",
            args.device,
            "-o",
            str(stem_root),
            str(film_path),
        ]
        demucs_env = os.environ.copy()
        demucs_env["PYTHONIOENCODING"] = "utf-8"
        demucs_env["PYTHONUTF8"] = "1"
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=demucs_env,
            check=False,
        )
        no_vocals = stem_root / args.model / film_path.stem / "no_vocals.wav"
        if result.returncode != 0 or not no_vocals.is_file():
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Demucs produced no no_vocals stem"
            raise StemsError(f"required Demucs separation failed: {detail}")
        else:
            duration_s = probe_duration(no_vocals)
            sample_rate, channels = _probe_audio(no_vocals)
            warnings: list[str] = []
            if abs(duration_s - source.duration_s) > 0.1:
                warnings.append(f"stem duration differs from source by {duration_s - source.duration_s:.3f}s")
            assets = AudioAssets.model_validate(
                {
                    "schema_version": "reaction-remix.v1",
                    "source_hash": source.input_hash,
                    "items": [
                        {
                            "asset_id": "stem-no-vocals",
                            "kind": "no_vocals",
                            "path": no_vocals.resolve().as_posix(),
                            "content_hash": file_hash(no_vocals),
                            "source_hash": source.input_hash,
                            "duration_s": duration_s,
                            "sample_rate": sample_rate,
                            "channels": channels,
                            "src_tc_start": 0.0,
                            "src_tc_end": min(duration_s, source.duration_s),
                            "leakage_detected": False,
                            "warnings": ["narrator leakage remains subject to R8 ASR/correlation QA"],
                        }
                    ],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "warnings": warnings,
                }
            )
    validate_audio_assets(assets, source=source)
    atomic_write_json(output_path, assets.model_dump(mode="json"))
    meta = ReactionStageMeta.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "stage": "stems",
            "algorithm_version": "reaction-stems-v2",
            "input_hashes": input_hashes,
            "config_hash": config_hash,
            "output_hashes": {output_path.name: file_hash(output_path)},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "cache_hits": [],
            "warnings": assets.warnings,
        }
    )
    atomic_write_json(meta_path, meta.model_dump(mode="json"))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_stems(args)
    except (StemsError, OSError, ValueError) as exc:
        parser.exit(2, f"reaction-remix stems: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
