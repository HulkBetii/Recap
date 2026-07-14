from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from common.integrity import atomic_write_json, stable_hash
from common.schema import (
    ReactionAnalysisRegion,
    ReactionAsrInfo,
    ReactionSource,
    ReactionTranscript,
    ReactionTurn,
    ReactionWord,
    validate_reaction_transcript,
)
from reaction_remix.analyze.asr import FasterWhisperTranscriber, RawTurn, RawWord as RawAsrWord
from reaction_remix.analyze.language import LinguaLanguageVerifier
from reaction_remix.analyze.regions import (
    TimeSpan,
    detect_silences,
    split_analysis_regions,
    split_at_silence_midpoints,
)
from reaction_remix.analyze.speakers import (
    SpeechBrainSpeakerClusterer,
    build_speaker_clusters,
    select_narrator_speaker,
)


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[RawTurn]:
        ...


class LanguageVerifier(Protocol):
    def detect(self, text: str) -> tuple[str, float]:
        ...


class SpeakerClusterer(Protocol):
    def cluster(self, audio_path: Path, turns: list[ReactionTurn]) -> dict[int, tuple[str, float]]:
        ...


class ReactionAnalyzeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalyzeSettings:
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "default"
    max_region_s: float = 30.0
    multilingual_window_s: float = 6.0
    overlap_s: float = 2.0
    max_attempts: int = 2
    silence_noise_db: float = -35.0
    min_silence_s: float = 0.35
    language_min_confidence: float = 0.70
    speaker_threshold: float = 0.28

    def validate(self) -> None:
        if self.model != "large-v3":
            raise ReactionAnalyzeError("reaction-remix.v1 locks Faster Whisper model large-v3")
        if self.max_region_s <= 0 or not 0 < self.multilingual_window_s <= self.max_region_s:
            raise ReactionAnalyzeError("analysis region settings are invalid")
        if not 0 <= self.overlap_s < self.multilingual_window_s:
            raise ReactionAnalyzeError("overlap_s must be smaller than multilingual_window_s")
        if self.max_attempts < 1:
            raise ReactionAnalyzeError("max_attempts must be at least one")
        if not 0 <= self.language_min_confidence <= 1:
            raise ReactionAnalyzeError("language_min_confidence must be between zero and one")
        if not 0 < self.speaker_threshold < 1:
            raise ReactionAnalyzeError("speaker_threshold must be between zero and one")


def extract_region_audio(input_path: Path, output_path: Path, span: TimeSpan) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{span.tc_start:.6f}",
            "-i",
            str(input_path),
            "-t",
            f"{span.duration_s:.6f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ReactionAnalyzeError(result.stderr.strip() or "could not extract analysis region")


def _raw_turn_payload(turn: RawTurn) -> dict[str, object]:
    return asdict(turn)


def _raw_turn_from_payload(payload: dict[str, object]) -> RawTurn:
    from reaction_remix.analyze.asr import RawWord

    return RawTurn(
        start=float(payload["start"]),
        end=float(payload["end"]),
        text=str(payload["text"]),
        language=str(payload["language"]),
        language_confidence=float(payload["language_confidence"]),
        asr_confidence=float(payload["asr_confidence"]),
        words=[RawWord(**word) for word in payload.get("words", [])],  # type: ignore[arg-type]
    )


def _deduplicate_turns(turns: list[tuple[str, float, RawTurn]]) -> list[tuple[str, float, RawTurn]]:
    ordered = sorted(turns, key=lambda item: (item[2].start + item[1], item[2].end + item[1]))
    output: list[tuple[str, float, RawTurn]] = []
    for region_id, offset, turn in ordered:
        absolute_start = turn.start + offset
        absolute_end = turn.end + offset
        if absolute_end <= absolute_start:
            continue
        if output:
            previous_region, previous_offset, previous = output[-1]
            previous_start = previous.start + previous_offset
            previous_end = previous.end + previous_offset
            same_text = " ".join(previous.text.lower().split()) == " ".join(turn.text.lower().split())
            if absolute_start < previous_end - 1e-6 and (same_text or absolute_end <= previous_end + 0.2):
                if turn.asr_confidence > previous.asr_confidence and same_text:
                    output[-1] = (region_id, offset, turn)
                continue
            if absolute_start < previous_end:
                clipped_start = previous_end - offset
                clipped_words = [word for word in turn.words if word.end > clipped_start]
                turn = RawTurn(
                    start=clipped_start,
                    end=turn.end,
                    text=turn.text,
                    language=turn.language,
                    language_confidence=turn.language_confidence,
                    asr_confidence=turn.asr_confidence,
                    words=clipped_words,
                )
        output.append((region_id, offset, turn))
    return output


