from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from common.media import MediaError, extract_audio, probe_duration, require_ffmpeg
from common.schema import (
    FilmMapMeta,
    TranslatedSegment,
    TranscriptSegment,
    VisionSegment,
    validate_film_map,
    write_json,
)
from ingest.cache import StageCache
from ingest.film_map import build_film_map
from ingest.gaps import detect_silent_gaps, select_gaps_for_vision
from ingest.llm import OpenAIIngestClient
from ingest.transcribe import transcribe_korean
from ingest.vision import describe_gaps

DEFAULT_TRANSLATE_MODEL = "gpt-4.1-mini"
DEFAULT_VISION_MODEL = "gpt-4.1-mini"
DEFAULT_WHISPER_MODEL = "large-v3"
DEFAULT_GAP_THRESHOLD = 4.0
DEFAULT_MAX_VISION_FRAMES = 200


class IngestError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 1 ingest: film.mp4 -> film_map.json")
    parser.add_argument("--input", required=True, type=Path, help="Input film video path")
    parser.add_argument("--output", required=True, type=Path, help="Output film_map.json path")
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--gap-threshold", default=DEFAULT_GAP_THRESHOLD, type=float)
    parser.add_argument("--max-vision-frames", default=DEFAULT_MAX_VISION_FRAMES, type=int)
    parser.add_argument("--translate-model", default=DEFAULT_TRANSLATE_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--work-dir", default=Path("work"), type=Path)
    parser.add_argument("--force", action="store_true", help="Rebuild stage artifacts")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def load_transcript(cache: StageCache, audio_path: Path, whisper_model: str, device: str, logger: logging.Logger) -> list[TranscriptSegment]:
    if cache.has("transcript_raw.json"):
        logger.info("[2/6] Using cached transcript_raw.json")
        return [TranscriptSegment.model_validate(item) for item in cache.read_json("transcript_raw.json")]
    logger.info("[2/6] Transcribing Korean audio with faster-whisper")
    segments = transcribe_korean(audio_path, whisper_model, device)
    cache.write_json("transcript_raw.json", segments)
    return segments


def load_translations(
    cache: StageCache,
    transcript: list[TranscriptSegment],
    client: OpenAIIngestClient,
    logger: logging.Logger,
) -> tuple[list[TranslatedSegment], int]:
    if cache.has("translated.json"):
        logger.info("[3/6] Using cached translated.json")
        return [TranslatedSegment.model_validate(item) for item in cache.read_json("translated.json")], 0
    logger.info("[3/6] Translating KO -> EN with stable segment ids")
    translated, warnings_count = client.translate_segments(transcript, logger=logger)
    cache.write_json("translated.json", translated)
    return translated, warnings_count


def load_vision(
    *,
    cache: StageCache,
    input_path: Path,
    translated: list[TranslatedSegment],
    duration: float,
    gap_threshold: float,
    max_vision_frames: int,
    client: OpenAIIngestClient,
    logger: logging.Logger,
) -> tuple[list[VisionSegment], int]:
    if cache.has("vision.json"):
        logger.info("[5/6] Using cached vision.json")
        return [VisionSegment.model_validate(item) for item in cache.read_json("vision.json")], 0
    logger.info("[4/6] Detecting silent gaps")
    gaps = detect_silent_gaps(translated, duration, gap_threshold)
    selected_gaps = select_gaps_for_vision(gaps, max_vision_frames)
    logger.info("[5/6] Running vision on %d/%d gaps", len(selected_gaps), len(gaps))
    vision_segments, warnings_count = describe_gaps(
        input_path=input_path,
        gaps=selected_gaps,
        frames_dir=cache.path("frames"),
        client=client,
        logger=logger,
    )
    cache.write_json("vision.json", vision_segments)
    return vision_segments, warnings_count


def run_ingest(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    logger = logging.getLogger("ingest")

    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()

    if not input_path.is_file():
        raise IngestError(f"Input video does not exist: {input_path}")
    if args.gap_threshold < 0:
        raise IngestError("--gap-threshold must be >= 0")
    if args.max_vision_frames < 0:
        raise IngestError("--max-vision-frames must be >= 0")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise IngestError("OPENAI_API_KEY is required for translation and vision")

    require_ffmpeg()
    cache = StageCache(work_dir, force=args.force)
    cache.prepare()
    client = OpenAIIngestClient(api_key, args.translate_model, args.vision_model)

    logger.info("[0/6] Probing input video")
    duration = probe_duration(input_path)
    audio_path = cache.path("audio.wav")
    if cache.has("audio.wav"):
        logger.info("[1/6] Using cached audio.wav")
    else:
        logger.info("[1/6] Extracting mono 16kHz audio")
        extract_audio(input_path, audio_path)

    warnings_count = 0
    transcript = load_transcript(cache, audio_path, args.whisper_model, args.device, logger)
    translated, translation_warnings = load_translations(cache, transcript, client, logger)
    warnings_count += translation_warnings
    vision_segments, vision_warnings = load_vision(
        cache=cache,
        input_path=input_path,
        translated=translated,
        duration=duration,
        gap_threshold=args.gap_threshold,
        max_vision_frames=args.max_vision_frames,
        client=client,
        logger=logger,
    )
    warnings_count += vision_warnings

    logger.info("[6/6] Building and validating film_map.json")
    film_map = build_film_map(translated, vision_segments, duration)
    validate_film_map(film_map, duration)
    write_json(output_path, film_map)

    meta = FilmMapMeta(
        input_path=str(input_path),
        duration=duration,
        created_at=datetime.now(timezone.utc),
        whisper_model=args.whisper_model,
        translate_model=args.translate_model,
        vision_model=args.vision_model,
        gap_threshold=args.gap_threshold,
        max_vision_frames=args.max_vision_frames,
        speech_count=sum(1 for item in film_map if item.type == "speech"),
        visual_count=sum(1 for item in film_map if item.type == "visual"),
        cache_hits=cache.cache_hits,
        warnings_count=warnings_count,
    )
    write_json(output_path.with_name(f"{output_path.stem}.meta.json"), meta)
    logger.info("Done: %s", output_path)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_ingest(args)
    except (IngestError, MediaError, ValueError) as exc:
        parser.exit(2, f"ingest: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
