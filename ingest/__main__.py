from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from common.media import MediaError, extract_audio, probe_duration, require_ffmpeg
from common.integrity import file_hash, media_identity_hash, stable_hash
from common.schema import (
    FilmMapMeta,
    TranslatedSegment,
    TranscriptQuality,
    TranscriptSegment,
    VideoProfile,
    VisionSegment,
    validate_film_map,
    write_json,
)
from ingest.asr import apply_alignment, clean_aligned_segments, detect_transcript_warnings, parse_manual_transcript, split_long_segments
from ingest.cache import StageCache
from ingest.correction import OpenAITranscriptCorrector, apply_glossary_replacements, load_glossary
from ingest.film_map import build_film_map
from ingest.gaps import detect_silent_gaps, select_gaps_for_vision, split_long_gaps
from ingest.integrity import (
    INGEST_CACHE_VERSION,
    audio_cache_key,
    correction_cache_key,
    ingest_config_hash,
    transcript_cache_key,
    translation_cache_key,
    vision_cache_key,
)
from ingest.llm import OpenAIIngestClient
from ingest.transcribe import transcribe_korean, transcribe_openai_chunked, transcribe_openai_gpt4o
from ingest.vision import describe_gaps

DEFAULT_TRANSLATE_MODEL = "gpt-4.1-mini"
DEFAULT_VISION_MODEL = "gpt-4.1-mini"
DEFAULT_WHISPER_MODEL = "large-v3"
DEFAULT_GAP_THRESHOLD = 4.0
DEFAULT_MAX_VISION_FRAMES = 200
DEFAULT_MAX_VISUAL_GAP_S = 20.0


class IngestError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 1 ingest: film.mp4 -> film_map.json")
    parser.add_argument("--input", required=True, type=Path, help="Input film video path")
    parser.add_argument("--output", required=True, type=Path, help="Output film_map.json path")
    parser.add_argument("--whisper-model", default=DEFAULT_WHISPER_MODEL)
    parser.add_argument("--gap-threshold", default=DEFAULT_GAP_THRESHOLD, type=float)
    parser.add_argument("--max-vision-frames", default=DEFAULT_MAX_VISION_FRAMES, type=int)
    parser.add_argument("--max-visual-gap-s", default=DEFAULT_MAX_VISUAL_GAP_S, type=float)
    parser.add_argument("--translate-model", default=DEFAULT_TRANSLATE_MODEL)
    parser.add_argument("--source-language", default="ko", choices=["ko", "vi"], help="Source speech language in the input video")
    parser.add_argument("--translate-mode", default="ko-en", choices=["ko-en", "none"], help="Translate transcript or keep source text as-is")
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
    parser.add_argument("--drop-non-korean-intro-s", default=30.0, type=float)
    parser.add_argument("--drop-visual-before-s", default=0.0, type=float, help="Drop/suppress visual gap segments before this source time, useful for episode intros/opening credits")
    parser.add_argument("--video-profile", default=None, type=Path, help="Optional GĐ0 video_profile.json used to suppress non-story visual gaps")
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

    for key, value in {"openai_transcribe_model": "gpt-4o-mini-transcribe", "openai_chunk_s": 20.0, "alignment_device": "cuda", "drop_non_korean_intro_s": 30.0, "source_language": "ko"}.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    logger.info("[2/6] Loading transcript with ASR provider: %s", args.asr_provider)
    if args.asr_provider == "manual":
        if args.transcript_input is None:
            raise IngestError("--transcript-input is required when --asr-provider manual")
        transcript, quality = parse_manual_transcript(args.transcript_input.expanduser().resolve(), duration)
    elif args.asr_provider == "openai-gpt4o":
        transcript, approximate = transcribe_openai_gpt4o(audio_path, duration, model=args.openai_transcribe_model, language=args.source_language)
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
            language=args.source_language,
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
            transcript = transcribe_korean(
                audio_path,
                args.whisper_model,
                args.device,
                vad_filter=args.vad_filter,
                language=args.source_language,
                duration=duration,
                chunks_dir=cache.path("local_asr_chunks"),
            )
        except TypeError:
            transcript = transcribe_korean(audio_path, args.whisper_model, args.device)
        quality = TranscriptQuality(asr_provider="faster-whisper", aligner_provider="none", timecode_quality="strict", approximate_timecodes=False)

    if not transcript:
        raise IngestError("transcript is empty")
    transcript = split_long_segments(transcript, args.max_segment_s)
    transcript, quality = apply_alignment(transcript, quality, args.aligner, args.timecode_quality, audio_path=audio_path, alignment_device=args.alignment_device, source_language=args.source_language)
    transcript, qc_warnings = clean_aligned_segments(
        transcript,
        duration=duration,
        min_segment_s=0.45,
        max_segment_s=args.max_segment_s or 30.0,
        drop_non_korean_intro_s=args.drop_non_korean_intro_s,
    )
    cache.write_json("transcript_text.json", transcript)
    warnings = quality.warnings + qc_warnings + detect_transcript_warnings(transcript)
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
        correction_meta = cache.read_json("transcript_correction.meta.json")
        correction_warnings = list(correction_meta.get("warnings", []))
        return corrected, quality.model_copy(update={
            "correction_mode": mode,
            "correction_model": correction_meta.get("model"),
            "correction_warnings": correction_warnings,
            "warnings": quality.warnings + correction_warnings,
        })
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
    cache.write_json("transcript_correction.meta.json", {"mode": mode, "model": correction_model, "warnings": correction_warnings})
    return corrected, quality.model_copy(update={
        "correction_mode": mode,
        "correction_model": correction_model,
        "correction_warnings": correction_warnings,
        "warnings": quality.warnings + correction_warnings,
    })

