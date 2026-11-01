"""CapCut Text-to-Speech API Client using httpx.

Ported from the user's trans-zh project (src/zhsub/dub/capcut/client.py); Recap classifies errors as transient or fatal.
"""

from __future__ import annotations

import functools
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import BASE_URL, DEFAULT_DEVICE
from .exceptions import CapCutAPIError, CapCutError, CapCutTaskError
from .signer import (
    base_headers,
    common_query,
    compact_json,
    escape_xml,
    make_sign_header,
    make_tts_payload_sign,
)

log = logging.getLogger(__name__)

DEFAULT_VOICE = "BV074_streaming"  # Cô Gái Hoạt Ngôn
DEFAULT_RESOURCE_ID = "7102355709945188865"
VOICE_CATALOG_PATH = Path(__file__).parent / "voices.json"
REQUEST_MAX_ATTEMPTS = 5
# CapCut documents no busy code, so overload is recognized by message markers in a non-zero `ret` response.
BUSY_MARKERS = ("busy", "too many", "too frequent", "rate limit", "ratelimit", "throttl")


@dataclass(frozen=True, slots=True)
class CapCutVoice:
    voice_type: str
    display_name: str
    resource_id: str
    lang: str
    lan: str


@functools.lru_cache(maxsize=1)
def load_voice_catalog() -> tuple[CapCutVoice, ...]:
    """Load available voices from the packaged voices.json."""
    if not VOICE_CATALOG_PATH.is_file():
        return ()
    try:
        with open(VOICE_CATALOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return tuple(
            CapCutVoice(
                voice_type=v["voice_type"],
                display_name=v.get("display_name", v["voice_type"]),
                resource_id=v.get("resource_id", ""),
                lang=v.get("lang", ""),
                lan=v.get("lan", ""),
            )
            for v in data
            if isinstance(v, dict) and "voice_type" in v
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("Could not read voices.json catalog: %s", exc)
        return ()


def is_busy_response(data: dict[str, Any]) -> bool:
    """Return True when a non-zero `ret` response reports overload rather than a request problem."""
    text = " ".join(str(data.get(key, "")) for key in ("ret", "code", "errmsg", "message", "msg")).casefold()
    return any(marker in text for marker in BUSY_MARKERS)


class CapCutClient:
    """Pure Python client for CapCut Text-to-Speech (TTS) API."""

    def __init__(
        self,
        device: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        timeout_sec: float = 60.0,
    ) -> None:
        self.device = dict(device or DEFAULT_DEVICE)
        self._external_client = client is not None
        self._client = client or httpx.Client(timeout=timeout_sec)

    def close(self) -> None:
        if not self._external_client:
            self._client.close()

    def __enter__(self) -> CapCutClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -------------------------------------------------------------------------
    # Voice Resolution
    # -------------------------------------------------------------------------

    def list_voices(self, lang: str | None = None) -> list[CapCutVoice]:
        """Load available voices from packaged voices.json."""
        voices = load_voice_catalog()
        if not lang:
            return list(voices)
        l_lower = lang.lower().strip()
        return [
            v
            for v in voices
            if l_lower in v.lang.lower() or l_lower in v.lan.lower()
        ]

    def resolve_voice(
        self, voice: str | None = None, resource_id: str | None = None
    ) -> tuple[str, str]:
        """Resolve voice_type and resource_id from catalog or fallback to default."""
        target = (voice or DEFAULT_VOICE).strip()
        if resource_id and resource_id.strip():
            return target, resource_id.strip()

        target_lower = target.lower()
        for v in self.list_voices():
            if v.voice_type.lower() == target_lower:
                return v.voice_type, v.resource_id or DEFAULT_RESOURCE_ID
            if v.display_name.lower() == target_lower:
                return v.voice_type, v.resource_id or DEFAULT_RESOURCE_ID

        return target, DEFAULT_RESOURCE_ID

    # -------------------------------------------------------------------------
    # Request Builders
    # -------------------------------------------------------------------------

    def build_tts_new_request(
        self,
        texts: str | list[str],
        voice: str | None = DEFAULT_VOICE,
        resource_id: str | None = None,
        rate: str = "1.0",
    ) -> tuple[str, dict[str, str], bytes]:
        text_list = [texts] if isinstance(texts, str) else list(texts)
        if not text_list:
            raise ValueError("texts must contain at least one string")

        voice_type, final_resource_id = self.resolve_voice(voice=voice, resource_id=resource_id)
        babi = {
            "feature_entrance": "editor",
            "feature_entrance_detail": "editor-feature-text_to_speech",
            "feature_key": "text_to_speech",
            "scenario": "video_editor",
        }
        voice_blocks = [
            f'    <voice name="{voice_type}" mock_tone_info="" platform="sami" '
            f'resource_id="{final_resource_id}" emotion="" emotion_scale="0" style="" role="" '
            f'moyin_emotion="" is_clone_tone="false" need_subtitle_timestamp="false">\n'
            f'        <prosody rate="{rate}">{escape_xml(t)}</prosody>\n'
            f'    </voice>'
            for t in text_list
        ]
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">\n'
            + "\n".join(voice_blocks)
            + "\n</speak>"
        )
        extra_info = compact_json({"benefit_info": {}})
        payload = {
            "audio_format": "mp3",
            "babi_param": compact_json(babi),
            "credit_disable": False,
            "extra_info": extra_info,
            "need_merge_voice": False,
            "need_subtitle_timestamp": False,
            "scene": "text_to_speech",
            "ssml": ssml,
        }
        payload["sign"] = make_tts_payload_sign(
            ssml, extra_info, self.device["device_id"], self.device["aid"]
        )
        body = {
            "bind_id": str(uuid.uuid4()),
            "can_queue": True,
            "enter_from": "text_to_speech",
            "tasks": [
                {
                    "context": str(uuid.uuid4()),
                    "payload": compact_json(payload),
                    "req_key": "sami_text_to_speech",
                    "task_version": "v3",
                }
            ],
        }

        body_text = compact_json(body)
        query = common_query(self.device, babi, include_region=True)
        url = f"{BASE_URL}/lv/v1/common_task/new?{urlencode(query)}"
        headers = base_headers(self.device, body_text, appid=True)
        lower_headers = {k.lower(): v for k, v in headers.items()}
        if "sign" not in lower_headers:
            headers["sign"] = make_sign_header(
                url, self.device["appvr"], lower_headers["device-time"], self.device["tdid"]
            )
        return url, headers, body_text.encode("utf-8")

    def build_query_request(
        self, task_id: str, token: str, bind_id: str = ""
    ) -> tuple[str, dict[str, str], bytes]:
        body = {
            "tasks": [
                {
                    "bind_id": bind_id,
                    "id": task_id,
                    "req_key": "sami_text_to_speech",
                    "task_version": "v3",
                    "token": token,
                }
            ]
        }
        body_text = compact_json(body)
        query = common_query(self.device, None, include_region=False)
        url = f"{BASE_URL}/lv/v1/common_task/query?{urlencode(query)}"
        headers = base_headers(self.device, body_text, appid=True)
        lower_headers = {k.lower(): v for k, v in headers.items()}
        if "sign" not in lower_headers:
            headers["sign"] = make_sign_header(
                url, self.device["appvr"], lower_headers["device-time"], self.device["tdid"]
            )
        return url, headers, body_text.encode("utf-8")

    # -------------------------------------------------------------------------
    # Core API Calls
    # -------------------------------------------------------------------------

    def _post_json(self, url: str, headers: dict[str, str], body_bytes: bytes, label: str) -> dict[str, Any]:
        """POST with short in-client retries; 429/5xx/network exhaustion is raised as transient."""
        last_status = 0
        for attempt in range(REQUEST_MAX_ATTEMPTS):
            last_attempt = attempt == REQUEST_MAX_ATTEMPTS - 1
            try:
                resp = self._client.post(url, headers=headers, content=body_bytes)
            except httpx.HTTPError as exc:
                if last_attempt:
                    raise CapCutAPIError(f"{label} request failed: {exc}", transient=True) from exc
                time.sleep(1.0 + random.random())
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_status = resp.status_code
                time.sleep(1.0 + random.random())
                continue
            if resp.status_code >= 400:
                raise CapCutAPIError(
                    f"{label} HTTP {resp.status_code}: {resp.text[:300]}", status_code=resp.status_code
                )
            try:
                data = resp.json()
            except ValueError as exc:
                if last_attempt:
                    raise CapCutAPIError(
                        f"{label} returned non-JSON: {resp.text[:200]}", status_code=resp.status_code
                    ) from exc
                time.sleep(1.0 + random.random())
                continue
            if not isinstance(data, dict):
                raise CapCutAPIError(f"{label} returned a non-object JSON body: {data!r}"[:300])
            return data

        raise CapCutAPIError(
            f"{label}: retry limit exceeded (last HTTP {last_status})", status_code=last_status, transient=True
        )

    def create_tts_task(
        self,
        texts: str | list[str],
        voice: str | None = DEFAULT_VOICE,
        resource_id: str | None = None,
        rate: str = "1.0",
    ) -> dict[str, Any]:
        """Submit a TTS task to the CapCut API and return the JSON response."""
        url, headers, body_bytes = self.build_tts_new_request(texts, voice, resource_id, rate)
        data = self._post_json(url, headers, body_bytes, "create_tts_task")
        if data.get("ret") != "0" and data.get("ret") != 0:
            raise CapCutAPIError(f"CapCut API error: {data}", response_data=data, transient=is_busy_response(data))
        return data

    def query_tts_task(self, task_id: str, token: str, bind_id: str = "") -> dict[str, Any]:
        """Query TTS task status."""
        url, headers, body_bytes = self.build_query_request(task_id, token, bind_id)
        return self._post_json(url, headers, body_bytes, "query_tts_task")

    def synthesize(
        self,
        text: str,
        voice: str | None = DEFAULT_VOICE,
        resource_id: str | None = None,
        rate: str = "1.0",
    ) -> tuple[str, str]:
        """Create a TTS task and return (task_id, token)."""
        create_res = self.create_tts_task(text, voice, resource_id, rate)
        tasks = (create_res.get("data") or {}).get("tasks") or []
        if not tasks:
            raise CapCutTaskError(f"CapCut returned no task: {create_res}")
        task_id = str(tasks[0]["id"])
        token = str(tasks[0]["token"])
        return task_id, token

    def wait(
        self,
        task_id: str,
        token: str,
        *,
        poll_interval: float = 1.0,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        """Wait for the TTS task to finish and return a dict with audio_url and duration."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            query_res = self.query_tts_task(task_id, token)
            query_tasks = (query_res.get("data") or {}).get("tasks") or []
            if query_tasks:
                status = query_tasks[0].get("status")
                # CapCut reports 'succeed' rather than 'success'.
                if status in ("succeed", "success"):
                    raw_payload = query_tasks[0].get("payload")
                    if isinstance(raw_payload, str):
                        payload = json.loads(raw_payload)
                    elif isinstance(raw_payload, dict):
                        payload = raw_payload
                    else:
                        payload = {}
                    subtitles = payload.get("audio_subtitles") or []
                    if not subtitles:
                        raise CapCutTaskError(f"audio_subtitles missing from payload: {payload}")
                    first_sub = subtitles[0]
                    speech_url = first_sub.get("speech_url")
                    if not speech_url:
                        raise CapCutTaskError(f"speech_url missing from result: {first_sub}")
                    duration_ms = first_sub.get("duration", 0)
                    return {
                        "audio_url": speech_url,
                        "duration_sec": float(duration_ms) / 1000.0 if duration_ms else 0.0,
                        "raw": first_sub,
                    }
                if status == "failed":
                    raise CapCutTaskError(f"CapCut TTS task failed: {query_res}")

            time.sleep(poll_interval)

        raise CapCutTaskError(f"CapCut TTS task {task_id} timed out after {timeout:.0f}s")

    def download(self, url: str, dst: Path) -> Path:
        """Download the MP3 from URL to disk with atomic replace."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f"{dst.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            try:
                with self._client.stream("GET", url, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                raise CapCutAPIError(
                    f"audio download HTTP {status}", status_code=status, transient=status == 429 or status >= 500
                ) from exc
            except httpx.HTTPError as exc:
                raise CapCutAPIError(f"audio download failed: {exc}", transient=True) from exc
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise CapCutError(f"Downloaded audio from {url} is empty", transient=True)
            tmp.replace(dst)
            return dst
        finally:
            if tmp.is_file():
                tmp.unlink(missing_ok=True)
