from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from common.llm_gateway import (
    QUARANTINE_WAIT_S,
    GatewayChatClient,
    GatewayChatError,
    check_gateway_health,
    parse_chat_completion,
    probe_gateway_model,
    resolve_gateway_base_url,
    retry_after_seconds,
)


def _completion(content: str, *, prompt_tokens: int = 10, completion_tokens: int = 5) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class _Recorder:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _run(client_kwargs: dict, responses: list[httpx.Response | Exception], prompt: str = "hello") -> tuple[str, GatewayChatClient, _Recorder, list[float]]:
    recorder = _Recorder(responses)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario() -> tuple[str, GatewayChatClient]:
        client = GatewayChatClient(
            model="ag/test-model",
            base_url="http://gateway.test/v1",
            api_key="k-test",
            transport=httpx.MockTransport(recorder),
            sleep=fake_sleep,
            **client_kwargs,
        )
        async with client:
            return await client.ask(prompt), client

    result, client = asyncio.run(scenario())
    return result, client, recorder, sleeps


def test_ask_posts_openai_payload_and_tracks_usage() -> None:
    result, client, recorder, sleeps = _run({}, [httpx.Response(200, json=_completion('{"ok": true}'))], prompt="PROMPT")

    assert result == '{"ok": true}'
    assert sleeps == []
    request = recorder.requests[0]
    assert str(request.url) == "http://gateway.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer k-test"
    body = json.loads(request.content)
    assert body["model"] == "ag/test-model"
    assert body["stream"] is False
    assert body["temperature"] == 0.0
    assert body["messages"][-1] == {"role": "user", "content": "PROMPT"}
    assert client.usage_summary() == {
        "provider": "ninerouter",
        "model": "ag/test-model",
        "base_url": "http://gateway.test/v1",
        "request_count": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "served_models": {"ag/test-model": 1},
    }


def test_combo_records_the_model_that_actually_answered() -> None:
    body = _completion("ok")
    body["model"] = "claude-sonnet-4-6"
    _result, client, _recorder, _sleeps = _run({}, [httpx.Response(200, json=body)])
    assert client.usage_summary()["served_models"] == {"claude-sonnet-4-6": 1}


def test_parse_sse_body_even_when_not_streaming() -> None:
    body = "\n".join(
        [
            'data: {"choices":[{"delta":{"content":"{\\"a\\":"}}]}',
            'data: {"choices":[{"delta":{"content":" 1}"}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}',
            "data: [DONE]",
        ]
    )
    completion = parse_chat_completion(body)
    assert completion.content == '{"a": 1}'
    assert (completion.input_tokens, completion.output_tokens) == (3, 2)


def test_rate_limit_retries_with_backoff_then_succeeds() -> None:
    result, client, recorder, sleeps = _run(
        {},
        [httpx.Response(429, text="slow down"), httpx.Response(200, json=_completion("ok"))],
    )
    assert result == "ok"
    assert len(recorder.requests) == 2
    assert sleeps == [2.0]
    assert client.last_attempt_count == 2


def test_quarantine_waits_at_least_32_seconds() -> None:
    result, _client, _recorder, sleeps = _run(
        {},
        [httpx.Response(503, text="account quarantined"), httpx.Response(200, json=_completion("ok"))],
    )
    assert result == "ok"
    assert sleeps == [QUARANTINE_WAIT_S]


UPSTREAM_404_BODY = json.dumps(
    {"error": {"message": '[antigravity/gemini-x] [404]: {"error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}} (reset after 1s)'}}
)


def test_503_wrapping_upstream_404_retries_after_account_reset() -> None:
    # One pooled account may lack the model; 9router says when it rotates ("reset after 1s").
    result, _client, recorder, sleeps = _run({}, [httpx.Response(503, text=UPSTREAM_404_BODY), httpx.Response(200, json=_completion("ok"))])
    assert result == "ok"
    assert len(recorder.requests) == 2
    assert sleeps == [1.0]


def test_response_too_large_is_not_retried() -> None:
    body = json.dumps({"error": {"message": "response_too_large", "type": "server_error", "code": "bad_gateway"}})
    with pytest.raises(GatewayChatError) as excinfo:
        _run({}, [httpx.Response(502, text=body), httpx.Response(200, json=_completion("never"))])
    assert excinfo.value.code == "response_too_large"
    assert excinfo.value.attempts == 1


def test_probe_detects_listed_but_unserved_model() -> None:
    calls: list[float] = []
    transport = httpx.MockTransport(lambda _request: httpx.Response(503, text=UPSTREAM_404_BODY))
    health = probe_gateway_model(model="ag/gemini-x", base_url="http://gateway.test/v1", transport=transport, sleep=calls.append)
    assert health.reachable is True
    assert health.model_available is False
    assert "upstream_rejected" in (health.reason or "")
    assert calls == [1.0, 1.0]

    ok = httpx.MockTransport(lambda _request: httpx.Response(200, json=_completion('{"ok": true}')))
    assert probe_gateway_model(model="ag/a", base_url="http://gateway.test/v1", transport=ok).model_available is True


