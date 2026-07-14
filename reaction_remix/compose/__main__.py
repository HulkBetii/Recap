from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from common.integrity import atomic_write_json, file_hash, media_identity_hash, stable_hash
from common.schema import AudioAssets, CommentaryAudio, ReactionBlocks, ReactionSource, ReactionStageMeta, RemixPlan, RemixRepairRequests, validate_remix_edl, validate_remix_plan

from reaction_remix.compose.cache import output_is_current
from reaction_remix.compose.composer import ComposeError, compose_remix


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction-remix R6: compose audio-aware remix EDL")
    parser.add_argument("--film", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--blocks", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--commentary-audio", required=True, type=Path)
    parser.add_argument("--audio-assets", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repair-request", type=Path)
    parser.add_argument("--repair-overrides", type=Path)
    parser.add_argument("--work-dir", default=Path("work/reaction-remix/compose"), type=Path)
    parser.add_argument("--tts-gain-db", default=1.0, type=float)
    parser.add_argument("--bed-gain-db", default=-14.0, type=float)
    parser.add_argument("--boundary-fade-ms", default=50, type=int)
    parser.add_argument("--bed-fade-ms", default=180, type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def run_compose(args: argparse.Namespace) -> int:
    logger = logging.getLogger("reaction_remix.compose")
    paths = [args.film, args.source, args.blocks, args.plan, args.commentary_audio]
    for path in paths:
        if not path.expanduser().resolve().is_file():
            raise ComposeError(f"required input does not exist: {path}")
    output_path = args.output.expanduser().resolve()
    meta_path = output_path.with_name("remix_edl.meta.json")
    input_hashes = {path.name: file_hash(path.expanduser().resolve()) for path in paths}
    if args.audio_assets:
        input_hashes[args.audio_assets.name] = file_hash(args.audio_assets.expanduser().resolve())
    if args.repair_overrides:
        input_hashes[args.repair_overrides.name] = file_hash(args.repair_overrides.expanduser().resolve())
    identity = {
        "input_hashes": input_hashes,
        "config_hash": stable_hash(
            {
                "tts_gain_db": args.tts_gain_db,
                "bed_gain_db": args.bed_gain_db,
                "boundary_fade_ms": args.boundary_fade_ms,
                "bed_fade_ms": args.bed_fade_ms,
                "algorithm": "reaction-compose-v2",
            }
        ),
    }
    if not args.force and output_is_current(output_path, meta_path, identity):
        logger.info("Compose output is current; skipping")
        return 0

    source = ReactionSource.model_validate_json(args.source.read_text(encoding="utf-8"))
    blocks = ReactionBlocks.model_validate_json(args.blocks.read_text(encoding="utf-8"))
    plan = RemixPlan.model_validate_json(args.plan.read_text(encoding="utf-8"))
    commentary_audio = CommentaryAudio.model_validate_json(args.commentary_audio.read_text(encoding="utf-8"))
    if source.input_hash != media_identity_hash(args.film.expanduser().resolve()):
        raise ComposeError("film identity does not match reaction_source.json")
    if blocks.source_hash != source.input_hash or plan.blocks_hash != file_hash(args.blocks.expanduser().resolve()):
        raise ComposeError("blocks or plan provenance does not match compose inputs")
    validate_remix_plan(plan, blocks)
    assets = None
    if args.audio_assets:
        assets = AudioAssets.model_validate_json(args.audio_assets.read_text(encoding="utf-8"))
    force_tts_slots: set[str] = set()
    if args.repair_overrides:
        repair_overrides = RemixRepairRequests.model_validate_json(args.repair_overrides.read_text(encoding="utf-8"))
        if repair_overrides.source_hash != source.input_hash:
            raise ComposeError("repair overrides source hash does not match source")
        force_tts_slots = {
            affected_id
            for item in repair_overrides.items
            if item.kind == "bed_leakage"
            for affected_id in item.affected_ids
        }
    edl, repair = compose_remix(
        film_path=args.film.expanduser().resolve(),
        source=source,
        blocks=blocks,
        plan=plan,
        commentary_audio=commentary_audio,
        commentary_audio_base=args.commentary_audio.expanduser().resolve().parent,
        audio_assets=assets,
        audio_assets_base=args.audio_assets.expanduser().resolve().parent if args.audio_assets else None,
        tts_gain_db=args.tts_gain_db,
        bed_gain_db=args.bed_gain_db,
        boundary_fade_ms=args.boundary_fade_ms,
        bed_fade_ms=args.bed_fade_ms,
        force_tts_slots=force_tts_slots,
        plan_hash=file_hash(args.plan.expanduser().resolve()),
        commentary_audio_hash=file_hash(args.commentary_audio.expanduser().resolve()),
    )
    if repair is not None:
        repair_path = (args.repair_request or output_path.with_name("remix_repair_requests.json")).expanduser().resolve()
        atomic_write_json(repair_path, repair.model_dump(mode="json"))
        raise ComposeError(f"commentary visual capacity requires repair: {repair_path}")
    validate_remix_edl(edl, source=source)
    atomic_write_json(output_path, edl.model_dump(mode="json"))
    meta = ReactionStageMeta.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "stage": "compose",
            "algorithm_version": "reaction-compose-v2",
            "input_hashes": input_hashes,
            "config_hash": identity["config_hash"],
            "output_hashes": {output_path.name: file_hash(output_path)},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "cache_hits": [],
            "warnings": edl.warnings,
        }
    )
    atomic_write_json(meta_path, meta.model_dump(mode="json"))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_compose(args)
    except (ComposeError, ValueError, OSError) as exc:
        parser.exit(2, f"reaction-remix compose: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
