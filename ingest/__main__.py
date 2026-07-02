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
    TranscriptQuality,
    TranscriptSegment,
    VisionSegment,
    validate_film_map,
    write_json,
)
from ingest.asr import apply_alignment, clean_aligned_segments, detect_transcript_warnings, parse_manual_transcript, split_long_segments
from ingest.cache import StageCache
from ingest.correction import OpenAITranscriptCorrector, apply_glossary_replacements, load_glossary
from ingest.film_map import build_film_map
from ingest.gaps import detect_silent_gaps, select_gaps_for_vision
from ingest.llm import OpenAIIngestClient
from ingest.transcribe import transcribe_korean, transcribe_openai_chunked, transcribe_openai_gpt4o
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
    parser.add_argument("--asr-provider", default="faster-whisper", choices=["faster-whisper", "openai-gpt4o", "openai-gpt4o-hybrid", "manual"])
    parser.add_argument("--aligner", default="none", choices=["none", "whisperx", "qwen3"])
    parser.add_argument("--transcript-input", default=None, type=Path)
    parser.add_argument("--timecode-quality", default="strict", choices=["strict", "approximate"])
    parser.add_argument("--max-segment-s", default=30.0, type=float)
    parser.add_argument("--merge-gap-s", default=0.0, type=float)
    parser.add_argument("--openai-transcribe-model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--openai-chunk-s", default=20.0, type=float)
    parser.add_argument("--alignment-device", default="cuda", choices=["cpu", "cuda"])
    parser.add_argument("--transcript-correction", default="off", choices=["off", "glossary", "openai"])
    parser.add_argument("--glossary", default=None, type=Path, help="JSON/YAML/TXT glossary for transcript name/entity correction")
    parser.add_argument("--correction-model", default="gpt-4.1-mini")
    parser.add_argument("--vad-filter", action="store_true", default=True)
    parser.add_argument("--no-vad-filter", dest="vad_filter", action="store_false")
    parser.add_argument("--work-dir", default=Path("work"), type=Path)
    parser.add_argument("--force", action="store_true", help="Rebuild stage artifacts")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def load_transcript(
    cache: StageCache,
    audio_path: Path,
    duration: float,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> tuple[list[TranscriptSegment], TranscriptQuality]:
    if cache.has("transcript_aligned.json") and cache.has("transcript_quality.json"):
        logger.info("[2/6] Using cached transcript_aligned.json")
        segments = [TranscriptSegment.model_validate(item) for item in cache.read_json("transcript_aligned.json")]
        quality = TranscriptQuality.model_validate(cache.read_json("transcript_quality.json"))
        return segments, quality

    for key, value in {"openai_transcribe_model": "gpt-4o-mini-transcribe", "openai_chunk_s": 20.0, "alignment_device": "cuda"}.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    logger.info("[2/6] Loading transcript with ASR provider: %s", args.asr_provider)
    if args.asr_provider == "manual":
        if args.transcript_input is None:
            raise IngestError("--transcript-input is required when --asr-provider manual")
        transcript, quality = parse_manual_transcript(args.transcript_input.expanduser().resolve(), duration)
    elif args.asr_provider == "openai-gpt4o":
        transcript, approximate = transcribe_openai_gpt4o(audio_path, duration, model=args.openai_transcribe_model)
        quality = TranscriptQuality(
            asr_provider="openai-gpt4o",
            aligner_provider="none",
            timecode_quality="approximate" if approximate else "strict",
            approximate_timecodes=approximate,
            warnings=["OpenAI transcription returned text without segment timestamps; duration was inferred"] if approximate else [],
        )
    elif args.asr_provider == "openai-gpt4o-hybrid":
        transcript = transcribe_openai_chunked(
            audio_path,
            duration,
            cache.path("openai_chunks"),
            model=args.openai_transcribe_model,
            chunk_s=args.openai_chunk_s,
        )
        quality = TranscriptQuality(
            asr_provider="openai-gpt4o-hybrid",
            aligner_provider="none",
            timecode_quality="approximate",
            approximate_timecodes=True,
            warnings=["OpenAI chunked transcription uses rough chunk timestamps before alignment"],
        )
    else:
        try:
            transcript = transcribe_korean(audio_path, args.whisper_model, args.device, vad_filter=args.vad_filter)
        except TypeError:
            transcript = transcribe_korean(audio_path, args.whisper_model, args.device)
        quality = TranscriptQuality(asr_provider="faster-whisper", aligner_provider="none", timecode_quality="strict", approximate_timecodes=False)

    if not transcript:
        raise IngestError("transcript is empty")
    transcript = split_long_segments(transcript, args.max_segment_s)
    transcript, quality = apply_alignment(transcript, quality, args.aligner, args.timecode_quality, audio_path=audio_path, alignment_device=args.alignment_device)
    transcript, qc_warnings = clean_aligned_segments(transcript, duration=duration, min_segment_s=0.45, max_segment_s=args.max_segment_s or 30.0)
    cache.write_json("transcript_text.json", transcript)
    transcript, quality = correct_transcript(cache, transcript, quality, args, logger)
    warnings = quality.warnings + qc_warnings + quality.correction_warnings + detect_transcript_warnings(transcript)
    quality = quality.model_copy(update={"warnings": warnings})
    cache.write_json("transcript_aligned.json", transcript)
    cache.write_json("transcript_quality.json", quality)
    return transcript, quality

def correct_transcript(
    cache: StageCache,
    transcript: list[TranscriptSegment],
    quality: TranscriptQuality,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> tuple[list[TranscriptSegment], TranscriptQuality]:
    mode = getattr(args, "transcript_correction", "off")
    if mode == "off":
        return transcript, quality.model_copy(update={"correction_mode": "off", "correction_model": None, "correction_warnings": []})
    if cache.has("transcript_corrected.json"):
        logger.info("[2/6] Using cached transcript_corrected.json")
        corrected = [TranscriptSegment.model_validate(item) for item in cache.read_json("transcript_corrected.json")]
        return corrected, quality.model_copy(update={"correction_mode": mode, "correction_model": getattr(args, "correction_model", None) if mode == "openai" else None})
    glossary = load_glossary(getattr(args, "glossary", None))
    logger.info("[2/6] Correcting transcript with mode: %s", mode)
    corrected, correction_warnings = apply_glossary_replacements(transcript, glossary)
    correction_model = None
    if mode == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise IngestError("OPENAI_API_KEY is required for --transcript-correction openai")
        correction_model = getattr(args, "correction_model", "gpt-4.1-mini")
        corrected, openai_warnings = OpenAITranscriptCorrector(api_key, correction_model).correct_segments(corrected, glossary)
        correction_warnings.extend(openai_warnings)
    corrected = [segment.model_copy(update={"id": index}) for index, segment in enumerate(corrected)]
    cache.write_json("transcript_corrected.json", corrected)
    return corrected, quality.model_copy(update={"correction_mode": mode, "correction_model": correction_model, "correction_warnings": correction_warnings})

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

    for key, value in {
        "asr_provider": "faster-whisper",
        "aligner": "none",
        "transcript_input": None,
        "timecode_quality": "strict",
        "max_segment_s": 30.0,
        "merge_gap_s": 0.0,
        "vad_filter": True,
        "openai_transcribe_model": "gpt-4o-mini-transcribe",
        "openai_chunk_s": 20.0,
        "alignment_device": "cuda",
        "transcript_correction": "off",
        "glossary": None,
        "correction_model": "gpt-4.1-mini",
    }.items():
        if not hasattr(args, key):
            setattr(args, key, value)

    if not input_path.is_file():
        raise IngestError(f"Input video does not exist: {input_path}")
    if args.gap_threshold < 0:
        raise IngestError("--gap-threshold must be >= 0")
    if args.max_vision_frames < 0:
        raise IngestError("--max-vision-frames must be >= 0")
    if args.max_segment_s < 0:
        raise IngestError("--max-segment-s must be >= 0")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise IngestError("OPENAI_API_KEY is required for translation and vision")
    if args.asr_provider == "openai-gpt4o" and not api_key:
        raise IngestError("OPENAI_API_KEY is required for openai-gpt4o ASR")

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
    transcript, transcript_quality = load_transcript(cache, audio_path, duration, args, logger)
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
        warnings_count=warnings_count + len(transcript_quality.warnings),
        asr_provider=transcript_quality.asr_provider,
        aligner_provider=transcript_quality.aligner_provider,
        timecode_quality=transcript_quality.timecode_quality,
        approximate_timecodes=transcript_quality.approximate_timecodes,
        asr_warnings=transcript_quality.warnings,
        transcript_correction_mode=transcript_quality.correction_mode,
        transcript_correction_model=transcript_quality.correction_model,
        transcript_correction_warnings=transcript_quality.correction_warnings,
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
