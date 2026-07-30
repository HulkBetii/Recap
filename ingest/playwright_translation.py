from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Callable

from common.integrity import atomic_write_json, stable_hash
from common.playwright_chat import PlaywrightChatClient, PlaywrightChatError
from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from common.schema import TranslatedSegment, TranscriptSegment

TRANSLATION_PROVIDER = "chatgpt_playwright"
TRANSLATION_PROMPT_VERSION = "ja-en-segment-v1"
TRANSLATION_PARSER_VERSION = "exact-id-english-v1"


class PlaywrightTranslationError(RuntimeError):
    pass


def resolve_profile_dir(configured_path: Path, *, require_exists: bool = True) -> Path:
    configured = configured_path.expanduser().resolve()
    canonical = CHATGPT_PLAYWRIGHT_PROFILE_DIR.expanduser().resolve()
    if configured != canonical:
        raise PlaywrightTranslationError(
            f"ingest ChatGPT profile is locked to {canonical}; configured path was {configured}"
        )
    if require_exists and not canonical.is_dir():
        raise PlaywrightTranslationError(
            f"ingest ChatGPT profile directory does not exist: {canonical}"
        )
    return canonical


def build_translation_prompt(
    batch: list[TranscriptSegment],
    *,
    source_language: str,
) -> str:
    source_name = {"ja": "Japanese", "ko": "Korean"}.get(source_language, source_language)
    expected_ids = [str(segment.id) for segment in batch]
    payload = [{"id": str(segment.id), "text": segment.ko} for segment in batch]
    return (
        f"TASK_VERSION: {TRANSLATION_PROMPT_VERSION}\n"
        f"Translate these {source_name} anime transcript segments into natural, concise English.\n"
        "Preserve names and meaning. Do not summarize, merge, split, omit, or renumber segments.\n"
        "Return only one JSON object whose keys are exactly EXPECTED_IDS and whose values are non-empty English strings.\n"
        f"EXPECTED_IDS: {json.dumps(expected_ids, ensure_ascii=False)}\n"
        f"SEGMENTS: {json.dumps(payload, ensure_ascii=False)}"
    )


