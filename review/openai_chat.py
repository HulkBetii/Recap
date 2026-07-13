from __future__ import annotations

import asyncio
import logging
from typing import Any


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
    def __init__(self, primary: Any, fallback: OpenAIChatClient) -> None:
        self.primary = primary
        self.fallback = fallback
        self.fallback_active = False
        self.fallback_reason: str | None = None
        self.logger = logging.getLogger("review.fallback")

    async def ask(self, prompt: str) -> str:
        if self.fallback_active:
            return await self.fallback.ask(prompt)
        try:
            return await self.primary.ask(prompt)
        except Exception as exc:  # noqa: BLE001
            self.fallback_active = True
            self.fallback_reason = str(exc)
            self.logger.warning("ChatGPT Playwright failed; activating OpenAI review fallback: %s", exc)
            return await self.fallback.ask(prompt)

    def usage_summary(self) -> dict[str, Any]:
        return {
            **self.fallback.usage_summary(),
            "triggered": self.fallback_active,
            "trigger_reason": self.fallback_reason,
        }
