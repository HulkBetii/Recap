from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from review.openai_chat import FallbackChatClient, OpenAIChatClient


class _FakeCompletions:
    async def create(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["model"] == "gpt-test"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"beat_id": 0, "narration": "ok"}'))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        )


class _FailingPrimary:
    async def ask(self, _prompt: str) -> str:
        raise RuntimeError("no new assistant response")


def _fake_openai_client() -> OpenAIChatClient:
    client = OpenAIChatClient.__new__(OpenAIChatClient)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    client.model = "gpt-test"
    client.timeout_s = 10
    client.max_attempts = 1
    client.request_count = 0
    client.input_tokens = 0
    client.output_tokens = 0
    client.logger = logging.getLogger("test.review.openai")
    return client


def test_openai_chat_client_tracks_usage() -> None:
    client = _fake_openai_client()
    result = asyncio.run(client.ask("prompt"))

    assert '"beat_id": 0' in result
    assert client.usage_summary()["request_count"] == 1
    assert client.usage_summary()["input_tokens"] == 12
    assert client.usage_summary()["output_tokens"] == 7


def test_fallback_client_uses_openai_after_primary_failure() -> None:
    client = FallbackChatClient(_FailingPrimary(), _fake_openai_client())
    result = asyncio.run(client.ask("prompt"))

    assert '"narration": "ok"' in result
    assert client.usage_summary()["triggered"] is True
    assert "no new assistant response" in str(client.usage_summary()["trigger_reason"])
