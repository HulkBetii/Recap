from __future__ import annotations

import json
import os
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from common.schema import TranscriptSegment

MIN_SEGMENT_SECONDS = 0.2
MIN_OPENAI_CHUNK_BYTES = 1024
LOCAL_ASR_CHUNK_SECONDS = 600.0
LOCAL_ASR_CHUNK_OVERLAP_SECONDS = 2.0


def _transcribe_with_model(
    model: object,
    audio_path: Path,
    *,
    language: str,
    vad_filter: bool,
    offset_s: float = 0.0,
    start_id: int = 0,
) -> list[TranscriptSegment]:
    raw_segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        word_timestamps=False,
        vad_filter=vad_filter,
        condition_on_previous_text=False,
    )
    segments: list[TranscriptSegment] = []
    for raw in raw_segments:
        text = str(getattr(raw, "text", "") or "").strip()
        start = offset_s + float(getattr(raw, "start", 0.0))
        end = offset_s + float(getattr(raw, "end", 0.0))
        if not text or end - start < MIN_SEGMENT_SECONDS:
            continue
        segments.append(TranscriptSegment(id=start_id + len(segments), tc_start=start, tc_end=end, ko=text))
    return segments


def _extract_audio_chunk(audio_path: Path, chunk_path: Path, start_s: float, length_s: float) -> None:
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = chunk_path.with_name(f"{chunk_path.stem}.tmp{chunk_path.suffix}")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_s:.3f}",
            "-i",
            str(audio_path),
            "-t",
            f"{length_s:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(temp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    if not temp_path.is_file() or temp_path.stat().st_size <= 44:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"invalid extracted audio chunk: {chunk_path}")
    os.replace(temp_path, chunk_path)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def _source_identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _chunk_cache_payload_is_valid(
    payload: dict,
    *,
    source_identity: dict[str, int],
    start_s: float,
    length_s: float,
    whisper_model: str,
    device: str,
    language: str,
    vad_filter: bool,
) -> bool:
    expected = {
        "source": source_identity,
        "start_s": round(start_s, 3),
        "length_s": round(length_s, 3),
        "whisper_model": whisper_model,
        "device": device,
        "language": language,
        "vad_filter": vad_filter,
    }
    return all(payload.get(key) == value for key, value in expected.items()) and isinstance(payload.get("segments"), list)


def _normalize_transcript_text(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold(), flags=re.UNICODE).strip()


def _dedupe_overlapping_segments(segments: list[TranscriptSegment], *, overlap_s: float) -> list[TranscriptSegment]:
    output: list[TranscriptSegment] = []
    for segment in sorted(segments, key=lambda item: (item.tc_start, item.tc_end)):
        duplicate_index = None
        normalized = _normalize_transcript_text(segment.ko)
        for index in range(len(output) - 1, -1, -1):
            previous = output[index]
            if segment.tc_start - previous.tc_end > overlap_s + 1.0:
                break
            previous_normalized = _normalize_transcript_text(previous.ko)
            similarity = SequenceMatcher(None, normalized, previous_normalized).ratio()
            intervals_overlap = min(segment.tc_end, previous.tc_end) > max(segment.tc_start, previous.tc_start)
            near_same_start = abs(segment.tc_start - previous.tc_start) <= overlap_s + 0.5
            if similarity >= 0.85 and (intervals_overlap or near_same_start):
                duplicate_index = index
                break
        if duplicate_index is None:
            output.append(segment)
            continue
        previous = output[duplicate_index]
        if len(segment.ko.strip()) > len(previous.ko.strip()):
            output[duplicate_index] = segment
    return [segment.model_copy(update={"id": index}) for index, segment in enumerate(output)]


