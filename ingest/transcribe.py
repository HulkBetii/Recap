from __future__ import annotations

import json
import subprocess
from pathlib import Path

from common.schema import TranscriptSegment

MIN_SEGMENT_SECONDS = 0.2
MIN_OPENAI_CHUNK_BYTES = 1024
LOCAL_ASR_CHUNK_SECONDS = 600.0


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
            str(chunk_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


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

    model = WhisperModel(whisper_model, device=device)
    if duration is None or chunks_dir is None or duration <= chunk_s:
        return _transcribe_with_model(model, audio_path, language=language, vad_filter=vad_filter)

    segments: list[TranscriptSegment] = []
    start = 0.0
    index = 0
    while start < duration - 1e-6:
        length = min(chunk_s, duration - start)
        if length < MIN_SEGMENT_SECONDS:
            break
        chunk_path = chunks_dir / f"chunk-{index:04d}.wav"
        if not chunk_path.exists():
            _extract_audio_chunk(audio_path, chunk_path, start, length)
        segments.extend(
            _transcribe_with_model(
                model,
                chunk_path,
                language=language,
                vad_filter=vad_filter,
                offset_s=start,
                start_id=len(segments),
            )
        )
        start += chunk_s
        index += 1
    return [segment.model_copy(update={"id": index}) for index, segment in enumerate(segments)]


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