def load_translations(
    cache: StageCache,
    transcript: list[TranscriptSegment],
    client: OpenAIIngestClient | None,
    logger: logging.Logger,
    translate_mode: str = "ko-en",
) -> tuple[list[TranslatedSegment], int]:
    if cache.has("translated.json"):
        logger.info("[3/6] Using cached translated.json")
        return [TranslatedSegment.model_validate(item) for item in cache.read_json("translated.json")], 0
    if translate_mode == "none":
        logger.info("[3/6] Keeping source transcript text without translation")
        translated = [
            TranslatedSegment(id=segment.id, tc_start=segment.tc_start, tc_end=segment.tc_end, ko=segment.ko, en=segment.ko)
            for segment in transcript
        ]
        cache.write_json("translated.json", translated)
        return translated, 0
    if client is None:
        raise IngestError("OPENAI_API_KEY is required for translation")
    logger.info("[3/6] Translating KO -> EN with stable segment ids")
    translated, warnings_count = client.translate_segments(transcript, logger=logger)
    cache.write_json("translated.json", translated)
    return translated, warnings_count


def load_video_profile(path: Path | None) -> VideoProfile | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise IngestError(f"video profile does not exist: {resolved}")
    return VideoProfile.model_validate_json(resolved.read_text(encoding="utf-8"))

def overlaps_non_story(start: float, end: float, profile: VideoProfile | None) -> bool:
    if profile is None:
        return False
    return any(start < item.end_s and end > item.start_s for item in profile.non_story_ranges)

