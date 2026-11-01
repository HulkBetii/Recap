from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common.llm_gateway import GatewayChatClient, GatewayChatError
from ingest.llm import VisionUnavailableError

VISION_SYSTEM_PROMPT = (
    "Describe only what is visible in the frame. "
    "Use 1-2 concise English sentences covering characters, action, setting, and mood. "
    "Do not infer plot beyond the image. Ignore burned-in subtitles, captions, logos and watermarks."
)
VISION_USER_PROMPT = "Describe this frame for a movie recap scene map."
# Vision is best-effort, so it gives up sooner than script writing does.
VISION_MAX_ATTEMPTS = 3
# Consecutive frames that still fail after retries mean the gateway is saturated; stop instead of
# spending hours on retries for the remaining frames.
MAX_CONSECUTIVE_FAILURES = 3


class GatewayVisionClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        concurrency: int = 3,
        timeout_s: int = 180,
        chat_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("vision concurrency must be at least 1")
        self.model = model
        self.concurrency = concurrency
        self.logger = logging.getLogger("ingest.gateway_vision")
        self._chat_client_factory = chat_client_factory or (
            lambda: GatewayChatClient(
                model=model,
                base_url=base_url,
                timeout_s=timeout_s,
                max_attempts=VISION_MAX_ATTEMPTS,
                system_prompt=VISION_SYSTEM_PROMPT,
            )
        )

    def describe_frame(self, frame_path: Path) -> str:
        outcome = self.describe_frames([frame_path])[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def describe_frames(self, frame_paths: list[Path]) -> list[str | Exception]:
        """Describe frames concurrently; each slot is the text or the exception for that frame."""
        return asyncio.run(self._describe_all(frame_paths))

    async def _describe_all(self, frame_paths: list[Path]) -> list[str | Exception]:
        semaphore = asyncio.Semaphore(self.concurrency)
        stop_reason: list[str] = []
        consecutive_failures = 0

        async def describe(chat_client: Any, frame_path: Path) -> str | Exception:
            nonlocal consecutive_failures
            async with semaphore:
                if stop_reason:
                    return VisionUnavailableError(f"vision stopped: {stop_reason[0]}")
                data_url = "data:image/jpeg;base64," + base64.b64encode(frame_path.read_bytes()).decode("ascii")
                try:
                    text = await chat_client.ask(VISION_USER_PROMPT, image_data_urls=[data_url])
                except GatewayChatError as exc:
                    consecutive_failures += 1
                    if not exc.retryable:
                        stop_reason.append(f"permanent gateway error {exc.code}: {exc}")
                        return VisionUnavailableError(stop_reason[0])
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        stop_reason.append(f"{consecutive_failures} consecutive frames failed; last error {exc.code}")
                    return exc
                consecutive_failures = 0
                return text.strip()

        async with self._chat_client_factory() as chat_client:
            outcomes = await asyncio.gather(*(describe(chat_client, path) for path in frame_paths))
        if stop_reason:
            self.logger.error("gateway vision stopped early: %s", stop_reason[0])
        return list(outcomes)