def _uncovered_spans(
    turns: list[tuple[str, float, RawTurn]],
    *,
    duration_s: float,
    min_gap_s: float = 1.0,
) -> list[TimeSpan]:
    spans: list[TimeSpan] = []
    cursor = 0.0
    for _region_id, offset, turn in turns:
        start = turn.start + offset
        end = turn.end + offset
        if start - cursor >= min_gap_s:
            spans.append(TimeSpan(cursor, start))
        cursor = max(cursor, end)
    if duration_s - cursor >= min_gap_s:
        spans.append(TimeSpan(cursor, duration_s))
    return spans


def _prefer_refined_turns(
    primary: list[tuple[str, float, RawTurn]],
    refined: list[tuple[str, float, RawTurn]],
) -> list[tuple[str, float, RawTurn]]:
    """Use short-window multilingual ASR where available, with primary regions as gap fallback."""
    preferred = _deduplicate_turns(refined)
    fallback: list[tuple[str, float, RawTurn]] = []
    refined_spans = sorted(
        (turn.start + offset, turn.end + offset)
        for _region_id, offset, turn in preferred
    )
    merged_refined_spans: list[tuple[float, float]] = []
    for start, end in refined_spans:
        if merged_refined_spans and start <= merged_refined_spans[-1][1] + 1e-6:
            previous_start, previous_end = merged_refined_spans[-1]
            merged_refined_spans[-1] = (previous_start, max(previous_end, end))
        else:
            merged_refined_spans.append((start, end))
    for item in _deduplicate_turns(primary):
        region_id, offset, turn = item
        start = turn.start + offset
        end = turn.end + offset
        uncovered = [(start, end)]
        for refined_start, refined_end in merged_refined_spans:
            next_uncovered: list[tuple[float, float]] = []
            for fragment_start, fragment_end in uncovered:
                if refined_end <= fragment_start + 1e-6 or refined_start >= fragment_end - 1e-6:
                    next_uncovered.append((fragment_start, fragment_end))
                    continue
                if fragment_start < refined_start - 1e-6:
                    next_uncovered.append((fragment_start, min(fragment_end, refined_start)))
                if refined_end < fragment_end - 1e-6:
                    next_uncovered.append((max(fragment_start, refined_end), fragment_end))
            uncovered = next_uncovered
            if not uncovered:
                break
        for fragment_start, fragment_end in uncovered:
            local_start = fragment_start - offset
            local_end = fragment_end - offset
            clipped_words = [
                RawAsrWord(
                    start=max(local_start, word.start),
                    end=min(local_end, word.end),
                    text=word.text,
                    confidence=word.confidence,
                )
                for word in turn.words
                if min(local_end, word.end) > max(local_start, word.start) + 1e-6
            ]
            if turn.words and not clipped_words:
                continue
            if clipped_words:
                separator = "" if turn.language.lower().startswith(("ja", "zh", "ko")) else " "
                text = separator.join(word.text.strip() for word in clipped_words if word.text.strip())
            else:
                text = turn.text
            fallback.append(
                (
                    region_id,
                    offset,
                    RawTurn(
                        start=local_start,
                        end=local_end,
                        text=text or turn.text,
                        language=turn.language,
                        language_confidence=turn.language_confidence,
                        asr_confidence=turn.asr_confidence,
                        words=clipped_words,
                    ),
                )
            )
    return _deduplicate_turns([*preferred, *fallback])


