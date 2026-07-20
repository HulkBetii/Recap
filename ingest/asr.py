from __future__ import annotations

import json
import re
from pathlib import Path

from common.schema import TranscriptQuality, TranscriptSegment

TIMESTAMP_RE = re.compile(r"^\s*-?\s*\[(?P<stamp>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<text>.+?)\s*$")
SENTENCE_RE = re.compile(r"(?<=[.!?。！？]|[.?!]|[다요죠까니네])\s+")
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
HIRAGANA_KATAKANA_RE = re.compile(r"[\u3040-\u30ff]")


def parse_timestamp(value: str) -> float:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes * 60 + seconds)
    hours, minutes, seconds = parts
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_manual_transcript(path: Path, duration: float) -> tuple[list[TranscriptSegment], TranscriptQuality]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        segments = [TranscriptSegment.model_validate(item) for item in data]
    else:
        segments = parse_markdown_transcript(path.read_text(encoding="utf-8"), duration)
    warnings = ["manual transcript timestamps are inferred/approximate unless aligned"]
    return segments, TranscriptQuality(
        asr_provider="manual",
        aligner_provider="none",
        timecode_quality="approximate",
        approximate_timecodes=True,
        warnings=warnings,
    )


def parse_markdown_transcript(text: str, duration: float) -> list[TranscriptSegment]:
    starts: list[tuple[float, str]] = []
    for line in text.splitlines():
        match = TIMESTAMP_RE.match(line)
        if not match:
            continue
        starts.append((parse_timestamp(match.group("stamp")), match.group("text").strip()))
    starts = [(start, body) for start, body in starts if body]
    segments: list[TranscriptSegment] = []
    for index, (start, body) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else duration
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if end <= start:
            continue
        segments.append(TranscriptSegment(id=len(segments), tc_start=start, tc_end=end, ko=body))
    return segments


def split_long_segments(segments: list[TranscriptSegment], max_segment_s: float) -> list[TranscriptSegment]:
    if max_segment_s <= 0:
        return reassign_ids(segments)
    output: list[TranscriptSegment] = []
    for segment in segments:
        duration = segment.tc_end - segment.tc_start
        if duration <= max_segment_s:
            output.append(segment)
            continue
        pieces = split_text(segment.ko)
        if len(pieces) <= 1:
            output.extend(split_by_duration(segment, max_segment_s))
            continue
        weights = [max(1, len(piece)) for piece in pieces]
        total = sum(weights)
        cursor = segment.tc_start
        for index, piece in enumerate(pieces):
            if index == len(pieces) - 1:
                end = segment.tc_end
            else:
                end = cursor + duration * (weights[index] / total)
            if end > cursor:
                output.append(TranscriptSegment(id=0, tc_start=round(cursor, 3), tc_end=round(end, 3), ko=piece))
            cursor = end
    return reassign_ids(output)


def split_text(text: str) -> list[str]:
    normalized = " ".join(text.split())
    pieces = [piece.strip() for piece in SENTENCE_RE.split(normalized) if piece.strip()]
    return pieces or [normalized]


