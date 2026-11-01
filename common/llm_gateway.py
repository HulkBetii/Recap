"""OpenAI-compatible chat client for the local 9router LLM gateway.

The gateway exposes ``POST {base_url}/chat/completions``. Some upstream routes answer
with a Server-Sent Events body even when ``stream=False``, so responses are parsed
from raw text instead of through the OpenAI SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

GATEWAY_BACKEND = "ninerouter"
DEFAULT_GATEWAY_BASE_URL = "http://localhost:20128/v1"
GATEWAY_URL_ENV = "NINEROUTER_URL"
GATEWAY_KEY_ENV = "NINEROUTER_API_KEY"
# A concrete model id rather than a combo: combos are user-defined in each 9router install.
DEFAULT_GATEWAY_MODEL = "ag/gemini-3.8-flash-high"
DEFAULT_TIMEOUT_S = 600
# Shared Antigravity accounts often report "retry budget exhausted" for several minutes in a row.
DEFAULT_MAX_ATTEMPTS = 6
HEALTH_TIMEOUT_S = 10.0
DEFAULT_PROBE_TIMEOUT_S = 120.0
# 9router quarantines an upstream account for roughly 30s after overload.
QUARANTINE_WAIT_S = 32.0
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 30.0
MAX_RETRY_WAIT_S = 600.0
JSON_ONLY_SYSTEM_PROMPT = "Return only the JSON requested by the user prompt. Do not add Markdown fences or commentary."
RETRYABLE_SERVER_STATUSES = {500, 502, 504}
UPSTREAM_CLIENT_ERROR = re.compile(r"\[(?:400|401|403|404)\]|\bNOT_FOUND\b|requested entity was not found", re.IGNORECASE)
RESET_AFTER = re.compile(r"reset after (\d+(?:\.\d+)?)s", re.IGNORECASE)
PROBE_PROMPT = 'Return {"ok": true} exactly.'
PROBE_ATTEMPTS = 3
PROBE_MAX_WAIT_S = 20.0
# Errors that mean the gateway's account pool is saturated right now, so parallel requests only
# make it worse: the adaptive limiter halves its window on them.
THROTTLE_CODES = frozenset({"rate_limited", "quarantined", "upstream_rejected"})
# Errors after which a configured fallback model is worth trying instead of waiting on the primary.
FALLBACK_CODES = THROTTLE_CODES | {"upstream_error"}
FALLBACK_AFTER_FAILURES = 2
LIMITER_START = 2
LIMITER_RAMP_SUCCESSES = 8
LIMITER_HOLD_S = 60.0


class AdaptiveLimiter:
    """Bound in-flight gateway requests: ramp up while responses succeed, halve on throttling.

    The window starts at ``min(LIMITER_START, max_concurrency)``, grows by one after every
    ``LIMITER_RAMP_SUCCESSES`` consecutive successes up to ``max_concurrency``, and halves (never
    below 1) on a throttle error, holding that level for ``LIMITER_HOLD_S`` before it may grow again.
    """

    def __init__(self, max_concurrency: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.max_concurrency = max_concurrency
        self.limit = min(LIMITER_START, max_concurrency)
        self.in_flight = 0
        self.peak_concurrency = 0
        self.throttle_events = 0
        self._successes = 0
        self._held_until = 0.0
        self._clock = clock
        self._condition: asyncio.Condition | None = None

    def _cond(self) -> asyncio.Condition:
        if self._condition is None:
            self._condition = asyncio.Condition()
        return self._condition

    async def acquire(self) -> None:
        async with self._cond():
            await self._cond().wait_for(lambda: self.in_flight < self.limit)
            self.in_flight += 1
            self.peak_concurrency = max(self.peak_concurrency, self.in_flight)

    async def release(self) -> None:
        async with self._cond():
            self.in_flight = max(0, self.in_flight - 1)
            self._cond().notify_all()

    def record_success(self) -> None:
        self._successes += 1
        if self._successes >= LIMITER_RAMP_SUCCESSES and self.limit < self.max_concurrency and self._clock() >= self._held_until:
            self.limit += 1
            self._successes = 0

    def record_throttle(self) -> None:
        self.throttle_events += 1
        self._successes = 0
        self.limit = max(1, self.limit // 2)
        self._held_until = self._clock() + LIMITER_HOLD_S

    def summary(self) -> dict[str, int]:
        return {
            "max_concurrency": self.max_concurrency,
            "current_concurrency": self.limit,
            "peak_concurrency": self.peak_concurrency,
            "throttle_events": self.throttle_events,
        }


class GatewayChatError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool, attempts: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = attempts


@dataclass(frozen=True)
class ChatCompletion:
    content: str
    input_tokens: int
    output_tokens: int
    # The model that actually answered; differs from the requested id for 9router combos.
    served_model: str | None = None


@dataclass(frozen=True)
class GatewayHealth:
    reachable: bool
    model_available: bool
    reason: str | None = None


def resolve_gateway_base_url(configured: str | None = None) -> str:
    raw = (configured or os.getenv(GATEWAY_URL_ENV) or DEFAULT_GATEWAY_BASE_URL).strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raise GatewayChatError(f"LLM gateway base URL must start with http:// or https://: {raw}", code="config_error", retryable=False)
    return raw


def resolve_gateway_api_key() -> str | None:
    value = os.getenv(GATEWAY_KEY_ENV, "").strip()
    return value or None


def chat_completions_url(base_url: str) -> str:
    return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"


def _auth_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _usage_tokens(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def parse_chat_completion(body: str) -> ChatCompletion:
    """Parse a JSON or SSE chat-completions body into text plus token usage."""
    text = body.strip()
    if text.startswith("data:"):
        parts: list[str] = []
        input_tokens = output_tokens = 0
        served_model: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            if not data:
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as exc:
                raise GatewayChatError(f"LLM gateway sent an invalid SSE chunk: {exc}", code="invalid_response", retryable=True) from exc
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                message = choices[0].get("message") or {}
                parts.append(str(delta.get("content") or message.get("content") or ""))
            chunk_input, chunk_output = _usage_tokens(chunk)
            input_tokens = chunk_input or input_tokens
            output_tokens = chunk_output or output_tokens
            served_model = chunk.get("model") or served_model
        return ChatCompletion("".join(parts).strip(), input_tokens, output_tokens, served_model)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GatewayChatError(f"LLM gateway returned a non-JSON body: {text[:200]}", code="invalid_response", retryable=True) from exc
    if not isinstance(payload, dict):
        raise GatewayChatError("LLM gateway returned a JSON value that is not an object", code="invalid_response", retryable=True)
    choices = payload.get("choices") or []
    if not choices:
        raise GatewayChatError("LLM gateway response has no choices", code="invalid_response", retryable=True)
    message = choices[0].get("message") or {}
    input_tokens, output_tokens = _usage_tokens(payload)
    served_model = payload.get("model")
    return ChatCompletion(
        str(message.get("content") or "").strip(), input_tokens, output_tokens, str(served_model) if served_model else None
    )


def classify_http_error(status: int, body: str) -> GatewayChatError:
    snippet = body.strip()[:300]
    lowered = snippet.lower()
    if "quarantin" in lowered:
        return GatewayChatError(f"LLM gateway is temporarily unavailable (HTTP {status}): {snippet}", code="quarantined", retryable=True)
    if "response_too_large" in lowered:
        # Resending the same request cannot shrink the answer; the caller must split the work.
        return GatewayChatError(f"LLM gateway refused an oversized response (HTTP {status}): {snippet}", code="response_too_large", retryable=False)
    if status in {502, 503} and UPSTREAM_CLIENT_ERROR.search(snippet):
        # 9router wraps an upstream 4xx in a 503. It is often one pooled account lacking the model
        # ("reset after Ns"), so retry; a model no account serves is caught by the preflight probe.
        return GatewayChatError(f"LLM gateway upstream rejected the request (HTTP {status}): {snippet}", code="upstream_rejected", retryable=True)
    if status == 503:
        return GatewayChatError(f"LLM gateway is temporarily unavailable (HTTP {status}): {snippet}", code="quarantined", retryable=True)
    if status == 429:
        return GatewayChatError(f"LLM gateway rate limited the request: {snippet}", code="rate_limited", retryable=True)
    if status in RETRYABLE_SERVER_STATUSES:
        return GatewayChatError(f"LLM gateway upstream error (HTTP {status}): {snippet}", code="upstream_error", retryable=True)
    if status == 401:
        return GatewayChatError(
            f"LLM gateway rejected the credentials (HTTP 401); set {GATEWAY_KEY_ENV}: {snippet}",
            code="unauthorized",
            retryable=False,
        )
    if status == 403:
        return GatewayChatError(f"LLM gateway forbade the request (HTTP 403): {snippet}", code="forbidden", retryable=False)
    if status == 404:
        return GatewayChatError(f"LLM gateway endpoint or model not found (HTTP 404): {snippet}", code="not_found", retryable=False)
    return GatewayChatError(f"LLM gateway rejected the request (HTTP {status}): {snippet}", code="bad_request", retryable=False)


def retry_after_seconds(header: str | None, body: str, *, now: datetime | None = None) -> float | None:
    """Read a wait hint from a ``Retry-After`` header or the body ``retryAfter`` ISO timestamp."""
    if header:
        try:
            return max(float(header), 0.0)
        except ValueError:
            pass
    reset = RESET_AFTER.search(body)
    if reset:
        return float(reset.group(1))
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    value = payload.get("retryAfter") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        until = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return max((until - (now or datetime.now(timezone.utc))).total_seconds(), 0.0)


def _retry_delay(error: GatewayChatError, attempt: int, retry_after_s: float | None) -> float:
    if error.code == "quarantined":
        # 9router reports when an exhausted account budget resets; wait for it, bounded.
        return min(max(QUARANTINE_WAIT_S, retry_after_s or 0.0), MAX_RETRY_WAIT_S)
    if retry_after_s is not None:
        return min(retry_after_s, MAX_RETRY_WAIT_S)
    return min(BACKOFF_BASE_S * (2 ** (attempt - 1)), BACKOFF_CAP_S)


class GatewayChatClient:
    """Stateless chat client with the same ``ask(prompt)`` contract as the Playwright client."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        temperature: float = 0.0,
        system_prompt: str = JSON_ONLY_SYSTEM_PROMPT,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_concurrency: int = 1,
        fallback_models: list[str] | tuple[str, ...] | None = None,
        limiter: AdaptiveLimiter | None = None,
    ) -> None:
        if not model.strip():
            raise GatewayChatError("LLM gateway model must not be empty", code="config_error", retryable=False)
        if max_attempts < 1:
            raise GatewayChatError("LLM gateway max_attempts must be at least 1", code="config_error", retryable=False)
        if timeout_s <= 0:
            raise GatewayChatError("LLM gateway timeout_s must be positive", code="config_error", retryable=False)
        if max_concurrency < 1:
            raise GatewayChatError("LLM gateway max_concurrency must be at least 1", code="config_error", retryable=False)
        self.model = model.strip()
        self.fallback_models = [item.strip() for item in (fallback_models or []) if item and item.strip() and item.strip() != self.model]
        self.limiter = limiter or AdaptiveLimiter(max_concurrency)
        self.fallback_switches = 0
        self.base_url = resolve_gateway_base_url(base_url)
        self.api_key = api_key if api_key is not None else resolve_gateway_api_key()
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.temperature = temperature
        self.system_prompt = system_prompt
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None
        self.request_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.served_models: dict[str, int] = {}
        self.last_attempt_count = 0
        self.last_error_code: str | None = None
        self.logger = logging.getLogger("common.llm_gateway")

    async def __aenter__(self) -> "GatewayChatClient":
        self._client = httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ask(self, prompt: str, *, image_data_urls: list[str] | None = None) -> str:
        if self._client is None:
            raise GatewayChatError("GatewayChatClient is not started", code="client_not_started", retryable=False)
        user_content: str | list[dict[str, Any]] = prompt
        if image_data_urls:
            user_content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": url}} for url in image_data_urls
            ]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        url = chat_completions_url(self.base_url)
        self.last_attempt_count = 0
        self.last_error_code = None
        await self.limiter.acquire()
        try:
            return await self._ask_with_retries(url, payload)
        finally:
            await self.limiter.release()

    async def _ask_with_retries(self, url: str, payload: dict[str, Any]) -> str:
        assert self._client is not None
        models = [self.model, *self.fallback_models]
        model_index = 0
        failures_on_model = 0
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempt_count = attempt
            retry_after_s: float | None = None
            try:
                response = await self._client.post(url, json=payload, headers=_auth_headers(self.api_key))
            except httpx.TimeoutException as exc:
                error = GatewayChatError(f"LLM gateway request timed out after {self.timeout_s}s", code="timeout", retryable=True)
                error.__cause__ = exc
            except httpx.TransportError as exc:
                error = GatewayChatError(f"LLM gateway connection failed: {exc}", code="network", retryable=True)
                error.__cause__ = exc
            else:
                if response.status_code == 200:
                    try:
                        completion = parse_chat_completion(response.text)
                    except GatewayChatError as parse_error:
                        error = parse_error
                    else:
                        if completion.content:
                            self.request_count += 1
                            self.input_tokens += completion.input_tokens
                            self.output_tokens += completion.output_tokens
                            served = completion.served_model or models[model_index]
                            self.served_models[served] = self.served_models.get(served, 0) + 1
                            self.limiter.record_success()
                            self.logger.info(
                                "LLM gateway request %d completed with model=%s served_by=%s input_tokens=%d output_tokens=%d",
                                self.request_count,
                                models[model_index],
                                served,
                                completion.input_tokens,
                                completion.output_tokens,
                            )
                            return completion.content
                        error = GatewayChatError("LLM gateway returned an empty message", code="empty_response", retryable=True)
                else:
                    error = classify_http_error(response.status_code, response.text)
                    retry_after_s = retry_after_seconds(response.headers.get("retry-after"), response.text)
            self.last_error_code = error.code
            if error.code in THROTTLE_CODES:
                self.limiter.record_throttle()
            if not error.retryable or attempt >= self.max_attempts:
                raise GatewayChatError(str(error), code=error.code, retryable=error.retryable, attempts=attempt) from error
            delay = _retry_delay(error, attempt, retry_after_s)
            if error.code in FALLBACK_CODES:
                failures_on_model += 1
                if failures_on_model >= FALLBACK_AFTER_FAILURES and model_index + 1 < len(models):
                    # The primary keeps failing on pool capacity; a different model usually maps to
                    # other accounts, so switch instead of sleeping out the primary's reset window.
                    previous = models[model_index]
                    model_index += 1
                    failures_on_model = 0
                    payload["model"] = models[model_index]
                    self.fallback_switches += 1
                    delay = min(delay, BACKOFF_BASE_S)
                    self.logger.warning("LLM gateway switched model %s -> %s after repeated %s", previous, models[model_index], error.code)
            self.logger.warning(
                "LLM gateway attempt %d/%d failed (%s); retrying in %.0fs: %s",
                attempt,
                self.max_attempts,
                error.code,
                delay,
                error,
            )
            await self._sleep(delay)
        raise AssertionError("unreachable: retry loop always returns or raises")  # pragma: no cover

    def usage_summary(self) -> dict[str, Any]:
        return {
            "provider": GATEWAY_BACKEND,
            "model": self.model,
            "base_url": self.base_url,
            "request_count": self.request_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "served_models": dict(self.served_models),
            "fallback_models": list(self.fallback_models),
            "fallback_switches": self.fallback_switches,
            **self.limiter.summary(),
        }