def test_probe_accepts_model_after_one_account_rejection() -> None:
    responses = [httpx.Response(503, text=UPSTREAM_404_BODY), httpx.Response(200, json=_completion('{"ok": true}'))]
    transport = httpx.MockTransport(lambda _request: responses.pop(0))
    health = probe_gateway_model(model="ag/a", base_url="http://gateway.test/v1", transport=transport, sleep=lambda _s: None)
    assert health.model_available is True


def test_budget_exhausted_waits_until_body_retry_after() -> None:
    until = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    body = json.dumps({"error": {"code": 503, "message": "Account retry budget exhausted"}, "retryAfter": until})
    result, _client, _recorder, sleeps = _run({}, [httpx.Response(503, text=body), httpx.Response(200, json=_completion("ok"))])
    assert result == "ok"
    assert 110 <= sleeps[0] <= 120


def test_retry_after_seconds_parsing() -> None:
    now = datetime(2026, 10, 31, 17, 40, tzinfo=timezone.utc)
    assert retry_after_seconds("7", "", now=now) == 7.0
    assert retry_after_seconds(None, json.dumps({"retryAfter": "2026-10-31T17:45:00Z"}), now=now) == 300.0
    assert retry_after_seconds(None, json.dumps({"retryAfter": "2026-10-31T17:00:00Z"}), now=now) == 0.0
    assert retry_after_seconds(None, "not json", now=now) is None


def test_probe_treats_busy_account_as_available() -> None:
    body = json.dumps({"error": {"code": 503, "message": "Account retry budget exhausted"}})
    transport = httpx.MockTransport(lambda _request: httpx.Response(503, text=body))
    health = probe_gateway_model(model="ag/a", base_url="http://gateway.test/v1", transport=transport)
    assert health.model_available is True
    assert "temporarily busy" in (health.reason or "")


def test_unauthorized_fails_fast_without_retry() -> None:
    with pytest.raises(GatewayChatError) as excinfo:
        _run({}, [httpx.Response(401, text="bad key"), httpx.Response(200, json=_completion("never"))])
    error = excinfo.value
    assert error.code == "unauthorized"
    assert error.retryable is False
    assert error.attempts == 1
    assert "NINEROUTER_API_KEY" in str(error)


def test_unknown_model_is_not_retried() -> None:
    with pytest.raises(GatewayChatError) as excinfo:
        _run({}, [httpx.Response(400, text="model not found"), httpx.Response(200, json=_completion("never"))])
    assert excinfo.value.code == "bad_request"
    assert excinfo.value.attempts == 1


def test_empty_message_is_retried_then_exhausts() -> None:
    with pytest.raises(GatewayChatError) as excinfo:
        _run({"max_attempts": 2}, [httpx.Response(200, json=_completion("")), httpx.Response(200, json=_completion("   "))])
    assert excinfo.value.code == "empty_response"
    assert excinfo.value.attempts == 2


def test_network_error_is_retried() -> None:
    result, _client, recorder, _sleeps = _run(
        {},
        [httpx.ConnectError("refused"), httpx.Response(200, json=_completion("ok"))],
    )
    assert result == "ok"
    assert len(recorder.requests) == 2


def test_ask_requires_started_client() -> None:
    client = GatewayChatClient(model="m", base_url="http://gateway.test/v1")
    with pytest.raises(GatewayChatError) as excinfo:
        asyncio.run(client.ask("x"))
    assert excinfo.value.code == "client_not_started"


def test_base_url_resolution_prefers_config_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NINEROUTER_URL", "http://env.test/v1/")
    assert resolve_gateway_base_url(None) == "http://env.test/v1"
    assert resolve_gateway_base_url("http://cfg.test/v1") == "http://cfg.test/v1"
    with pytest.raises(GatewayChatError):
        resolve_gateway_base_url("localhost:20128/v1")


def test_health_reports_missing_model() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"data": [{"id": "ag/a"}, {"id": "ag/b"}]}))
    assert check_gateway_health(model="ag/a", base_url="http://gateway.test/v1", transport=transport).model_available is True
    missing = check_gateway_health(model="ag/z", base_url="http://gateway.test/v1", transport=transport)
    assert missing.reachable is True
    assert missing.model_available is False


def test_health_reports_unreachable_gateway() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    health = check_gateway_health(model="ag/a", base_url="http://gateway.test/v1", transport=httpx.MockTransport(refuse))
    assert health.reachable is False
    assert "not reachable" in (health.reason or "")
