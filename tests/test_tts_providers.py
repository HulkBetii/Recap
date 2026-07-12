from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from tts.providers import (
    ProviderResult,
    TtsProviderClient,
    TtsProviderError,
    http_json,
    resolve_provider_order,
    synthesize_openai,
)


class FallbackClient(TtsProviderClient):
    async def _synthesize_ai33(self, text: str, voice_id: str, speed: float, output_path: Path) -> ProviderResult:
        raise TtsProviderError("ai33 failed")

    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        output_path.write_bytes(b"mp3")
        return ProviderResult(provider="genmax", voice_id=voice_id, audio_url="file://ok")


class StrictClient(FallbackClient):
    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        raise AssertionError("genmax should not be called")


class OpenAiFallbackClient(FallbackClient):
    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        raise TtsProviderError("genmax failed")

    async def _synthesize_openai(self, text: str, voice_id: str, model: str, speed: float, output_path: Path) -> ProviderResult:
        output_path.write_bytes(b"mp3")
        return ProviderResult(provider="openai", voice_id=voice_id, audio_url="openai://ok", model=model)


def test_provider_auto_falls_back_to_genmax(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    monkeypatch.setenv("VIVOO_API_KEY", "ai33")
    monkeypatch.setenv("GENMAX_API_KEY", "genmax")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

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
    assert result.attempted_providers == ("ai33", "genmax")


def test_provider_auto_skips_missing_genmax_and_falls_back_to_openai(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    monkeypatch.setenv("VIVOO_API_KEY", "ai33")
    monkeypatch.delenv("GENMAX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai")

    result = asyncio.run(
        OpenAiFallbackClient().synthesize(
            text="hello",
            voice_id="ai33voice",
            genmax_voice_id="gxvoice",
            model="m",
            openai_model="gpt-4o-mini-tts",
            openai_voice="coral",
            speed=1.0,
            provider_mode="auto",
            output_path=tmp_path / "out.mp3",
        )
    )

    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini-tts"
    assert result.attempted_providers == ("ai33", "openai")


def test_provider_auto_requires_at_least_one_available_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    monkeypatch.delenv("VIVOO_API_KEY", raising=False)
    monkeypatch.delenv("GENMAX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(TtsProviderError, match="requires"):
        asyncio.run(
            FallbackClient().synthesize(
                text="hello",
                voice_id="voice",
                genmax_voice_id=None,
                model="m",
                speed=1.0,
                provider_mode="auto",
                output_path=Path("unused.mp3"),
            )
        )


@pytest.mark.parametrize(
    ("mode", "key", "expected"),
    [("ai33", "VIVOO_API_KEY", ["ai33"]), ("genmax", "GENMAX_API_KEY", ["genmax"]), ("openai", "OPENAI_API_KEY", ["openai"])],
)
def test_explicit_provider_modes_require_only_their_own_key(mode, key, expected) -> None:  # type: ignore[no-untyped-def]
    assert resolve_provider_order(
        mode,
        voice_id="voice",
        genmax_voice_id="genmax-voice",
        environ={key: "configured"},
    ) == expected


def test_provider_order_rejects_unknown_mode() -> None:
    with pytest.raises(TtsProviderError, match="unsupported provider_mode"):
        resolve_provider_order("unknown", voice_id="voice", genmax_voice_id=None, environ={})  # type: ignore[arg-type]


def test_provider_ai33_mode_does_not_fallback(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    monkeypatch.setenv("VIVOO_API_KEY", "ai33")

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


def test_openai_tts_retries_and_streams_atomically(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    class Response:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        def stream_to_file(self, path):  # type: ignore[no-untyped-def]
            Path(path).write_bytes(b"mp3")

    class Create:
        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("temporary")
            assert kwargs["model"] == "gpt-4o-mini-tts"
            assert kwargs["voice"] == "coral"
            assert kwargs["response_format"] == "mp3"
            return Response()

    class FakeOpenAI:
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            self.audio = type("Audio", (), {"speech": type("Speech", (), {"with_streaming_response": Create()})()})()

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    monkeypatch.setattr("tts.providers.time.sleep", lambda _seconds: None)
    output = tmp_path / "out.mp3"

    synthesize_openai("Xin chào", "coral", "gpt-4o-mini-tts", 1.0, output)

    assert output.read_bytes() == b"mp3"
    assert calls == 2
    assert not output.with_suffix(".mp3.part").exists()
