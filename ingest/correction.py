from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from common.schema import TranscriptSegment
from ingest.llm import retry_call

CorrectionMode = Literal["off", "glossary", "openai"]

@dataclass(frozen=True)
class Glossary:
    replacements: dict[str, str]
    names: list[str]
    context: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.replacements and not self.names and not self.context


def load_glossary(path: Path | None) -> Glossary:
    if path is None:
        return Glossary(replacements={}, names=[])
    if not path.is_file():
        raise FileNotFoundError(f"glossary file does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required to read YAML glossary files")
        data = yaml.safe_load(text) or {}
    else:
        data = parse_plain_glossary(text)
    if not isinstance(data, dict):
        raise ValueError("glossary root must be an object")
    replacements = parse_replacements(data.get("replacements") or data.get("corrections") or {})
    names = parse_names(data.get("names") or data.get("characters") or [])
    context = data.get("context")
    return Glossary(replacements=replacements, names=names, context=str(context).strip() if context else None)


def parse_plain_glossary(text: str) -> dict[str, Any]:
    replacements: dict[str, str] = {}
    names: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            wrong, correct = line.split("=>", 1)
            replacements[wrong.strip()] = correct.strip()
        elif "->" in line:
            wrong, correct = line.split("->", 1)
            replacements[wrong.strip()] = correct.strip()
        else:
            names.append(line)
    return {"replacements": replacements, "names": names}


def parse_replacements(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip() and str(v).strip()}
    if isinstance(value, list):
        parsed: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict):
                wrong = item.get("wrong") or item.get("from") or item.get("source")
                correct = item.get("correct") or item.get("to") or item.get("target")
                if wrong and correct:
                    parsed[str(wrong).strip()] = str(correct).strip()
        return parsed
    raise ValueError("glossary replacements must be an object or list")


def parse_names(value: Any) -> list[str]:
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name") or item.get("ko") or item.get("canonical")
                if name:
                    names.append(str(name).strip())
        return names
    if isinstance(value, dict):
        return [str(k).strip() for k in value if str(k).strip()]
    raise ValueError("glossary names must be a list or object")


def apply_glossary_replacements(segments: list[TranscriptSegment], glossary: Glossary) -> tuple[list[TranscriptSegment], list[str]]:
    if not glossary.replacements:
        return segments, []
    corrected: list[TranscriptSegment] = []
    changed = 0
    for segment in segments:
        text = segment.ko
        for wrong, correct in sorted(glossary.replacements.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(wrong, correct)
        if text != segment.ko:
            changed += 1
        corrected.append(segment.model_copy(update={"ko": text}))
    warnings = [f"glossary correction changed {changed} transcript segment(s)"] if changed else []
    return corrected, warnings


class OpenAITranscriptCorrector:
    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def correct_segments(
        self,
        segments: list[TranscriptSegment],
        glossary: Glossary,
        *,
        batch_size: int = 20,
    ) -> tuple[list[TranscriptSegment], list[str]]:
        corrected: list[TranscriptSegment] = []
        warnings: list[str] = []
        for offset in range(0, len(segments), batch_size):
            batch = segments[offset : offset + batch_size]
            try:
                mapping = retry_call(lambda: self._correct_batch(batch, glossary))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"OpenAI transcript correction batch failed at segment #{batch[0].id}: {exc}")
                corrected.extend(batch)
                continue
            for segment in batch:
                text = str(mapping.get(str(segment.id)) or mapping.get(segment.id) or segment.ko).strip()
                corrected.append(segment.model_copy(update={"ko": text or segment.ko}))
        return corrected, warnings

    def _correct_batch(self, batch: list[TranscriptSegment], glossary: Glossary) -> dict[str, str]:
        payload = [{"id": item.id, "ko": item.ko} for item in batch]
        glossary_payload = {
            "canonical_names": glossary.names,
            "required_replacements": glossary.replacements,
            "context": glossary.context,
        }
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You correct Korean movie transcript ASR text before translation. "
                        "Fix character names/entities using the glossary, obvious ASR homophones, spacing, and punctuation. "
                        "Preserve Korean meaning, do not summarize, do not translate, do not add new facts. "
                        "Return only a JSON object mapping each id to corrected Korean text. "
                        "Do not merge, split, omit, or renumber segments."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"glossary": glossary_payload, "segments": payload}, ensure_ascii=False),
                },
            ],
            temperature=0,
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(strip_json_fence(content))
        if not isinstance(parsed, dict):
            raise ValueError("correction response must be a JSON object")
        return {str(key): str(value) for key, value in parsed.items()}


def strip_json_fence(content: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else content