def transcribe_korean(
    audio_path: Path,
    whisper_model: str,
    device: str,
    vad_filter: bool = True,
    *,
    language: str = "ko",
    duration: float | None = None,
    chunks_dir: Path | None = None,
    chunk_s: float = LOCAL_ASR_CHUNK_SECONDS,
) -> list[TranscriptSegment]:
    from faster_whisper import WhisperModel

    if duration is None or chunks_dir is None or duration <= chunk_s:
        model = WhisperModel(whisper_model, device=device)
        return _transcribe_with_model(model, audio_path, language=language, vad_filter=vad_filter)

    segments: list[TranscriptSegment] = []
    model = None
    chunks_dir.mkdir(parents=True, exist_ok=True)
    source_identity = _source_identity(audio_path)
    overlap_s = min(LOCAL_ASR_CHUNK_OVERLAP_SECONDS, max(0.0, chunk_s * 0.1))
    step_s = chunk_s - overlap_s
    if step_s <= 0:
        raise ValueError("local ASR chunk overlap must be smaller than chunk size")
    start = 0.0
    index = 0
    while start < duration - 1e-6:
        length = min(chunk_s, duration - start)
        if length < MIN_SEGMENT_SECONDS:
            break
        chunk_path = chunks_dir / f"chunk-{index:04d}-{round(start * 1000):010d}.wav"
        transcript_path = chunk_path.with_suffix(".json")
        cached_segments: list[TranscriptSegment] | None = None
        if transcript_path.is_file():
            try:
                payload = json.loads(transcript_path.read_text(encoding="utf-8"))
                if _chunk_cache_payload_is_valid(
                    payload,
                    source_identity=source_identity,
                    start_s=start,
                    length_s=length,
                    whisper_model=whisper_model,
                    device=device,
                    language=language,
                    vad_filter=vad_filter,
                ):
                    cached_segments = [TranscriptSegment.model_validate(item) for item in payload["segments"]]
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                cached_segments = None
        if cached_segments is None:
            _extract_audio_chunk(audio_path, chunk_path, start, length)
            model = model or WhisperModel(whisper_model, device=device)
            cached_segments = _transcribe_with_model(
                model,
                chunk_path,
                language=language,
                vad_filter=vad_filter,
                offset_s=start,
            )
            _write_json_atomic(
                transcript_path,
                {
                    "source": source_identity,
                    "start_s": round(start, 3),
                    "length_s": round(length, 3),
                    "whisper_model": whisper_model,
                    "device": device,
                    "language": language,
                    "vad_filter": vad_filter,
                    "segments": [segment.model_dump(mode="json") for segment in cached_segments],
                },
            )
        segments.extend(cached_segments)
        start += step_s
        index += 1
    bounded = [
        segment.model_copy(update={"tc_start": max(0.0, segment.tc_start), "tc_end": min(duration, segment.tc_end)})
        for segment in segments
        if segment.tc_start < duration and min(duration, segment.tc_end) - max(0.0, segment.tc_start) >= MIN_SEGMENT_SECONDS
    ]
    return _dedupe_overlapping_segments(bounded, overlap_s=overlap_s)


def transcribe_openai_gpt4o(audio_path: Path, duration: float, model: str = "gpt-4o-mini-transcribe", language: str = "ko") -> tuple[list[TranscriptSegment], bool]:
    from openai import OpenAI

    client = OpenAI()
    with audio_path.open("rb") as audio_file:
        response = client.audio.transcriptions.create(
            model=model,
            file=audio_file,
            language=language,
            response_format="json",
        )
    raw_segments = getattr(response, "segments", None) or []
    segments: list[TranscriptSegment] = []
    for raw in raw_segments:
        if isinstance(raw, dict):
            text = str(raw.get("text") or "").strip()
            start = float(raw.get("start") or 0.0)
            end = float(raw.get("end") or 0.0)
        else:
            text = str(getattr(raw, "text", "") or "").strip()
            start = float(getattr(raw, "start", 0.0) or 0.0)
            end = float(getattr(raw, "end", 0.0) or 0.0)
        if text and end - start >= MIN_SEGMENT_SECONDS:
            segments.append(TranscriptSegment(id=len(segments), tc_start=start, tc_end=end, ko=text))
    if segments:
        return segments, False
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        return [], True
    return [TranscriptSegment(id=0, tc_start=0.0, tc_end=max(duration, MIN_SEGMENT_SECONDS), ko=text)], True


def transcribe_openai_chunked(
    audio_path: Path,
    duration: float,
    chunks_dir: Path,
    model: str = "gpt-4o-mini-transcribe",
    chunk_s: float = 20.0,
    language: str = "ko",
) -> list[TranscriptSegment]:
    from openai import OpenAI

    if chunk_s <= 0:
        raise ValueError("chunk_s must be > 0")
    chunks_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI()
    segments: list[TranscriptSegment] = []
    start = 0.0
    index = 0
    while start < duration - 1e-6:
        length = min(chunk_s, duration - start)
        chunk_path = chunks_dir / f"chunk-{index:03d}.mp3"
        text_path = chunks_dir / f"chunk-{index:03d}.json"
        if not chunk_path.exists():
            subprocess.run([
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(audio_path),
                "-t",
                f"{length:.3f}",
                "-vn",
                "-acodec",
                "libmp3lame",
                "-q:a",
                "3",
                str(chunk_path),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if length < MIN_SEGMENT_SECONDS or chunk_path.stat().st_size < MIN_OPENAI_CHUNK_BYTES:
            start += chunk_s
            index += 1
            continue
        if text_path.exists():
            text = str(json.loads(text_path.read_text(encoding="utf-8")).get("text") or "").strip()
        else:
            with chunk_path.open("rb") as audio_file:
                try:
                    response = client.audio.transcriptions.create(
                        model=model,
                        file=audio_file,
                        language=language,
                        response_format="json",
                    )
                except Exception as exc:
                    message = str(exc).lower()
                    if "corrupted or unsupported" in message or "invalid_value" in message:
                        start += chunk_s
                        index += 1
                        continue
                    raise
            text = str(getattr(response, "text", "") or "").strip()
            text_path.write_text(
                json.dumps({"start": start, "length": length, "text": text}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if text:
            segments.append(TranscriptSegment(id=len(segments), tc_start=round(start, 3), tc_end=round(start + length, 3), ko=text))
        start += chunk_s
        index += 1
    return segments