def split_by_duration(segment: TranscriptSegment, max_segment_s: float) -> list[TranscriptSegment]:
    duration = segment.tc_end - segment.tc_start
    count = max(1, int((duration + max_segment_s - 1e-6) // max_segment_s))
    step = duration / count
    return [
        TranscriptSegment(
            id=0,
            tc_start=round(segment.tc_start + index * step, 3),
            tc_end=round(segment.tc_end if index == count - 1 else segment.tc_start + (index + 1) * step, 3),
            ko=segment.ko,
        )
        for index in range(count)
    ]


def reassign_ids(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    ordered = sorted(segments, key=lambda item: (item.tc_start, item.tc_end))
    return [segment.model_copy(update={"id": index}) for index, segment in enumerate(ordered)]


def detect_transcript_warnings(segments: list[TranscriptSegment], *, source_language: str = "ko") -> list[str]:
    warnings: list[str] = []
    for segment in segments:
        if source_language == "ko" and is_non_korean_cjk_text(segment.ko):
            warnings.append(f"segment #{segment.id} has high non-Korean CJK/Japanese character count")
        words = segment.ko.split()
        if len(words) >= 6:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.35:
                warnings.append(f"segment #{segment.id} may contain repeated hallucinated text")
    return warnings

def is_non_korean_cjk_text(text: str) -> bool:
    hangul_count = len(HANGUL_RE.findall(text))
    chinese_count = len(CHINESE_RE.findall(text))
    japanese_count = len(HIRAGANA_KATAKANA_RE.findall(text))
    return chinese_count + japanese_count > max(8, hangul_count * 2)



def clean_aligned_segments(
    segments: list[TranscriptSegment],
    *,
    duration: float,
    min_segment_s: float = 0.45,
    max_segment_s: float = 30.0,
    drop_non_korean_intro_s: float = 0.0,
) -> tuple[list[TranscriptSegment], list[str]]:
    warnings: list[str] = []
    normalized: list[TranscriptSegment] = []
    previous_end = 0.0
    for segment in sorted(segments, key=lambda item: (item.tc_start, item.tc_end)):
        start = max(0.0, min(segment.tc_start, duration))
        end = max(0.0, min(segment.tc_end, duration))
        if start < previous_end:
            start = previous_end
        if end <= start:
            warnings.append(f"segment #{segment.id} dropped after clamp/overlap cleanup")
            continue
        if drop_non_korean_intro_s > 0 and start < drop_non_korean_intro_s and is_non_korean_cjk_text(segment.ko):
            warnings.append(f"segment #{segment.id} dropped as non-Korean intro/credit text near {start:.3f}s")
            previous_end = end
            continue
        normalized.append(TranscriptSegment(id=0, tc_start=round(start, 3), tc_end=round(end, 3), ko=segment.ko))
        previous_end = end

    merged: list[TranscriptSegment] = []
    for segment in normalized:
        segment_duration = segment.tc_end - segment.tc_start
        if segment_duration < min_segment_s and merged:
            previous = merged[-1]
            merged[-1] = TranscriptSegment(
                id=0,
                tc_start=previous.tc_start,
                tc_end=segment.tc_end,
                ko=(previous.ko.rstrip() + " " + segment.ko.lstrip()).strip(),
            )
            warnings.append(f"short segment merged near {segment.tc_start:.3f}s")
        else:
            merged.append(segment)
    if len(merged) >= 2 and merged[0].tc_end - merged[0].tc_start < min_segment_s:
        first = merged.pop(0)
        second = merged.pop(0)
        merged.insert(0, TranscriptSegment(id=0, tc_start=first.tc_start, tc_end=second.tc_end, ko=(first.ko + " " + second.ko).strip()))
        warnings.append("first short segment merged forward")

    split_segments = split_long_segments(merged, max_segment_s)
    for segment in split_segments:
        if segment.tc_end - segment.tc_start > max_segment_s + 1e-3:
            warnings.append(f"segment #{segment.id} remains longer than max_segment_s")
    warnings.extend(detect_qc_warnings(split_segments, duration=duration, min_segment_s=min_segment_s, max_segment_s=max_segment_s))
    return reassign_ids(split_segments), warnings


def detect_qc_warnings(
    segments: list[TranscriptSegment],
    *,
    duration: float,
    min_segment_s: float,
    max_segment_s: float,
) -> list[str]:
    warnings: list[str] = []
    if not segments:
        return ["transcript has no segments after QC"]
    last_end = max(segment.tc_end for segment in segments)
    if duration - last_end > 45:
        warnings.append(f"last transcript segment ends {duration - last_end:.1f}s before media end")
    for segment in segments:
        segment_duration = segment.tc_end - segment.tc_start
        if segment_duration < min_segment_s:
            warnings.append(f"segment #{segment.id} is very short ({segment_duration:.3f}s)")
        if segment_duration > max_segment_s:
            warnings.append(f"segment #{segment.id} is very long ({segment_duration:.3f}s)")
        if looks_like_subtitle_artifact(segment.ko):
            warnings.append(f"segment #{segment.id} may be subtitle/credit artifact")
    return warnings


def looks_like_subtitle_artifact(text: str) -> bool:
    lowered = text.lower()
    patterns = ("??", "????", "2?", "??", "subtitle", "subtitles", " by ")
    return any(pattern in lowered for pattern in patterns)

def apply_alignment(
    segments: list[TranscriptSegment],
    quality: TranscriptQuality,
    aligner: str,
    requested_quality: str,
    audio_path: Path | None = None,
    alignment_device: str = "cuda",
    source_language: str = "ko",
) -> tuple[list[TranscriptSegment], TranscriptQuality]:
    warnings = list(quality.warnings)
    if aligner == "none":
        approximate = quality.approximate_timecodes or requested_quality == "approximate"
        return segments, quality.model_copy(update={
            "aligner_provider": "none",
            "timecode_quality": "approximate" if approximate else "strict",
            "approximate_timecodes": approximate,
            "warnings": warnings,
        })
    if aligner == "whisperx" and audio_path is not None:
        try:
            aligned = align_with_whisperx(segments, audio_path, alignment_device, source_language=source_language)
            return aligned, quality.model_copy(update={
                "aligner_provider": "whisperx",
                "timecode_quality": "strict",
                "approximate_timecodes": False,
                "warnings": warnings,
            })
        except Exception as exc:  # noqa: BLE001 - alignment must safely fallback
            warnings.append(f"whisperx alignment failed; using approximate timestamps: {exc}")
            return segments, quality.model_copy(update={
                "aligner_provider": "whisperx",
                "timecode_quality": "approximate",
                "approximate_timecodes": True,
                "warnings": warnings,
            })
    warnings.append(f"aligner '{aligner}' is configured but not available; using existing timestamps")
    return segments, quality.model_copy(update={
        "aligner_provider": aligner,
        "timecode_quality": "approximate",
        "approximate_timecodes": True,
        "warnings": warnings,
    })


def whisperx_language_code(source_language: str) -> str:
    language_map = {"ko": "ko", "vi": "vi", "ja": "ja"}
    return language_map.get(source_language, source_language)


def align_with_whisperx(
    segments: list[TranscriptSegment],
    audio_path: Path,
    device: str,
    source_language: str = "ko",
) -> list[TranscriptSegment]:
    import whisperx

    whisperx_segments = [
        {"start": segment.tc_start, "end": segment.tc_end, "text": segment.ko}
        for segment in segments
    ]
    audio = whisperx.load_audio(str(audio_path))
    model, metadata = whisperx.load_align_model(language_code=whisperx_language_code(source_language), device=device)
    result = whisperx.align(whisperx_segments, model, metadata, audio, device, return_char_alignments=False)
    aligned: list[TranscriptSegment] = []
    for item in result.get("segments", []):
        text = str(item.get("text") or "").strip()
        start = item.get("start")
        end = item.get("end")
        if not text or start is None or end is None:
            continue
        start_f = float(start)
        end_f = float(end)
        if end_f <= start_f:
            continue
        aligned.append(TranscriptSegment(id=len(aligned), tc_start=round(start_f, 3), tc_end=round(end_f, 3), ko=text))
    if not aligned:
        raise RuntimeError("WhisperX produced no aligned segments")
    return reassign_ids(aligned)