def check_gateway_health(
    *,
    model: str | None,
    base_url: str | None = None,
    timeout_s: float = HEALTH_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> GatewayHealth:
    """Return whether the gateway answers ``GET /models`` and lists ``model``."""
    try:
        resolved = resolve_gateway_base_url(base_url)
    except GatewayChatError as exc:
        return GatewayHealth(reachable=False, model_available=False, reason=str(exc))
    try:
        with httpx.Client(timeout=timeout_s, transport=transport) as client:
            response = client.get(f"{resolved}/models", headers=_auth_headers(resolve_gateway_api_key()))
    except httpx.HTTPError as exc:
        return GatewayHealth(reachable=False, model_available=False, reason=f"LLM gateway is not reachable at {resolved}: {exc}")
    if response.status_code != 200:
        return GatewayHealth(reachable=False, model_available=False, reason=f"LLM gateway GET /models returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return GatewayHealth(reachable=False, model_available=False, reason="LLM gateway GET /models returned non-JSON")
    data = payload.get("data") if isinstance(payload, dict) else None
    model_ids = {str(item.get("id")) for item in data if isinstance(item, dict)} if isinstance(data, list) else set()
    if model and model not in model_ids:
        return GatewayHealth(reachable=True, model_available=False, reason=f"LLM gateway does not list model {model}")
    return GatewayHealth(reachable=True, model_available=True)


def probe_gateway_model(
    *,
    model: str,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> GatewayHealth:
    """Send a tiny completion: ``GET /models`` can list a model the upstream no longer serves."""
    try:
        resolved = resolve_gateway_base_url(base_url)
    except GatewayChatError as exc:
        return GatewayHealth(reachable=False, model_available=False, reason=str(exc))
    payload = {"model": model, "messages": [{"role": "user", "content": PROBE_PROMPT}], "temperature": 0.0, "stream": False}
    response: httpx.Response | None = None
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=timeout_s, transport=transport) as client:
                response = client.post(chat_completions_url(resolved), json=payload, headers=_auth_headers(resolve_gateway_api_key()))
        except httpx.HTTPError as exc:
            return GatewayHealth(reachable=False, model_available=False, reason=f"LLM gateway probe for {model} failed: {exc}")
        if response.status_code == 200:
            break
        error = classify_http_error(response.status_code, response.text)
        if error.code == "upstream_rejected":
            # Only a rejection that repeats across pooled accounts means the model is not served.
            if attempt < PROBE_ATTEMPTS:
                sleep(min(retry_after_seconds(response.headers.get("retry-after"), response.text) or BACKOFF_BASE_S, PROBE_MAX_WAIT_S))
                continue
            return GatewayHealth(reachable=True, model_available=False, reason=f"model {model} probe failed ({error.code}) {PROBE_ATTEMPTS} times: {error}")
        if error.retryable:
            # A busy account is not a misconfiguration; the run-time client waits and retries.
            return GatewayHealth(reachable=True, model_available=True, reason=f"model {model} is temporarily busy ({error.code})")
        return GatewayHealth(reachable=True, model_available=False, reason=f"model {model} probe failed ({error.code}): {error}")
    assert response is not None
    try:
        completion = parse_chat_completion(response.text)
    except GatewayChatError as exc:
        return GatewayHealth(reachable=True, model_available=False, reason=f"model {model} probe returned an unreadable body: {exc}")
    if not completion.content:
        return GatewayHealth(reachable=True, model_available=False, reason=f"model {model} probe returned an empty message")
    return GatewayHealth(reachable=True, model_available=True)
