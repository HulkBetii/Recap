from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from common.integrity import file_hash
from common.media import MediaError, require_ffmpeg
from common.schema import CommentaryFitRequests, CommentaryScript, ReactionSource
from reaction_remix.tts.core import CommentaryTtsError, ReactionTtsSettings, synthesize_commentary
from reaction_remix.tts.asr import JapaneseAsrVerifier
from tts.providers import TtsProviderError

REPAIR_REQUIRED_EXIT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction Remix R5: strict Japanese AI33 commentary TTS")
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fit-request-output", required=True, type=Path)
    parser.add_argument("--fit-request", type=Path, default=None, help="Previous fit request used to increment per-slot repair attempts")
    parser.add_argument("--work-dir", type=Path, default=Path("work/reaction-remix/tts"))
    parser.add_argument("--trim-handle-ms", type=int, default=80)
    parser.add_argument("--target-lufs", type=float, default=-14.0)
    parser.add_argument("--max-true-peak-db", type=float, default=-2.0)
    parser.add_argument("--asr-model", default="large-v3")
    parser.add_argument("--min-asr-similarity", type=float, default=0.90)
    parser.add_argument("--fit-tolerance-s", type=float, default=0.10)
    parser.add_argument("--max-fit-iterations", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return parser


async def run(args: argparse.Namespace) -> int:
    require_ffmpeg()
    script = CommentaryScript.model_validate_json(args.script.read_text(encoding="utf-8"))
    source = ReactionSource.model_validate_json(args.source.read_text(encoding="utf-8"))
    previous = None
    if args.fit_request and args.fit_request.is_file():
        previous = CommentaryFitRequests.model_validate_json(args.fit_request.read_text(encoding="utf-8"))
    script_hash = file_hash(args.script.expanduser().resolve())
    if script_hash is None:
        raise CommentaryTtsError(f"could not hash commentary script: {args.script}")
    settings = ReactionTtsSettings(
        trim_handle_ms=args.trim_handle_ms,
        target_lufs=args.target_lufs,
        max_true_peak_db=args.max_true_peak_db,
        min_asr_similarity=args.min_asr_similarity,
        fit_tolerance_s=args.fit_tolerance_s,
        max_fit_iterations=args.max_fit_iterations,
    )
    _audio, requests = await synthesize_commentary(
        script,
        source,
        output_path=args.output.expanduser().resolve(),
        fit_request_path=args.fit_request_output.expanduser().resolve(),
        work_dir=args.work_dir.expanduser().resolve(),
        settings=settings,
        asr_verifier=JapaneseAsrVerifier(model_name=args.asr_model),
        previous_fit_requests=previous,
        script_hash=script_hash,
        force=args.force,
    )
    return REPAIR_REQUIRED_EXIT if requests.requests else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run(args))
    except (CommentaryTtsError, TtsProviderError, MediaError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"reaction-remix tts: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