def _extract_json_object(response: str) -> dict[str, object]:
    cleaned = response.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise PlaywrightTranslationError("ChatGPT translation response did not contain a JSON object") from None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise PlaywrightTranslationError(f"ChatGPT translation response contained invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PlaywrightTranslationError("ChatGPT translation response must be a JSON object")
    return {str(key): value for key, value in parsed.items()}


def validate_translation_response(
    response: str,
    batch: list[TranscriptSegment],
    *,
    source_language: str = "ja",
) -> dict[str, str]:
    parsed = _extract_json_object(response)
    expected_ids = {str(segment.id) for segment in batch}
    actual_ids = set(parsed)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise PlaywrightTranslationError(
            f"ChatGPT translation ID coverage mismatch: missing={missing}, extra={extra}"
        )
    translated: dict[str, str] = {}
    source_by_id = {str(segment.id): segment.ko.strip() for segment in batch}
    for segment_id in sorted(expected_ids, key=int):
        value = parsed[segment_id]
        if not isinstance(value, str) or not value.strip():
            raise PlaywrightTranslationError(
                f"ChatGPT translation for segment {segment_id} must be a non-empty string"
            )
        text = value.strip()
        if source_language == "ja" and _looks_like_japanese_echo(text, source_by_id[segment_id]):
            raise PlaywrightTranslationError(
                f"ChatGPT translation for segment {segment_id} appears to echo Japanese source text"
            )
        translated[segment_id] = text
    return translated


def _looks_like_japanese_echo(translated: str, source: str) -> bool:
    source_has_japanese = any(
        "\u3040" <= character <= "\u30ff" or "\u3400" <= character <= "\u9fff"
        for character in source
    )
    if translated == source and source_has_japanese:
        return True
    significant = [character for character in translated if character.isalnum()]
    if not significant:
        return False
    japanese = sum(
        1
        for character in significant
        if "\u3040" <= character <= "\u30ff" or "\u3400" <= character <= "\u9fff"
    )
    return japanese / len(significant) >= 0.5


def batch_cache_key(
    batch: list[TranscriptSegment],
    *,
    source_language: str,
    model: str,
) -> str:
    return stable_hash(
        {
            "provider": TRANSLATION_PROVIDER,
            "model": model,
            "prompt_version": TRANSLATION_PROMPT_VERSION,
            "parser_version": TRANSLATION_PARSER_VERSION,
            "source_language": source_language,
            "segments": [segment.model_dump(mode="json") for segment in batch],
        }
    )


class PlaywrightTranslationClient:
    def __init__(
        self,
        *,
        profile_dir: Path,
        cache_dir: Path,
        model: str = "chatgpt-web",
        batch_size: int = 80,
        batch_max_attempts: int = 2,
        reply_timeout_s: int = 600,
        playwright_max_attempts: int = 2,
        playwright_recovery_timeout_s: int = 60,
        headless: bool = False,
        session_file: Path | None = None,
        force: bool = False,
        chat_client_factory: Callable[[], PlaywrightChatClient] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("translation batch_size must be at least 1")
        if batch_max_attempts < 1:
            raise ValueError("translation batch_max_attempts must be at least 1")
        self.profile_dir = resolve_profile_dir(
            profile_dir,
            require_exists=chat_client_factory is None,
        )
        self.cache_dir = cache_dir
        self.model = model
        self.batch_size = batch_size
        self.batch_max_attempts = batch_max_attempts
        self.force = force
        self.logger = logging.getLogger("ingest.playwright_translation")
        self._chat_client_factory = chat_client_factory or (
            lambda: PlaywrightChatClient(
                self.profile_dir,
                headless=headless,
                timeout_s=reply_timeout_s,
                max_attempts=playwright_max_attempts,
                recovery_timeout_s=playwright_recovery_timeout_s,
                session_file=session_file,
            )
        )

    def translate_segments(
        self,
        segments: list[TranscriptSegment],
        *,
        batch_size: int | None = None,
        logger: logging.Logger | None = None,
        source_language: str = "ja",
    ) -> tuple[list[TranslatedSegment], int]:
        active_logger = logger or self.logger
        effective_batch_size = batch_size or self.batch_size
        if effective_batch_size < 1:
            raise ValueError("translation batch_size must be at least 1")
        try:
            mappings = asyncio.run(
                self._translate_batches(
                    segments,
                    batch_size=effective_batch_size,
                    source_language=source_language,
                    logger=active_logger,
                )
            )
        except PlaywrightChatError as exc:
            raise PlaywrightTranslationError(
                f"ChatGPT Playwright translation failed ({exc.code}, attempts={exc.attempts}): {exc}"
            ) from exc
        translated = [
            TranslatedSegment(
                id=segment.id,
                tc_start=segment.tc_start,
                tc_end=segment.tc_end,
                ko=segment.ko,
                en=mappings[str(segment.id)],
            )
            for segment in segments
        ]
        return translated, 0

    async def _translate_batches(
        self,
        segments: list[TranscriptSegment],
        *,
        batch_size: int,
        source_language: str,
        logger: logging.Logger,
    ) -> dict[str, str]:
        batches = [segments[offset : offset + batch_size] for offset in range(0, len(segments), batch_size)]
        mappings: dict[str, str] = {}
        pending: list[tuple[list[TranscriptSegment], str, Path]] = []
        for batch in batches:
            key = batch_cache_key(batch, source_language=source_language, model=self.model)
            cache_path = self.cache_dir / f"{key}.json"
            cached = None if self.force else self._read_cached_batch(
                cache_path,
                batch,
                key,
                source_language=source_language,
            )
            if cached is not None:
                mappings.update(cached)
                logger.info("Using cached ChatGPT translation batch %s", key[:12])
            else:
                pending.append((batch, key, cache_path))
        if not pending:
            return mappings

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        async with self._chat_client_factory() as chat_client:
            for index, (batch, key, cache_path) in enumerate(pending, start=1):
                logger.info(
                    "Translating ChatGPT batch %d/%d (%d segments)",
                    index,
                    len(pending),
                    len(batch),
                )
                last_error: PlaywrightTranslationError | None = None
                for attempt in range(1, self.batch_max_attempts + 1):
                    response = await chat_client.ask(
                        build_translation_prompt(batch, source_language=source_language)
                    )
                    try:
                        translated = validate_translation_response(
                            response,
                            batch,
                            source_language=source_language,
                        )
                    except PlaywrightTranslationError as exc:
                        last_error = exc
                        if attempt >= self.batch_max_attempts:
                            raise PlaywrightTranslationError(
                                f"ChatGPT translation batch {key[:12]} failed validation after {attempt} attempts: {exc}"
                            ) from exc
                        logger.warning(
                            "ChatGPT translation batch %s returned invalid coverage/JSON; retrying only this batch (%d/%d): %s",
                            key[:12],
                            attempt,
                            self.batch_max_attempts,
                            exc,
                        )
                        continue
                    atomic_write_json(
                        cache_path,
                        {
                            "cache_key": key,
                            "provider": TRANSLATION_PROVIDER,
                            "model": self.model,
                            "prompt_version": TRANSLATION_PROMPT_VERSION,
                            "parser_version": TRANSLATION_PARSER_VERSION,
                            "translations": translated,
                        },
                    )
                    mappings.update(translated)
                    break
                else:  # pragma: no cover - guarded by raises/break above
                    assert last_error is not None
                    raise last_error
        return mappings

    @staticmethod
    def _read_cached_batch(
        path: Path,
        batch: list[TranscriptSegment],
        expected_key: str,
        *,
        source_language: str,
    ) -> dict[str, str] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("cache_key") != expected_key:
                return None
            translations = payload.get("translations")
            if not isinstance(translations, dict):
                return None
            response = json.dumps(translations, ensure_ascii=False)
            return validate_translation_response(response, batch, source_language=source_language)
        except (OSError, json.JSONDecodeError, PlaywrightTranslationError, AttributeError):
            return None
