from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from common.integrity import file_hash, stable_hash
from common.schema import ReactionBlocks, ReactionSource, ReactionTranscript
from reaction_remix._artifacts import load_current_artifact, write_artifact
from reaction_remix.segment.blocks import SegmentSettings, build_reaction_blocks
from reaction_remix.segment.review_html import write_blocks_review_html

SEGMENT_ALGORITHM_VERSION = "reaction-segment-v7"


def _boundary_policy(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized not in {"strict", "strict-or-word-edge"}:
        raise argparse.ArgumentTypeError("boundary policy must be strict or strict-or-word-edge")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction-remix R2: conservative full-timeline block segmentation")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--shots", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--review-html", type=Path)
    parser.add_argument("--work-dir", default=Path("work/reaction-remix/segment"), type=Path)
    parser.add_argument("--min-silence-s", default=0.25, type=float)
    parser.add_argument("--speech-padding-s", default=0.12, type=float)
    parser.add_argument("--scene-cut-tolerance-s", "--scene-window-s", dest="scene_window_s", default=0.5, type=float)
    parser.add_argument("--min-cut-spacing-s", default=0.08, type=float)
    parser.add_argument("--commentary-min-confidence", default=0.90, type=float)
    parser.add_argument("--narrator-min-regions", default=3, type=int)
    parser.add_argument("--narrator-min-japanese-ratio", default=0.90, type=float)
    parser.add_argument("--broll-gap-s", default=1.5, type=float)
    parser.add_argument(
        "--commentary-boundary-policy",
        dest="boundary_policy",
        default="strict-or-word-edge",
        type=_boundary_policy,
        choices=["strict", "strict-or-word-edge"],
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def _load_shots(path: Path | None) -> tuple[list[dict[str, Any]], list[float]]:
    if path is None:
        return [], []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("shots.json must contain a list")
    boundaries = sorted({float(item["tc_start"]) for item in payload if float(item.get("tc_start", 0.0)) > 0})
    return payload, boundaries


def run_segment(args: argparse.Namespace) -> int:
    source_path = args.source.expanduser().resolve()
    transcript_path = args.transcript.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    review_path = (args.review_html or output_path.with_name("reaction_blocks.review.html")).expanduser().resolve()
    meta_path = output_path.with_name("reaction_blocks.meta.json")
    if not source_path.is_file() or not transcript_path.is_file():
        raise ValueError("reaction_source.json and reaction_transcript.json must exist")
    source = ReactionSource.model_validate_json(source_path.read_text(encoding="utf-8"))
    transcript = ReactionTranscript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
    shots_path = args.shots.expanduser().resolve() if args.shots else None
    if shots_path is not None and not shots_path.is_file():
        raise ValueError(f"shots file does not exist: {shots_path}")
    shots, scene_boundaries = _load_shots(shots_path)
    settings = SegmentSettings(
        min_silence_s=args.min_silence_s,
        speech_padding_s=args.speech_padding_s,
        scene_window_s=args.scene_window_s,
        min_cut_spacing_s=args.min_cut_spacing_s,
        commentary_min_confidence=args.commentary_min_confidence,
        narrator_min_regions=args.narrator_min_regions,
        narrator_min_japanese_ratio=args.narrator_min_japanese_ratio,
        broll_gap_s=args.broll_gap_s,
        boundary_policy=_boundary_policy(args.boundary_policy),
    )
    settings.validate()
    source_hash = file_hash(source_path)
    transcript_hash = file_hash(transcript_path)
    if source_hash is None or transcript_hash is None:
        raise ValueError("could not hash segment input artifacts")
    input_hashes = {"reaction_source": source_hash, "reaction_transcript": transcript_hash}
    if shots_path is not None:
        shots_hash = file_hash(shots_path)
        if shots_hash is None:
            raise ValueError("could not hash shots.json")
        input_hashes["shots"] = shots_hash
    config_hash = stable_hash(asdict(settings))
    if not args.force:
        cached = load_current_artifact(
            output_path,
            meta_path,
            ReactionBlocks,
            stage="segment",
            algorithm_version=SEGMENT_ALGORITHM_VERSION,
            input_hashes=input_hashes,
            config_hash=config_hash,
        )
        if cached is not None and review_path.is_file():
            logging.getLogger("reaction_remix.segment").info("Segment output is current; skipping")
            return 0
    blocks = build_reaction_blocks(
        source,
        transcript,
        scene_boundaries=scene_boundaries,
        settings=settings,
    )
    blocks = blocks.model_copy(update={"transcript_hash": transcript_hash})
    write_artifact(
        output_path,
        meta_path,
        blocks,
        stage="segment",
        algorithm_version=SEGMENT_ALGORITHM_VERSION,
        input_hashes=input_hashes,
        config_hash=config_hash,
        warnings=blocks.warnings,
    )
    write_blocks_review_html(
        review_path,
        blocks,
        transcript,
        shots=shots,
        shots_base=shots_path.parent if shots_path else None,
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_segment(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"reaction-remix segment: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
