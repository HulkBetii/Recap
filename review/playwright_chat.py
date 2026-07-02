from __future__ import annotations

import asyncio
import logging
from pathlib import Path

PROMPT_INPUT_SEL = "#prompt-textarea"
SEND_BUTTON_SELS = (
    'button[data-testid="send-button"]',
    'button[data-testid="composer-send-button"]',
    'button[aria-label*="Send"]',
)
ASSISTANT_MSG_SEL = '[data-message-author-role="assistant"]'
STOP_BUTTON_SEL = 'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
DEFAULT_REPLY_TIMEOUT_S = 240
POLL_INTERVAL_S = 2
TEXT_STABLE_SAMPLES = 3
TEXT_STABLE_INTERVAL_S = 2


class PlaywrightChatError(RuntimeError):
    pass


def clear_chrome_singleton_locks(profile_dir: Path) -> None:
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / lock_name).unlink()
        except FileNotFoundError:
            pass


async def _click_send(page) -> None:
    for selector in SEND_BUTTON_SELS:
        try:
            button = page.locator(selector).first
            if await button.is_visible(timeout=2_000) and await button.is_enabled():
                await button.click()
                return
        except Exception:
            continue
    await page.locator(PROMPT_INPUT_SEL).first.press("Enter")


async def _wait_streaming_done(page, timeout_s: int) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await page.locator(STOP_BUTTON_SEL).count() == 0:
                return
        except Exception:
            return
        await asyncio.sleep(POLL_INTERVAL_S)
    raise PlaywrightChatError(f"ChatGPT response still streaming after {timeout_s}s")


async def _get_last_assistant_text(page) -> str:
    locator = page.locator(ASSISTANT_MSG_SEL)
    count = await locator.count()
    if count == 0:
        raise PlaywrightChatError("No assistant response found. Make sure ChatGPT profile is logged in.")
    text = await locator.nth(count - 1).evaluate("(node) => node.innerText || node.textContent || ''", timeout=10_000)
    cleaned = str(text or "").strip()
    if not cleaned:
        raise PlaywrightChatError("Assistant response was empty")
    return cleaned


async def _wait_text_stable(page) -> str:
    stable_count = 0
    previous = ""
    latest = ""
    while stable_count < TEXT_STABLE_SAMPLES:
        latest = await _get_last_assistant_text(page)
        if latest == previous and latest:
            stable_count += 1
        else:
            stable_count = 0
            previous = latest
        await asyncio.sleep(TEXT_STABLE_INTERVAL_S)
    return latest


class PlaywrightChatClient:
    def __init__(self, profile_dir: Path, *, headless: bool = False, timeout_s: int = DEFAULT_REPLY_TIMEOUT_S) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout_s = timeout_s
        self._playwright = None
        self._context = None
        self._page = None
        self.logger = logging.getLogger("review.playwright")

    async def __aenter__(self) -> "PlaywrightChatClient":
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        clear_chrome_singleton_locks(self.profile_dir)
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self._page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        try:
            await self._page.locator(PROMPT_INPUT_SEL).first.wait_for(timeout=15_000)
        except Exception as exc:
            raise PlaywrightChatError(
                "ChatGPT prompt box was not found. Open the same profile manually and log in before running GĐ2."
            ) from exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def ask(self, prompt: str) -> str:
        if self._page is None:
            raise PlaywrightChatError("PlaywrightChatClient is not started")
        box = self._page.locator(PROMPT_INPUT_SEL).first
        await box.click()
        await box.fill(prompt)
        await _click_send(self._page)
        await _wait_streaming_done(self._page, self.timeout_s)
        return await _wait_text_stable(self._page)
