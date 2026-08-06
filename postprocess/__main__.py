from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from common.inputs import load_shots
from common.integrity import atomic_write_json, file_hash, media_identity_hash, stable_hash
from common.schema import (
    AudioAssetManifest,
    BeatTiming,
    EditOverrides,
    EditPlan,
    EditPlanMeta,
    EditPlanQa,
    EdlPlacement,
    EdlSourceMap,
    SeriesEventBank,
    SeriesReviewBeat,
    validate_beats_timing,
    validate_edl,
    validate_series_review_script,
)
from postprocess.assets import AudioAssetError, load_audio_manifest, resolve_asset_path, validate_audio_assets
from postprocess.planner import ALGORITHM_VERSION, build_edit_plan


class PostprocessError(RuntimeError):
    pass


class OutputSignature(BaseModel):
    size: int
    mtime_ns: int
    digest: str | None = None


class CacheManifest(BaseModel):
    cache_version: str = ALGORITHM_VERSION
    input_fingerprint: str
    outputs: dict[str, OutputSignature]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan enhanced offline anime post-production effects and audio cues.")
    parser.add_argument("--edl", required=True, type=Path)
    parser.add_argument("--series-review-script", required=True, type=Path)
    parser.add_argument("--event-bank", required=True, type=Path)
    parser.add_argument("--beats-timing", required=True, type=Path)
    parser.add_argument("--episode-run-dir", action="append", default=[], help="Episode artifact dir as episode_key=path")
    parser.add_argument("--source-map", required=True, type=Path)
    parser.add_argument("--audio-assets", required=True, type=Path)
    parser.add_argument("--overrides", default=None, type=Path)
    parser.add_argument("--output", "--output-plan", dest="output", required=True, type=Path)
    parser.add_argument("--output-meta", required=True, type=Path)
    parser.add_argument("--output-qa", required=True, type=Path)
    parser.add_argument("--output-attribution", required=True, type=Path)
    parser.add_argument("--seed", default=1234, type=int)
    parser.add_argument("--profile", default="dynamic_anime", choices=["dynamic_anime"])
    parser.add_argument("--work-dir", default=Path("work") / "postprocess", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def parse_episode_run_dirs(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise PostprocessError("--episode-run-dir must use episode_key=path")
        episode_key, raw_path = value.split("=", 1)
        episode_key = episode_key.strip()
        if not episode_key:
            raise PostprocessError("--episode-run-dir episode_key cannot be empty")
        if episode_key in result:
            raise PostprocessError(f"duplicate episode run dir for {episode_key}")
        result[episode_key] = Path(raw_path).expanduser().resolve()
    if not result:
        raise PostprocessError("at least one --episode-run-dir is required")
    return result


def _load_json(path: Path) -> object:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PostprocessError(f"input file does not exist: {resolved}")
    return json.loads(resolved.read_text(encoding="utf-8"))


def _load_overrides(path: Path | None) -> EditOverrides:
    if path is None:
        return EditOverrides()
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PostprocessError(f"edit overrides file does not exist: {resolved}")
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return EditOverrides.model_validate(raw or {})


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _output_paths(args: argparse.Namespace) -> list[Path]:
    return [args.output, args.output_meta, args.output_qa, args.output_attribution]


def _signature(path: Path) -> OutputSignature | None:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return None
    stat = resolved.stat()
    return OutputSignature(size=stat.st_size, mtime_ns=stat.st_mtime_ns, digest=file_hash(resolved))


def _outputs_valid(args: argparse.Namespace) -> bool:
    try:
        EditPlan.model_validate_json(args.output.read_text(encoding="utf-8"))
        EditPlanMeta.model_validate_json(args.output_meta.read_text(encoding="utf-8"))
        EditPlanQa.model_validate_json(args.output_qa.read_text(encoding="utf-8"))
        return bool(args.output_attribution.read_text(encoding="utf-8").strip())
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return False


def _cache_current(args: argparse.Namespace, input_fingerprint: str) -> bool:
    manifest_path = args.work_dir / "cache_manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = CacheManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return False
    if manifest.cache_version != ALGORITHM_VERSION or manifest.input_fingerprint != input_fingerprint:
        return False
    expected = {str(path.resolve()) for path in _output_paths(args)}
    if set(manifest.outputs) != expected:
        return False
    for output in _output_paths(args):
        signature = _signature(output)
        if signature is None or signature != manifest.outputs[str(output.resolve())]:
            return False
    return _outputs_valid(args)


def _commit_cache(args: argparse.Namespace, input_fingerprint: str) -> None:
    signatures: dict[str, OutputSignature] = {}
    for output in _output_paths(args):
        signature = _signature(output)
        if signature is None:
            raise PostprocessError(f"cannot cache missing output: {output}")
        signatures[str(output.resolve())] = signature
    atomic_write_json(
        args.work_dir / "cache_manifest.json",
        CacheManifest(input_fingerprint=input_fingerprint, outputs=signatures).model_dump(mode="json"),
    )


def _input_fingerprint(
    args: argparse.Namespace,
    episode_run_dirs: dict[str, Path],
    audio_manifest: AudioAssetManifest,
    audio_root: Path,
) -> str:
    files = {
        "edl": file_hash(args.edl),
        "series_review_script": file_hash(args.series_review_script),
        "event_bank": file_hash(args.event_bank),
        "beats_timing": file_hash(args.beats_timing),
        "source_map": file_hash(args.source_map),
        "audio_assets": file_hash(args.audio_assets),
        "overrides": file_hash(args.overrides) if args.overrides else None,
        "shots": {key: file_hash(run_dir / "shots.json") for key, run_dir in sorted(episode_run_dirs.items())},
        "asset_files": {
            asset.asset_id: media_identity_hash(resolve_asset_path(asset, audio_root)) for asset in audio_manifest.assets
        },
    }
    return stable_hash(
        {
            "algorithm_version": ALGORITHM_VERSION,
            "profile": args.profile,
            "seed": args.seed,
            "files": files,
        }
    )


def run_postprocess(args: argparse.Namespace) -> int:
    args.edl = args.edl.expanduser().resolve()
    args.series_review_script = args.series_review_script.expanduser().resolve()
    args.event_bank = args.event_bank.expanduser().resolve()
    args.beats_timing = args.beats_timing.expanduser().resolve()
    args.source_map = args.source_map.expanduser().resolve()
    args.audio_assets = args.audio_assets.expanduser().resolve()
    args.overrides = args.overrides.expanduser().resolve() if args.overrides else None
    args.output = args.output.expanduser().resolve()
    args.output_meta = args.output_meta.expanduser().resolve()
    args.output_qa = args.output_qa.expanduser().resolve()
    args.output_attribution = args.output_attribution.expanduser().resolve()
    args.work_dir = args.work_dir.expanduser().resolve()
    episode_run_dirs = parse_episode_run_dirs(args.episode_run_dir)
    audio_manifest = validate_audio_assets(args.audio_assets)
    _, audio_root = load_audio_manifest(args.audio_assets)
    input_fingerprint = _input_fingerprint(args, episode_run_dirs, audio_manifest, audio_root)
    if not args.force and _cache_current(args, input_fingerprint):
        logging.info("Using existing postprocess outputs")
        return 0

    logging.info("Postprocess: validating timeline inputs (10%)")
    placements = validate_edl([EdlPlacement.model_validate(item) for item in _load_json(args.edl)])
    beats = validate_series_review_script(
        [SeriesReviewBeat.model_validate(item) for item in _load_json(args.series_review_script)]
    )
    timings_raw = [BeatTiming.model_validate(item) for item in _load_json(args.beats_timing)]
    pause_s = max(0.0, timings_raw[1].tl_start - timings_raw[0].tl_end) if len(timings_raw) > 1 else 0.0
    timings = validate_beats_timing(timings_raw, pause_s=round(pause_s, 3))
    event_bank = SeriesEventBank.model_validate(_load_json(args.event_bank))
    source_map = EdlSourceMap.model_validate(_load_json(args.source_map))
    missing_sources = sorted({placement.src for placement in placements} - set(source_map.sources))
    if missing_sources:
        raise PostprocessError(f"source map is missing EDL sources: {missing_sources}")
    shots_by_episode = {key: load_shots(run_dir / "shots.json") for key, run_dir in episode_run_dirs.items()}
    overrides = _load_overrides(args.overrides)
    logging.info("Postprocess: planning visual and audio cues (45%)")
    plan, meta, qa, attribution = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=event_bank,
        shots_by_episode=shots_by_episode,
        audio_manifest=audio_manifest,
        input_fingerprint=input_fingerprint,
        seed=args.seed,
        profile=args.profile,
        overrides=overrides,
    )
    atomic_write_json(args.output, plan.model_dump(mode="json"))
    atomic_write_json(args.output_meta, meta.model_dump(mode="json"))
    atomic_write_json(args.output_qa, qa.model_dump(mode="json"))
    _atomic_write_text(args.output_attribution, attribution)
    logging.info("Postprocess: validating outputs (90%)")
    if not _outputs_valid(args):
        raise PostprocessError("postprocess outputs failed validation")
    _commit_cache(args, input_fingerprint)
    logging.info("Postprocess complete (100%)")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_postprocess(args)
    except (AudioAssetError, PostprocessError, ValidationError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        parser.exit(2, f"postprocess: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
