from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from review.openai_chat import FallbackChatClient, OpenAIChatClient
from review.playwright_chat import PlaywrightChatError


class _FakeCompletions:
    async def create(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["model"] == "gpt-test"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"beat_id": 0, "narration": "ok"}'))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
        )


class _SuccessfulPrimary:
    last_attempt_count = 1
    last_error_code = None

    async def ask(self, _prompt: str) -> str:
        return '{"ok": true}'


class _FailingPrimary:
    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, _prompt: str) -> str:
        self.calls += 1
        raise PlaywrightChatError(
            "no new assistant response",
            code="response_not_started",
            retryable=True,
            fallback_eligible=True,
            attempts=2,
        )


class _ProgrammingErrorPrimary:
    async def ask(self, _prompt: str) -> str:
        raise RuntimeError("programming error")


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


def test_fallback_keeps_openai_idle_when_playwright_succeeds() -> None:
    factory_calls = 0

    def factory() -> OpenAIChatClient:
        nonlocal factory_calls
        factory_calls += 1
        return _fake_openai_client()

    client = FallbackChatClient(_SuccessfulPrimary(), factory, model="gpt-test")

    assert asyncio.run(client.ask("prompt")) == '{"ok": true}'
    assert factory_calls == 0
    assert client.usage_summary()["triggered"] is False
    assert client.usage_summary()["request_count"] == 0


def test_fallback_client_uses_openai_after_playwright_retry_exhaustion() -> None:
    primary = _FailingPrimary()
    client = FallbackChatClient(primary, _fake_openai_client, model="gpt-test")
    result = asyncio.run(client.ask("prompt"))

    assert '"narration": "ok"' in result
    assert primary.calls == 1
    assert client.usage_summary()["triggered"] is True
    assert "no new assistant response" in str(client.usage_summary()["trigger_reason"])
    assert client.usage_summary()["playwright_attempts"] == 2
    assert client.usage_summary()["playwright_error_code"] == "response_not_started"


def test_active_circuit_breaker_routes_later_requests_to_openai() -> None:
    primary = _FailingPrimary()
    client = FallbackChatClient(primary, _fake_openai_client, model="gpt-test")

    asyncio.run(client.ask("first"))
    asyncio.run(client.ask("second"))

    assert primary.calls == 1
    assert client.usage_summary()["request_count"] == 2


def test_non_playwright_exception_does_not_activate_openai() -> None:
    client = FallbackChatClient(_ProgrammingErrorPrimary(), _fake_openai_client, model="gpt-test")

    with pytest.raises(RuntimeError, match="programming error"):
        asyncio.run(client.ask("prompt"))

    assert client.usage_summary()["triggered"] is False
    assert client.usage_summary()["request_count"] == 0


def test_budget_guard_blocks_fallback_without_constructing_openai() -> None:
    factory_calls = 0

    def factory() -> OpenAIChatClient:
        nonlocal factory_calls
        factory_calls += 1
        return _fake_openai_client()

    client = FallbackChatClient(_FailingPrimary(), factory, model="gpt-test", allowed=False)

    with pytest.raises(Exception, match="api_budget_guard=block"):
        asyncio.run(client.ask("prompt"))

    assert factory_calls == 0
    assert client.usage_summary()["blocked"] is True
    assert client.usage_summary()["triggered"] is False


def test_missing_key_factory_is_recorded_without_openai_request() -> None:
    def missing_key() -> OpenAIChatClient:
        raise RuntimeError("OPENAI_API_KEY is not set")

    client = FallbackChatClient(_FailingPrimary(), missing_key, model="gpt-test")

    with pytest.raises(Exception, match="OPENAI_API_KEY is not set"):
        asyncio.run(client.ask("prompt"))

    usage = client.usage_summary()
    assert usage["blocked"] is True
    assert usage["policy_allowed"] is True
    assert usage["allowed"] is False
    assert usage["triggered"] is False
    assert usage["request_count"] == 0