def load_vision(
    *,
    cache: StageCache,
    input_path: Path,
    translated: list[TranslatedSegment],
    duration: float,
    gap_threshold: float,
    max_vision_frames: int,
    max_visual_gap_s: float,
    client: OpenAIIngestClient | None,
    logger: logging.Logger,
    drop_visual_before_s: float = 0.0,
    video_profile: VideoProfile | None = None,
) -> tuple[list[VisionSegment], int]:
    if cache.has("vision.json"):
        logger.info("[5/6] Using cached vision.json")
        return [VisionSegment.model_validate(item) for item in cache.read_json("vision.json")], 0
    logger.info("[4/6] Detecting silent gaps")
    gaps = split_long_gaps(detect_silent_gaps(translated, duration, gap_threshold), max_visual_gap_s)
    if video_profile is not None:
        gaps = [gap for gap in gaps if not overlaps_non_story(gap.tc_start, gap.tc_end, video_profile)]
    if drop_visual_before_s > 0:
        gaps = [gap for gap in gaps if gap.tc_end > drop_visual_before_s]
        gaps = [gap.model_copy(update={"id": index, "tc_start": max(gap.tc_start, drop_visual_before_s)}) for index, gap in enumerate(gaps) if max(gap.tc_start, drop_visual_before_s) < gap.tc_end]
    selected_gaps = select_gaps_for_vision(gaps, max_vision_frames)
    logger.info("[5/6] Running vision on %d/%d split gaps", len(selected_gaps), len(gaps))
    if not selected_gaps:
        cache.write_json("vision.json", [])
        return [], 0
    if client is None:
        raise IngestError("OPENAI_API_KEY is required for vision")
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
        "max_visual_gap_s": DEFAULT_MAX_VISUAL_GAP_S,
        "merge_gap_s": 0.0,
        "vad_filter": True,
        "openai_transcribe_model": "gpt-4o-mini-transcribe",
        "openai_chunk_s": 20.0,
        "alignment_device": "cuda",
        "transcript_correction": "off",
        "glossary": None,
        "correction_model": "gpt-4.1-mini",
        "drop_non_korean_intro_s": 30.0,
        "drop_visual_before_s": 0.0,
        "video_profile": None,
        "source_language": "ko",
        "translate_mode": "ko-en",
    }.items():
        if not hasattr(args, key):
            setattr(args, key, value)

    if not input_path.is_file():
        raise IngestError(f"Input video does not exist: {input_path}")
    if args.gap_threshold < 0:
        raise IngestError("--gap-threshold must be >= 0")
    if args.max_vision_frames < 0:
        raise IngestError("--max-vision-frames must be >= 0")
    if args.max_visual_gap_s < 0:
        raise IngestError("--max-visual-gap-s must be >= 0")
    if args.max_segment_s < 0:
        raise IngestError("--max-segment-s must be >= 0")
    if args.drop_non_korean_intro_s < 0:
        raise IngestError("--drop-non-korean-intro-s must be >= 0")
    if args.drop_visual_before_s < 0:
        raise IngestError("--drop-visual-before-s must be >= 0")
    if args.source_language == "vi" and args.translate_mode != "none":
        logger.warning("source_language=vi should use translate_mode=none; overriding translate_mode")
        args.translate_mode = "none"
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    needs_openai_asr = args.asr_provider in {"openai-gpt4o", "openai-gpt4o-hybrid"}
    needs_openai_translate = args.translate_mode != "none"
    needs_openai_vision = args.max_vision_frames > 0
    needs_openai_correction = args.transcript_correction == "openai"
    if (needs_openai_asr or needs_openai_translate or needs_openai_vision or needs_openai_correction) and not api_key:
        raise IngestError("OPENAI_API_KEY is required for configured ingest OpenAI usage")

    require_ffmpeg()
    cache = StageCache(work_dir, force=args.force)
    cache.prepare()
    client = (
        OpenAIIngestClient(api_key, args.translate_model, args.vision_model)
        if needs_openai_translate or needs_openai_vision
        else None
    )

    logger.info("[0/6] Probing input video")
    duration = probe_duration(input_path)
    input_hash = media_identity_hash(input_path)
    stage_audio_key = audio_cache_key(input_hash)
    audio_path = cache.path("audio.wav")
    if cache.stage_current("audio", stage_audio_key, ("audio.wav",)) and cache.has("audio.wav"):
        logger.info("[1/6] Using cached audio.wav")
    else:
        logger.info("[1/6] Extracting mono 16kHz audio")
        extract_audio(input_path, audio_path)
        cache.commit_stage("audio", stage_audio_key)

    warnings_count = 0
    stage_transcript_key = transcript_cache_key(stage_audio_key, args)
    transcript_cached = cache.stage_current(
        "transcript",
        stage_transcript_key,
        ("transcript_aligned.json", "transcript_quality.json"),
    )
    transcript, transcript_quality = load_transcript(cache, audio_path, duration, args, logger)
    if not transcript_cached:
        cache.commit_stage("transcript", stage_transcript_key)

    aligned_hash = file_hash(cache.path("transcript_aligned.json"))
    if aligned_hash is None:
        raise IngestError("transcript_aligned.json was not written")
    stage_correction_key = correction_cache_key(aligned_hash, args)
    correction_required = () if args.transcript_correction == "off" else ("transcript_corrected.json", "transcript_correction.meta.json")
    correction_cached = cache.stage_current("correction", stage_correction_key, correction_required)
    transcript, transcript_quality = correct_transcript(cache, transcript, transcript_quality, args, logger)
    if not correction_cached:
        cache.commit_stage("correction", stage_correction_key)

    final_transcript_hash = stable_hash([item.model_dump(mode="json") for item in transcript])
    stage_translation_key = translation_cache_key(final_transcript_hash, args)
    translation_cached = cache.stage_current("translation", stage_translation_key, ("translated.json",))
    translated, translation_warnings = load_translations(cache, transcript, client, logger, translate_mode=args.translate_mode)
    if not translation_cached:
        cache.commit_stage("translation", stage_translation_key)
    warnings_count += translation_warnings
    video_profile = load_video_profile(args.video_profile)
    video_profile_hash = file_hash(args.video_profile) if args.video_profile else None
    translated_hash = file_hash(cache.path("translated.json"))
    if translated_hash is None:
        raise IngestError("translated.json was not written")
    stage_vision_key = vision_cache_key(
        input_hash=input_hash,
        translated_hash=translated_hash,
        video_profile_hash=video_profile_hash,
        settings=args,
    )
    vision_cached = cache.stage_current("vision", stage_vision_key, ("vision.json",))
    vision_segments, vision_warnings = load_vision(
        cache=cache,
        input_path=input_path,
        translated=translated,
        duration=duration,
        gap_threshold=args.gap_threshold,
        max_vision_frames=args.max_vision_frames,
        max_visual_gap_s=args.max_visual_gap_s,
        client=client,
        logger=logger,
        drop_visual_before_s=args.drop_visual_before_s,
        video_profile=video_profile,
    )
    if not vision_cached:
        cache.commit_stage("vision", stage_vision_key)
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
        translate_model=args.translate_model if args.translate_mode != "none" else "none",
        vision_model=args.vision_model,
        gap_threshold=args.gap_threshold,
        max_vision_frames=args.max_vision_frames,
        max_visual_gap_s=args.max_visual_gap_s,
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
        source_language=args.source_language,
        translate_mode=args.translate_mode,
        input_hash=input_hash,
        config_hash=ingest_config_hash(args),
        video_profile_hash=video_profile_hash,
        cache_version=INGEST_CACHE_VERSION,
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
