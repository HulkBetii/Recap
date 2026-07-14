from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path

from common.integrity import file_hash, media_identity_hash, stable_hash
from common.media import MediaError, require_ffmpeg
from common.schema import ReactionSource, ReactionTranscript
from reaction_remix._artifacts import load_current_artifact, write_artifact
from reaction_remix.analyze.core import AnalyzeSettings, ReactionAnalyzeError, analyze_reaction

ANALYZE_ALGORITHM_VERSION = "reaction-analyze-v5"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction-remix R1: multilingual ASR, language and speaker analysis")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-dir", default=Path("work/reaction-remix/analyze"), type=Path)
    parser.add_argument("--whisper-model", "--model", dest="model", default="large-v3", choices=["large-v3"])
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="default")
    parser.add_argument("--source-language", default="auto", choices=["auto"])
    parser.add_argument("--max-region-s", default=30.0, type=float)
    parser.add_argument("--multilingual-window-s", default=6.0, type=float)
    parser.add_argument("--region-overlap-s", "--overlap-s", dest="overlap_s", default=2.0, type=float)
    parser.add_argument("--speech-padding-s", default=0.12, type=float)
    parser.add_argument("--max-attempts", default=2, type=int)
    parser.add_argument("--silence-noise-db", default=-35.0, type=float)
    parser.add_argument("--min-silence-s", default=0.35, type=float)
    parser.add_argument("--language-min-confidence", default=0.70, type=float)
    parser.add_argument(
        "--speaker-cluster-threshold",
        "--speaker-threshold",
        dest="speaker_threshold",
        default=0.28,
        type=float,
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def run_analyze(args: argparse.Namespace) -> int:
    require_ffmpeg()
    input_path = args.input.expanduser().resolve()
    source_path = args.source.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    meta_path = output_path.with_name("reaction_transcript.meta.json")
    if not input_path.is_file() or not source_path.is_file():
        raise ReactionAnalyzeError("input video and reaction_source.json must exist")
    source = ReactionSource.model_validate_json(source_path.read_text(encoding="utf-8"))
    if source.input_hash != media_identity_hash(input_path):
        raise ReactionAnalyzeError("source media identity does not match reaction_source.json")
    if args.source_language != "auto":
        raise ReactionAnalyzeError("reaction-remix.v1 requires automatic per-utterance language detection")
    if args.speech_padding_s < 0:
        raise ReactionAnalyzeError("speech_padding_s must be non-negative")
    settings = AnalyzeSettings(
        model=args.model,
        device=args.device,
        compute_type=args.compute_type,
        max_region_s=args.max_region_s,
        multilingual_window_s=args.multilingual_window_s,
        overlap_s=args.overlap_s,
        max_attempts=args.max_attempts,
        silence_noise_db=args.silence_noise_db,
        min_silence_s=args.min_silence_s,
        language_min_confidence=args.language_min_confidence,
        speaker_threshold=args.speaker_threshold,
    )
    settings.validate()
    source_file_hash = file_hash(source_path)
    if source_file_hash is None:
        raise ReactionAnalyzeError("could not hash reaction_source.json")
    input_hashes = {"source_media": source.input_hash, "reaction_source": source_file_hash}
    config_hash = stable_hash(asdict(settings))
    if not args.force:
        cached = load_current_artifact(
            output_path,
            meta_path,
            ReactionTranscript,
            stage="analyze",
            algorithm_version=ANALYZE_ALGORITHM_VERSION,
            input_hashes=input_hashes,
            config_hash=config_hash,
        )
        if cached is not None:
            logging.getLogger("reaction_remix.analyze").info("Analysis output is current; skipping")
            return 0
    transcript, warnings = analyze_reaction(
        input_path,
        source,
        work_dir,
        settings=settings,
        force=args.force,
    )
    write_artifact(
        output_path,
        meta_path,
        transcript,
        stage="analyze",
        algorithm_version=ANALYZE_ALGORITHM_VERSION,
        input_hashes=input_hashes,
        config_hash=config_hash,
        warnings=warnings,
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_analyze(args)
    except (ReactionAnalyzeError, MediaError, OSError, ValueError) as exc:
        parser.exit(2, f"reaction-remix analyze: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
