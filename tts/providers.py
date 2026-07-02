from __future__ import annotations

import asyncio
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ProviderMode = Literal["auto", "ai33", "genmax"]

AI33_BASE_URL = "https://api.ai33.pro"
GENMAX_BASE_URL = "https://api.genmax.io"
RUNNING_STATUSES = {"pending", "queued", "processing", "running", "in_progress", "doing"}


class TtsProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    voice_id: str
    audio_url: str


class TtsProviderClient:
    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        genmax_voice_id: str | None,
        model: str,
        speed: float,
        provider_mode: ProviderMode,
        output_path: Path,
    ) -> ProviderResult:
        if provider_mode == "ai33":
            return await self._synthesize_ai33(text, voice_id, speed, output_path)
        if provider_mode == "genmax":
            gx_voice_id = genmax_voice_id or voice_id
            return await self._synthesize_genmax(text, gx_voice_id, model, output_path)
        try:
            return await self._synthesize_ai33(text, voice_id, speed, output_path)
        except Exception as ai33_error:
            gx_voice_id = genmax_voice_id or voice_id
            try:
                return await self._synthesize_genmax(text, gx_voice_id, model, output_path)
            except Exception as genmax_error:
                raise TtsProviderError(f"All TTS providers failed. AI33: {ai33_error}; Genmax: {genmax_error}") from genmax_error

    async def _synthesize_ai33(self, text: str, voice_id: str, speed: float, output_path: Path) -> ProviderResult:
        task_id = await asyncio.to_thread(submit_ai33, text, voice_id, speed)
        audio_url = await poll_ai33(task_id)
        await asyncio.to_thread(download_file, audio_url, output_path)
        return ProviderResult(provider="ai33", voice_id=voice_id, audio_url=audio_url)

    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        task_id = await asyncio.to_thread(submit_genmax, text, voice_id, model)
        audio_url = await poll_genmax(task_id)
        await asyncio.to_thread(download_file, audio_url, output_path)
        return ProviderResult(provider="genmax", voice_id=voice_id, audio_url=audio_url)


def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, data: bytes | None = None) -> dict:
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise TtsProviderError(f"HTTP {exc.code}: {body}") from exc


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
        payload = await asyncio.to_thread(
            http_json,
            f"{AI33_BASE_URL}/v1/task/{urllib.parse.quote(task_id)}",
            headers={"xi-api-key": api_key},
        )
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
        payload = await asyncio.to_thread(
            http_json,
            f"{GENMAX_BASE_URL}/v1/history/{urllib.parse.quote(task_id)}",
            headers={"xi-api-key": api_key},
        )
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
