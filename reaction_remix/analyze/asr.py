from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RawWord:
    start: float
    end: float
    text: str
    confidence: float


@dataclass(frozen=True)
class RawTurn:
    start: float
    end: float
    text: str
    language: str
    language_confidence: float
    asr_confidence: float
    words: list[RawWord] = field(default_factory=list)


class FasterWhisperTranscriber:
    def __init__(self, *, model: str, device: str, compute_type: str) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is required for reaction analysis") from exc
        self._model = WhisperModel(model, device=device, compute_type=compute_type)

    def transcribe(self, audio_path: Path) -> list[RawTurn]:
        segments, info = self._model.transcribe(
            str(audio_path),
            language=None,
            word_timestamps=True,
            vad_filter=False,
        )
        language = str(getattr(info, "language", None) or "und").lower()
        language_probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        output: list[RawTurn] = []
        for segment in segments:
            text = str(segment.text or "").strip()
            if not text or float(segment.end) <= float(segment.start):
                continue
            words = [
                RawWord(
                    start=float(word.start),
                    end=float(word.end),
                    text=str(word.word or "").strip(),
                    confidence=max(0.0, min(1.0, float(getattr(word, "probability", 0.0) or 0.0))),
                )
                for word in (segment.words or [])
                if word.start is not None and word.end is not None and float(word.end) > float(word.start)
            ]
            avg_logprob = float(getattr(segment, "avg_logprob", -10.0) or -10.0)
            output.append(
                RawTurn(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=text,
                    language=language,
                    language_confidence=language_probability,
                    asr_confidence=max(0.0, min(1.0, math.exp(avg_logprob))),
                    words=words,
                )
            )
        return output

