from __future__ import annotations

from pathlib import Path

from common.schema import TranscriptSegment

MIN_SEGMENT_SECONDS = 0.2


def transcribe_korean(audio_path: Path, whisper_model: str, device: str) -> list[TranscriptSegment]:
    from faster_whisper import WhisperModel

    model = WhisperModel(whisper_model, device=device)
    raw_segments, _info = model.transcribe(str(audio_path), language="ko", word_timestamps=False)
    segments: list[TranscriptSegment] = []
    for raw in raw_segments:
        text = str(getattr(raw, "text", "") or "").strip()
        start = float(getattr(raw, "start", 0.0))
        end = float(getattr(raw, "end", 0.0))
        if not text or end - start < MIN_SEGMENT_SECONDS:
            continue
        segments.append(TranscriptSegment(id=len(segments), tc_start=start, tc_end=end, ko=text))
    return segments
