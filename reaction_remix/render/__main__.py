from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from common.integrity import atomic_write_json, file_hash, media_identity_hash, stable_hash
from common.media import require_ffmpeg
from common.schema import (
    ReactionSource,
    RemixCommandManifest,
    RemixEdl,
    RemixRenderMeta,
    RemixRenderTimeline,
    RemixRepairRequests,
    validate_remix_command_manifest,
    validate_remix_edl,
    validate_remix_render_timeline,
)

from reaction_remix.render.commands import RemixRenderError
from reaction_remix.render.engine import RENDER_ALGORITHM_VERSION, render_remix


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction-remix R7: render interleaved source audio and Japanese TTS")
    parser.add_argument("--film", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--edl", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-dir", default=Path("work/reaction-remix/render"), type=Path)
    parser.add_argument("--timeline-output", type=Path)
    parser.add_argument("--command-manifest", type=Path)
    parser.add_argument("--meta-output", type=Path)
    parser.add_argument("--repair-request", type=Path)
    parser.add_argument("--crf", default=18, type=int)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--audio-bitrate", default="192k")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def _cache_current(cache_path: Path, identity: dict[str, object], outputs: list[Path]) -> bool:
    if not cache_path.is_file() or not all(path.is_file() for path in outputs):
        return False
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("identity") == identity and payload.get("outputs") == {
        path.name: file_hash(path) for path in outputs
    }


def run_render(args: argparse.Namespace) -> int:
    logger = logging.getLogger("reaction_remix.render")
    require_ffmpeg()
    film_path = args.film.expanduser().resolve()
    source_path = args.source.expanduser().resolve()
    edl_path = args.edl.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    if not film_path.is_file() or not source_path.is_file() or not edl_path.is_file():
        raise RemixRenderError("film, reaction_source.json and remix_edl.json must exist")
    source = ReactionSource.model_validate_json(source_path.read_text(encoding="utf-8"))
    edl = RemixEdl.model_validate_json(edl_path.read_text(encoding="utf-8"))
    if source.input_hash != media_identity_hash(film_path) or edl.source_hash != source.input_hash:
        raise RemixRenderError("source identity mismatch")
    if source.video.frame_rate_mode != "cfr":
        raise RemixRenderError("reaction-remix.v1 requires a CFR source; VFR input is not renderable")
    validate_remix_edl(edl, source=source)

    timeline_path = (args.timeline_output or output_path.with_name("render.timeline.json")).expanduser().resolve()
    manifest_path = (args.command_manifest or output_path.with_name("render.command-manifest.json")).expanduser().resolve()
    meta_path = (args.meta_output or output_path.with_name("render.meta.json")).expanduser().resolve()
    identity = {
        "source": file_hash(source_path),
        "edl": file_hash(edl_path),
        "film": media_identity_hash(film_path),
        "config": stable_hash(
            {
                "algorithm": RENDER_ALGORITHM_VERSION,
                "crf": args.crf,
                "preset": args.preset,
                "audio_bitrate": args.audio_bitrate,
            }
        ),
        "repair_request": file_hash(args.repair_request.expanduser().resolve()) if args.repair_request else None,
    }
    cache_path = work_dir / "render_cache_manifest.json"
    required_outputs = [output_path, timeline_path, manifest_path, meta_path]
    if not args.force and _cache_current(cache_path, identity, required_outputs):
        try:
            cached_timeline = RemixRenderTimeline.model_validate_json(timeline_path.read_text(encoding="utf-8"))
            cached_manifest = RemixCommandManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            RemixRenderMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
            validate_remix_render_timeline(cached_timeline, edl=edl)
            validate_remix_command_manifest(cached_manifest)
        except (OSError, ValueError):
            logger.warning("Render cache metadata is invalid; rebuilding")
        else:
            logger.info("Render output is current; skipping")
            return 0

    bypass_placement_ids: set[str] = set()
    if args.repair_request:
        repairs = RemixRepairRequests.model_validate_json(args.repair_request.read_text(encoding="utf-8"))
        if repairs.source_hash != source.input_hash:
            raise RemixRenderError("repair request source hash does not match source")
        bypass_placement_ids = {
            affected_id
            for item in repairs.items
            if item.kind == "reaction_media_mismatch"
            for affected_id in item.affected_ids
        }
    timeline_payload, manifest_payload, meta_payload = render_remix(
        film_path=film_path,
        edl=edl,
        edl_hash=file_hash(edl_path) or "",
        output_path=output_path,
        work_dir=work_dir,
        force=args.force,
        crf=args.crf,
        preset=args.preset,
        audio_bitrate=args.audio_bitrate,
        bypass_placement_ids=bypass_placement_ids,
    )
    timeline = RemixRenderTimeline.model_validate(timeline_payload)
    validate_remix_render_timeline(timeline, edl=edl)
    manifest = RemixCommandManifest.model_validate(manifest_payload)
    validate_remix_command_manifest(manifest)
    atomic_write_json(timeline_path, timeline.model_dump(mode="json"))
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
    meta_payload["timeline_hash"] = file_hash(timeline_path)
    meta_payload["command_manifest_hash"] = file_hash(manifest_path)
    meta = RemixRenderMeta.model_validate(meta_payload)
    atomic_write_json(meta_path, meta.model_dump(mode="json"))
    atomic_write_json(
        cache_path,
        {
            "identity": identity,
            "outputs": {path.name: file_hash(path) for path in required_outputs},
        },
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_render(args)
    except (RemixRenderError, ValueError, OSError) as exc:
        parser.exit(2, f"reaction-remix render: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
