from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

ProviderMode = Literal["auto", "ai33", "genmax", "openai"]

AI33_BASE_URL = "https://api.ai33.pro"
GENMAX_BASE_URL = "https://api.genmax.io"
RUNNING_STATUSES = {"pending", "queued", "processing", "running", "in_progress", "doing"}
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
HTTP_MAX_ATTEMPTS = 3
HTTP_RETRY_BASE_DELAY_S = 1.0
OPENAI_MAX_ATTEMPTS = 3
OPENAI_RETRY_BASE_DELAY_S = 1.0
OPENAI_TIMEOUT_S = 300.0
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_VOICE = "coral"
OPENAI_VI_INSTRUCTIONS = (
    "Speak in natural Vietnamese with a clear female recap-review delivery. "
    "Keep names accurate, use dramatic but controlled pacing, and do not add words."
)
POLL_RETRYABLE_ERROR_PREFIXES = tuple(
    [f"HTTP {code}:" for code in sorted(RETRYABLE_HTTP_CODES)] + ["Network error:"]
)


class TtsProviderError(RuntimeError):
    pass


def is_retryable_poll_error(error: TtsProviderError) -> bool:
    return str(error).startswith(POLL_RETRYABLE_ERROR_PREFIXES)


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    voice_id: str
    audio_url: str
    model: str | None = None
    attempted_providers: tuple[str, ...] = ()


def resolve_provider_order(
    provider_mode: ProviderMode,
    *,
    voice_id: str,
    genmax_voice_id: str | None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    env = environ if environ is not None else os.environ
    if provider_mode not in {"auto", "ai33", "genmax", "openai"}:
        raise TtsProviderError(f"unsupported provider_mode: {provider_mode}")
    if provider_mode == "ai33":
        if not env.get("VIVOO_API_KEY", "").strip():
            raise TtsProviderError("VIVOO_API_KEY env var is required for provider_mode=ai33")
        return ["ai33"]
    if provider_mode == "genmax":
        if not env.get("GENMAX_API_KEY", "").strip():
            raise TtsProviderError("GENMAX_API_KEY env var is required for provider_mode=genmax")
        return ["genmax"]
    if provider_mode == "openai":
        if not env.get("OPENAI_API_KEY", "").strip():
            raise TtsProviderError("OPENAI_API_KEY env var is required for provider_mode=openai")
        return ["openai"]

    providers: list[str] = []
    if voice_id and env.get("VIVOO_API_KEY", "").strip():
        providers.append("ai33")
    if genmax_voice_id and env.get("GENMAX_API_KEY", "").strip():
        providers.append("genmax")
    if env.get("OPENAI_API_KEY", "").strip():
        providers.append("openai")
    if not providers:
        raise TtsProviderError(
            "provider_mode=auto requires VIVOO_API_KEY, GENMAX_API_KEY with --genmax-voice-id, or OPENAI_API_KEY"
        )
    return providers


class TtsProviderClient:
    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        genmax_voice_id: str | None,
        model: str,
        openai_model: str = DEFAULT_OPENAI_MODEL,
        openai_voice: str = DEFAULT_OPENAI_VOICE,
        speed: float,
        provider_mode: ProviderMode,
        output_path: Path,
    ) -> ProviderResult:
        provider_order = resolve_provider_order(
            provider_mode,
            voice_id=voice_id,
            genmax_voice_id=genmax_voice_id,
        )
        errors: list[str] = []
        attempted: list[str] = []
        for provider in provider_order:
            attempted.append(provider)
            try:
                if provider == "ai33":
                    result = await self._synthesize_ai33(text, voice_id, speed, output_path)
                    actual_model = result.model or model
                elif provider == "genmax":
                    result = await self._synthesize_genmax(text, genmax_voice_id or voice_id, model, output_path)
                    actual_model = result.model or model
                else:
                    result = await self._synthesize_openai(
                        text,
                        openai_voice,
                        openai_model,
                        speed,
                        output_path,
                    )
                    actual_model = result.model or openai_model
                return replace(result, model=actual_model, attempted_providers=tuple(attempted))
            except Exception as exc:  # noqa: BLE001 - provider chain must continue on provider-specific failures
                errors.append(f"{provider}: {exc}")
                logging.getLogger("tts.providers").warning("TTS provider %s failed; trying next provider: %s", provider, exc)
        raise TtsProviderError("All configured TTS providers failed. " + "; ".join(errors))

    async def _synthesize_ai33(self, text: str, voice_id: str, speed: float, output_path: Path) -> ProviderResult:
        task_id = await asyncio.to_thread(submit_ai33, text, voice_id, speed)
        audio_url = await poll_ai33(task_id)
        await asyncio.to_thread(download_file, audio_url, output_path)
        return ProviderResult(provider="ai33", voice_id=voice_id, audio_url=audio_url)

    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        task_id = await asyncio.to_thread(submit_genmax, text, voice_id, model)
        audio_url = await poll_genmax(task_id)
        await asyncio.to_thread(download_file, audio_url, output_path)
        return ProviderResult(provider="genmax", voice_id=voice_id, audio_url=audio_url, model=model)

    async def _synthesize_openai(
        self,
        text: str,
        voice_id: str,
        model: str,
        speed: float,
        output_path: Path,
    ) -> ProviderResult:
        await asyncio.to_thread(synthesize_openai, text, voice_id, model, speed, output_path)
        return ProviderResult(provider="openai", voice_id=voice_id, audio_url="openai://audio/speech", model=model)


