from __future__ import annotations

import json
import subprocess
from pathlib import Path

from common.schema import TranscriptSegment

MIN_SEGMENT_SECONDS = 0.2
MIN_OPENAI_CHUNK_BYTES = 1024


def transcribe_korean(audio_path: Path, whisper_model: str, device: str, vad_filter: bool = True) -> list[TranscriptSegment]:
    from faster_whisper import WhisperModel

    model = WhisperModel(whisper_model, device=device)
    raw_segments, _info = model.transcribe(
        str(audio_path),
        language="ko",
        word_timestamps=False,
        vad_filter=vad_filter,
        condition_on_previous_text=False,
    )
    segments: list[TranscriptSegment] = []
    for raw in raw_segments:
        text = str(getattr(raw, "text", "") or "").strip()
        start = float(getattr(raw, "start", 0.0))
        end = float(getattr(raw, "end", 0.0))
        if not text or end - start < MIN_SEGMENT_SECONDS:
            continue
        segments.append(TranscriptSegment(id=len(segments), tc_start=start, tc_end=end, ko=text))
    return segments


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
