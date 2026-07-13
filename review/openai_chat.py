from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from review.playwright_chat import PlaywrightChatError


class OpenAIChatError(RuntimeError):
    pass


class OpenAIChatClient:
    def __init__(self, api_key: str, *, model: str, timeout_s: int = 300, max_attempts: int = 3) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.request_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.logger = logging.getLogger("review.openai")

    async def ask(self, prompt: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": "Return only the JSON requested by the user prompt. Do not add Markdown fences or commentary.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    timeout=self.timeout_s,
                )
                self.request_count += 1
                usage = getattr(response, "usage", None)
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                self.input_tokens += prompt_tokens
                self.output_tokens += completion_tokens
                content = (response.choices[0].message.content or "").strip()
                if not content:
                    raise OpenAIChatError("OpenAI review response was empty")
                self.logger.info(
                    "OpenAI review fallback request %d completed with model=%s input_tokens=%d output_tokens=%d",
                    self.request_count,
                    self.model,
                    prompt_tokens,
                    completion_tokens,
                )
                return content
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_attempts - 1:
                    await asyncio.sleep(2**attempt)
        raise OpenAIChatError(f"OpenAI review fallback failed after {self.max_attempts} attempts: {last_error}") from last_error

    def usage_summary(self) -> dict[str, Any]:
        return {
            "provider": "openai",
            "model": self.model,
            "request_count": self.request_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class FallbackChatClient:
    def __init__(
        self,
        primary: Any,
        fallback_factory: Callable[[], OpenAIChatClient],
        *,
        model: str,
        allowed: bool = True,
    ) -> None:
        self.primary = primary
        self.fallback_factory = fallback_factory
        self.model = model
        self.policy_allowed = allowed
        self.fallback: OpenAIChatClient | None = None
        self.fallback_active = False
        self.blocked = False
        self.block_reason: str | None = None
        self.fallback_reason: str | None = None
        self.playwright_error_code: str | None = None
        self.playwright_attempts = 0
        self.logger = logging.getLogger("review.fallback")

    async def ask(self, prompt: str) -> str:
        if self.fallback_active:
            assert self.fallback is not None
            return await self.fallback.ask(prompt)
        try:
            return await self.primary.ask(prompt)
        except PlaywrightChatError as exc:
            if not exc.fallback_eligible:
                raise
            self.fallback_reason = str(exc)
            self.playwright_error_code = exc.code
            self.playwright_attempts = exc.attempts
            if not self.policy_allowed:
                self.blocked = True
                self.block_reason = "api_budget_guard=block"
                raise OpenAIChatError(
                    "ChatGPT Playwright failed after retry exhaustion; OpenAI review fallback is blocked by api_budget_guard=block"
                ) from exc
            try:
                self.fallback = self.fallback_factory()
            except Exception as factory_error:  # noqa: BLE001
                self.blocked = True
                self.block_reason = str(factory_error)
                raise OpenAIChatError(
                    f"ChatGPT Playwright failed after retry exhaustion; OpenAI review fallback is unavailable: {factory_error}"
                ) from factory_error
            self.fallback_active = True
            self.logger.warning(
                "ChatGPT Playwright failed after %d attempt(s); activating OpenAI review fallback: %s",
                exc.attempts,
                exc,
            )
            return await self.fallback.ask(prompt)

    def usage_summary(self) -> dict[str, Any]:
        fallback_usage = self.fallback.usage_summary() if self.fallback is not None else {
            "provider": "openai",
            "model": self.model,
            "request_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        return {
            **fallback_usage,
            "configured": True,
            "policy_allowed": self.policy_allowed,
            "allowed": self.policy_allowed and not self.blocked,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "triggered": self.fallback_active,
            "trigger_reason": self.fallback_reason,
            "playwright_attempts": self.playwright_attempts or int(getattr(self.primary, "last_attempt_count", 0) or 0),
            "playwright_error_code": self.playwright_error_code or getattr(self.primary, "last_error_code", None),
        }