def synthesize_openai(text: str, voice_id: str, model: str, speed: float, output_path: Path) -> None:
    from openai import OpenAI

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    client = OpenAI(max_retries=0, timeout=OPENAI_TIMEOUT_S)
    for attempt in range(OPENAI_MAX_ATTEMPTS):
        try:
            with client.audio.speech.with_streaming_response.create(
                model=model,
                voice=voice_id,
                input=text,
                instructions=OPENAI_VI_INSTRUCTIONS,
                response_format="mp3",
                speed=speed,
                timeout=OPENAI_TIMEOUT_S,
            ) as response:
                response.stream_to_file(temp_path)
            temp_path.replace(output_path)
            return
        except Exception:  # noqa: BLE001 - OpenAI SDK exposes multiple transport/API exception types
            if temp_path.exists():
                temp_path.unlink()
            if attempt == OPENAI_MAX_ATTEMPTS - 1:
                raise
            time.sleep(OPENAI_RETRY_BASE_DELAY_S * (2 ** attempt))


def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, data: bytes | None = None) -> dict:
    for attempt in range(HTTP_MAX_ATTEMPTS):
        request_headers = {"User-Agent": "Mozilla/5.0", **(headers or {})}
        request = urllib.request.Request(url, method=method, headers=request_headers, data=data)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == HTTP_MAX_ATTEMPTS - 1:
                raise TtsProviderError(f"HTTP {exc.code}: {body}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt == HTTP_MAX_ATTEMPTS - 1:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else str(exc)
                raise TtsProviderError(f"Network error: {reason}") from exc
        time.sleep(HTTP_RETRY_BASE_DELAY_S * (2 ** attempt))
    raise AssertionError("HTTP retry loop exhausted unexpectedly")


def submit_ai33(text: str, voice_id: str, speed: float) -> str:
    api_key = os.getenv("VIVOO_API_KEY", "").strip()
    if not api_key:
        raise TtsProviderError("VIVOO_API_KEY env var is not set")
    boundary = "----RecapTtsBoundary"
    fields = {
        "text": text,
        "voice_id": voice_id,
        "speed": f"{speed:g}",
        "with_transcript": "false",
    }
    body = build_multipart_body(fields, boundary)
    payload = http_json(
        f"{AI33_BASE_URL}/v3/text-to-speech",
        method="POST",
        headers={"xi-api-key": api_key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=body,
    )
    task_id = payload.get("task_id") or payload.get("id")
    if not task_id:
        raise TtsProviderError(f"AI33 response has no task_id/id: {payload}")
    return str(task_id)


async def poll_ai33(task_id: str, *, timeout_s: int = 900, interval_s: float = 5.0) -> str:
    api_key = os.getenv("VIVOO_API_KEY", "").strip()
    if not api_key:
        raise TtsProviderError("VIVOO_API_KEY env var is not set")
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            payload = await asyncio.to_thread(
                http_json,
                f"{AI33_BASE_URL}/v1/task/{urllib.parse.quote(task_id)}",
                headers={"xi-api-key": api_key},
            )
        except TtsProviderError as exc:
            if not is_retryable_poll_error(exc):
                raise
            logging.getLogger("tts.providers").warning(
                "AI33 task %s polling is temporarily unavailable; continuing until timeout: %s",
                task_id,
                exc,
            )
            await asyncio.sleep(interval_s)
            continue
        if payload.get("status") == "done":
            audio_url = (payload.get("metadata") or {}).get("audio_url")
            if not audio_url:
                raise TtsProviderError(f"AI33 task {task_id} done but no audio_url")
            return str(audio_url)
        if payload.get("status") not in RUNNING_STATUSES:
            raise TtsProviderError(f"AI33 task {task_id} failed: {payload}")
        await asyncio.sleep(interval_s)
    raise TtsProviderError(f"AI33 task {task_id} timed out")


def submit_genmax(text: str, voice_id: str, model: str) -> str:
    api_key = os.getenv("GENMAX_API_KEY", "").strip()
    if not api_key:
        raise TtsProviderError("GENMAX_API_KEY env var is not set")
    minimax = voice_id.isdigit()
    body = {
        "text": text,
        "model_id": "speech-2.8-turbo" if minimax else model,
        "language_code": "English" if minimax else "en",
    }
    if minimax:
        body["provider"] = "minimax"
    payload = http_json(
        f"{GENMAX_BASE_URL}/v1/text-to-speech/{urllib.parse.quote(voice_id)}",
        method="POST",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )
    task_id = payload.get("id")
    if not task_id:
        raise TtsProviderError(f"Genmax response has no id: {payload}")
    return str(task_id)


async def poll_genmax(task_id: str, *, timeout_s: int = 900, interval_s: float = 5.0) -> str:
    api_key = os.getenv("GENMAX_API_KEY", "").strip()
    if not api_key:
        raise TtsProviderError("GENMAX_API_KEY env var is not set")
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            payload = await asyncio.to_thread(
                http_json,
                f"{GENMAX_BASE_URL}/v1/history/{urllib.parse.quote(task_id)}",
                headers={"xi-api-key": api_key},
            )
        except TtsProviderError as exc:
            if not is_retryable_poll_error(exc):
                raise
            logging.getLogger("tts.providers").warning(
                "Genmax task %s polling is temporarily unavailable; continuing until timeout: %s",
                task_id,
                exc,
            )
            await asyncio.sleep(interval_s)
            continue
        if payload.get("status") == "completed":
            audio_url = (payload.get("result") or {}).get("audio_url")
            if not audio_url:
                raise TtsProviderError(f"Genmax task {task_id} completed but no audio_url")
            return str(audio_url)
        if payload.get("status") == "failed":
            raise TtsProviderError(f"Genmax task {task_id} failed: {payload}")
        await asyncio.sleep(interval_s)
    raise TtsProviderError(f"Genmax task {task_id} timed out")


def build_multipart_body(fields: dict[str, str], boundary: str) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


def download_file(url: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("file://"):
        shutil.copyfile(Path(urllib.parse.urlparse(url).path), output_path)
        return
    headers = {"User-Agent": "Mozilla/5.0"}
    if urllib.parse.urlparse(url).netloc.endswith("ai33.pro") and os.getenv("VIVOO_API_KEY"):
        headers["xi-api-key"] = os.getenv("VIVOO_API_KEY", "")
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        output_path.write_bytes(response.read())