def analyze_reaction(
    input_path: Path,
    source: ReactionSource,
    work_dir: Path,
    *,
    settings: AnalyzeSettings,
    transcriber: Transcriber | None = None,
    language_verifier: LanguageVerifier | None = None,
    speaker_clusterer: SpeakerClusterer | None = None,
    silence_spans: list[TimeSpan] | None = None,
    force: bool = False,
) -> tuple[ReactionTranscript, list[str]]:
    settings.validate()
    resolved = input_path.expanduser().resolve()
    if source.input_path != resolved.as_posix():
        raise ReactionAnalyzeError("reaction source path does not match analysis input")
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_path = work_dir / "audio.wav"
    if force or not audio_path.is_file():
        extract_region_audio(resolved, audio_path, TimeSpan(0.0, source.duration_s))
    if silence_spans is None:
        silence_spans = detect_silences(
            audio_path,
            noise_db=settings.silence_noise_db,
            min_silence_s=settings.min_silence_s,
            duration_s=source.duration_s,
        )
    # Silence provides safe boundaries; every sample remains covered by an ASR region.
    regions = split_at_silence_midpoints(
        source.duration_s,
        silence_spans,
        max_region_s=settings.max_region_s,
        overlap_s=settings.overlap_s,
    )
    transcriber = transcriber or FasterWhisperTranscriber(
        model=settings.model,
        device=settings.device,
        compute_type=settings.compute_type,
    )
    warnings: list[str] = []
    region_models: list[ReactionAnalysisRegion] = []
    raw_turns: list[tuple[str, float, RawTurn]] = []
    region_dir = work_dir / "regions"
    region_dir.mkdir(parents=True, exist_ok=True)
    region_config_hash = stable_hash(asdict(settings))
    for index, span in enumerate(regions, start=1):
        region_id = f"region-{index:04d}"
        cache_path = region_dir / f"{region_id}.json"
        cache_key = stable_hash(
            {
                "source_hash": source.input_hash,
                "config_hash": region_config_hash,
                "algorithm": "reaction-asr-regions-v3",
                "tc_start": span.tc_start,
                "tc_end": span.tc_end,
            }
        )
        cached: dict[str, object] | None = None
        if not force and cache_path.is_file():
            try:
                candidate = json.loads(cache_path.read_text(encoding="utf-8"))
                if candidate.get("cache_key") == cache_key:
                    cached = candidate
            except (OSError, json.JSONDecodeError):
                cached = None
        if cached is not None:
            status = str(cached["status"])
            error = str(cached["error"]) if cached.get("error") else None
            attempts = int(cached["attempts"])
            region_turns = [_raw_turn_from_payload(item) for item in cached.get("turns", [])]  # type: ignore[arg-type]
        else:
            region_audio = region_dir / f"{region_id}.wav"
            extract_region_audio(resolved, region_audio, span)
            region_turns = []
            error = None
            attempts = 0
            for attempts in range(1, settings.max_attempts + 1):
                try:
                    region_turns = transcriber.transcribe(region_audio)
                    error = None
                    break
                except (RuntimeError, OSError, ValueError) as exc:
                    error = str(exc)
            status = "ok" if region_turns or error is None else "analysis_gap"
            atomic_write_json(
                cache_path,
                {
                    "cache_key": cache_key,
                    "status": status,
                    "attempts": attempts,
                    "error": error if status == "analysis_gap" else None,
                    "turns": [_raw_turn_payload(item) for item in region_turns],
                },
            )
        if status == "analysis_gap":
            warning = f"{region_id} failed after {attempts} attempt(s): {error}"
            warnings.append(warning)
            region_models.append(
                ReactionAnalysisRegion(
                    region_id=region_id,
                    tc_start=span.tc_start,
                    tc_end=span.tc_end,
                    status="analysis_gap",
                    attempts=attempts,
                    error=error or "unknown analysis error",
                    warnings=[warning],
                )
            )
            continue
        region_models.append(
            ReactionAnalysisRegion(
                region_id=region_id,
                tc_start=span.tc_start,
                tc_end=span.tc_end,
                status="ok",
                attempts=attempts,
            )
        )
        raw_turns.extend((region_id, span.tc_start, item) for item in region_turns)

    refine_spans = split_analysis_regions(
        [TimeSpan(0.0, source.duration_s)],
        max_region_s=settings.multilingual_window_s,
        overlap_s=min(settings.overlap_s, settings.multilingual_window_s / 3),
    )
    refine_dir = work_dir / "refine"
    refine_dir.mkdir(parents=True, exist_ok=True)
    refined_raw_turns: list[tuple[str, float, RawTurn]] = []
    for index, span in enumerate(refine_spans, start=1):
        region_id = f"refine-{index:04d}"
        cache_path = refine_dir / f"{region_id}.json"
        cache_key = stable_hash(
            {
                "source_hash": source.input_hash,
                "config_hash": region_config_hash,
                "pass": "full-multilingual-v3",
                "tc_start": span.tc_start,
                "tc_end": span.tc_end,
            }
        )
        cached_refine: dict[str, object] | None = None
        if not force and cache_path.is_file():
            try:
                candidate = json.loads(cache_path.read_text(encoding="utf-8"))
                if candidate.get("cache_key") == cache_key:
                    cached_refine = candidate
            except (OSError, json.JSONDecodeError):
                cached_refine = None
        if cached_refine is not None:
            refine_turns = [_raw_turn_from_payload(item) for item in cached_refine.get("turns", [])]  # type: ignore[arg-type]
            attempts = int(cached_refine["attempts"])
        else:
            refine_audio = refine_dir / f"{region_id}.wav"
            extract_region_audio(resolved, refine_audio, span)
            refine_turns = []
            refine_error: str | None = None
            attempts = 0
            for attempts in range(1, settings.max_attempts + 1):
                try:
                    refine_turns = transcriber.transcribe(refine_audio)
                    refine_error = None
                    break
                except (RuntimeError, OSError, ValueError) as exc:
                    refine_error = str(exc)
            if refine_error is not None:
                warnings.append(f"{region_id} multilingual refinement failed: {refine_error}")
            refine_turns = [turn for turn in refine_turns if turn.asr_confidence >= 0.55]
            atomic_write_json(
                cache_path,
                {
                    "cache_key": cache_key,
                    "attempts": attempts,
                    "turns": [_raw_turn_payload(item) for item in refine_turns],
                },
            )
        if not refine_turns:
            continue
        region_models.append(
            ReactionAnalysisRegion(
                region_id=region_id,
                tc_start=span.tc_start,
                tc_end=span.tc_end,
                status="ok",
                attempts=attempts,
                warnings=["second-pass multilingual refinement"],
            )
        )
        refined_raw_turns.extend((region_id, span.tc_start, item) for item in refine_turns)

    deduplicated = _prefer_refined_turns(raw_turns, refined_raw_turns)

    language_verifier_error: str | None = None
    if language_verifier is None:
        try:
            language_verifier = LinguaLanguageVerifier()
        except RuntimeError as exc:
            language_verifier_error = str(exc)
            warnings.append(language_verifier_error)
    turns: list[ReactionTurn] = []
    for turn_id, (region_id, offset, raw) in enumerate(deduplicated):
        absolute_start = max(0.0, raw.start + offset)
        absolute_end = min(source.duration_s, raw.end + offset)
        if absolute_end <= absolute_start:
            continue
        language = raw.language if raw.language else "und"
        language_confidence = raw.language_confidence
        turn_warnings: list[str] = []
        if language_verifier is not None:
            try:
                detected_language, detected_confidence = language_verifier.detect(raw.text)
                if detected_confidence >= settings.language_min_confidence:
                    language = detected_language
                    language_confidence = detected_confidence
                else:
                    language = "und"
                    language_confidence = detected_confidence
                    turn_warnings.append("Lingua confidence below threshold")
            except (RuntimeError, ValueError) as exc:
                turn_warnings.append(f"Lingua verification failed: {exc}")
        elif language_verifier_error:
            turn_warnings.append(language_verifier_error)
        words = [
            ReactionWord(
                word_id=f"word-{turn_id:06d}-{index:03d}",
                tc_start=max(absolute_start, word.start + offset),
                tc_end=min(absolute_end, word.end + offset),
                text=word.text or raw.text,
                confidence=word.confidence,
            )
            for index, word in enumerate(raw.words)
            if min(absolute_end, word.end + offset) > max(absolute_start, word.start + offset)
        ]
        turns.append(
            ReactionTurn(
                turn_id=len(turns),
                tc_start=absolute_start,
                tc_end=absolute_end,
                text=raw.text,
                language=language,
                language_confidence=language_confidence,
                speaker_id="speaker-unknown",
                speaker_confidence=0.0,
                asr_confidence=raw.asr_confidence,
                region_id=region_id,
                words=words,
                warnings=turn_warnings,
            )
        )

    if speaker_clusterer is None:
        speaker_clusterer = SpeechBrainSpeakerClusterer(
            threshold=settings.speaker_threshold,
            device=settings.device,
            cache_dir=work_dir / "speechbrain-copy-v1",
        )
    try:
        assignments = speaker_clusterer.cluster(audio_path, turns)
    except (RuntimeError, OSError, ValueError) as exc:
        warnings.append(f"speaker clustering failed; classification will remain conservative: {exc}")
        assignments = {}
    turns = [
        turn.model_copy(
            update={
                "speaker_id": assignments.get(turn.turn_id, ("speaker-unknown", 0.0))[0],
                "speaker_confidence": assignments.get(turn.turn_id, ("speaker-unknown", 0.0))[1],
            }
        )
        for turn in turns
    ]
    clusters = build_speaker_clusters(turns) if turns else []
    narrator_speaker_id, clusters = select_narrator_speaker(
        turns,
        clusters,
        source_duration_s=source.duration_s,
    )
    if narrator_speaker_id is not None:
        cluster_confidence = next(
            item.confidence for item in clusters if item.speaker_id == narrator_speaker_id
        )
        turns = [
            turn.model_copy(
                update={
                    "speaker_confidence": max(
                        turn.speaker_confidence,
                        (turn.speaker_confidence + cluster_confidence) / 2,
                    )
                }
            )
            if turn.speaker_id == narrator_speaker_id
            else turn
            for turn in turns
        ]
    transcript = ReactionTranscript(
        source_hash=source.input_hash,
        source_duration_s=source.duration_s,
        regions=region_models,
        turns=turns,
        speaker_clusters=clusters,
        narrator_speaker_id=narrator_speaker_id,
        asr=ReactionAsrInfo(
            device=settings.device,
            chunk_s=settings.multilingual_window_s,
            overlap_s=settings.overlap_s,
        ),
        created_at=datetime.now(timezone.utc),
        warnings=warnings,
    )
    return validate_reaction_transcript(transcript), warnings
