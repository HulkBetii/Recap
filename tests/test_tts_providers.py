from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from tts.providers import ProviderResult, TtsProviderClient, TtsProviderError, http_json


class FallbackClient(TtsProviderClient):
    async def _synthesize_ai33(self, text: str, voice_id: str, speed: float, output_path: Path) -> ProviderResult:
        raise TtsProviderError("ai33 failed")

    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        output_path.write_bytes(b"mp3")
        return ProviderResult(provider="genmax", voice_id=voice_id, audio_url="file://ok")


class StrictClient(FallbackClient):
    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        raise AssertionError("genmax should not be called")


def test_provider_auto_falls_back_to_genmax(tmp_path) -> None:
    import asyncio

    result = asyncio.run(
        FallbackClient().synthesize(
            text="hello",
            voice_id="ai33voice",
            genmax_voice_id="gxvoice",
            model="m",
            speed=1.0,
            provider_mode="auto",
            output_path=tmp_path / "out.mp3",
        )
    )

    assert result.provider == "genmax"
    assert result.voice_id == "gxvoice"


def test_provider_ai33_mode_does_not_fallback(tmp_path) -> None:
    import asyncio

    with pytest.raises(TtsProviderError, match="ai33 failed"):
        asyncio.run(
            StrictClient().synthesize(
                text="hello",
                voice_id="ai33voice",
                genmax_voice_id="gxvoice",
                model="m",
                speed=1.0,
                provider_mode="ai33",
                output_path=tmp_path / "out.mp3",
            )
        )


def test_http_json_retries_transient_gateway_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    class Response:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return json.dumps({"status": "ok"}).encode("utf-8")

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b""))
        return Response()

    monkeypatch.setattr("tts.providers.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("tts.providers.time.sleep", lambda _seconds: None)

    assert http_json("https://example.com") == {"status": "ok"}
    assert calls == 2


def test_http_json_retries_socket_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    class Response:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        def read(self) -> bytes:
            return json.dumps({"status": "ok"}).encode("utf-8")

    def fake_urlopen(_request, timeout):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("read operation timed out")
        return Response()

    monkeypatch.setattr("tts.providers.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("tts.providers.time.sleep", lambda _seconds: None)

    assert http_json("https://example.com") == {"status": "ok"}
    assert calls == 2
