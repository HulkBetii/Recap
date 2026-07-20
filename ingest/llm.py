from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from common.schema import TranslatedSegment, TranscriptSegment

TRANSLATION_UNAVAILABLE = "[translation unavailable]"
VISION_UNAVAILABLE = "[vision unavailable]"

T = TypeVar("T")


def retry_call(action: Callable[[], T], *, attempts: int = 3, base_delay: float = 1.0) -> T:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(base_delay * (2**attempt))
    assert last_error is not None
    raise last_error


class OpenAIIngestClient:
    def __init__(self, api_key: str, translate_model: str, vision_model: str) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.translate_model = translate_model
        self.vision_model = vision_model

    def translate_segments(
        self,
        segments: list[TranscriptSegment],
        *,
        batch_size: int = 20,
        logger: logging.Logger | None = None,
        source_language: str = "ko",
    ) -> tuple[list[TranslatedSegment], int]:
        translated: list[TranslatedSegment] = []
        warnings_count = 0
        for offset in range(0, len(segments), batch_size):
            batch = segments[offset : offset + batch_size]
            try:
                mapping = retry_call(lambda: self._translate_batch(batch, source_language=source_language))
            except Exception as exc:  # noqa: BLE001
                warnings_count += len(batch)
                if logger:
                    logger.warning("translation batch failed: %s", exc)
                mapping = {str(item.id): TRANSLATION_UNAVAILABLE for item in batch}
            for segment in batch:
                text = str(mapping.get(str(segment.id)) or mapping.get(segment.id) or "").strip()
                if not text:
                    warnings_count += 1
                    text = TRANSLATION_UNAVAILABLE
                translated.append(
                    TranslatedSegment(
                        id=segment.id,
                        tc_start=segment.tc_start,
                        tc_end=segment.tc_end,
                        ko=segment.ko,
                        en=text,
                    )
                )
        return translated, warnings_count

    def describe_frame(self, frame_path: Path) -> str:
        return retry_call(lambda: self._describe_frame(frame_path))

    def _translate_batch(self, batch: list[TranscriptSegment], *, source_language: str = "ko") -> dict[str, str]:
        payload = [{"id": item.id, "ko": item.ko} for item in batch]
        source_name = {"ko": "Korean", "ja": "Japanese", "vi": "Vietnamese"}.get(source_language, source_language)
        response = self.client.chat.completions.create(
            model=self.translate_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Translate {source_name} transcript segments to natural English. "
                        "Return only a JSON object mapping each id to translated text. "
                        "Do not merge, split, omit, or renumber segments."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
        )
        content = response.choices[0].message.content or "{}"
        parsed: Any = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("translation response must be a JSON object")
        return {str(key): str(value) for key, value in parsed.items()}

    def _describe_frame(self, frame_path: Path) -> str:
        encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
        response = self.client.chat.completions.create(
            model=self.vision_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Describe only what is visible in the frame. "
                        "Use 1-2 concise English sentences covering characters, action, setting, and mood. "
                        "Do not infer plot beyond the image."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this frame for a movie recap scene map."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                        },
                    ],
                },
            ],
            temperature=0,
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("vision response was empty")
        return content


