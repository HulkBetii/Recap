from __future__ import annotations

from pathlib import Path

import pytest

from tts.providers import ProviderResult, TtsProviderClient, TtsProviderError


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
