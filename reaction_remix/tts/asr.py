from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

PUNCTUATION_RE = re.compile(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+")


def normalize_asr_text(text: str) -> str:
    return PUNCTUATION_RE.sub("", unicodedata.normalize("NFKC", text)).lower()


def text_similarity(expected: str, actual: str) -> float:
    left = normalize_asr_text(expected)
    right = normalize_asr_text(actual)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


class JapaneseAsrVerifier:
    def __init__(self, *, model_name: str = "large-v3", device: str = "auto") -> None:
        self.model_name = model_name
        self.device = device
        self._model = None

    def transcribe(self, path: Path) -> str:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device=self.device)
        segments, _info = self._model.transcribe(str(path), language="ja", vad_filter=True)
        return "".join(segment.text for segment in segments).strip()

    def similarity(self, path: Path, expected: str) -> float:
        return text_similarity(expected, self.transcribe(path))
