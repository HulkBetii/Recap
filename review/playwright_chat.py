from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

PROMPT_INPUT_SEL = "#prompt-textarea"
SEND_BUTTON_SELS = (
    'button[data-testid="send-button"]',
    'button[data-testid="composer-send-button"]',
    'button[aria-label*="Send"]',
)
ASSISTANT_MSG_SEL = '[data-message-author-role="assistant"]'
MESSAGE_SEL = "[data-message-author-role]"
STOP_BUTTON_SEL = 'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
DEFAULT_REPLY_TIMEOUT_S = 600
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RECOVERY_TIMEOUT_S = 60
POLL_INTERVAL_S = 2
TEXT_STABLE_SAMPLES = 3
TEXT_STABLE_INTERVAL_S = 2
TEXT_STABLE_TIMEOUT_S = 60
HISTORY_STABLE_SAMPLES = 3
HISTORY_STABLE_INTERVAL_S = 0.5
HISTORY_LOAD_TIMEOUT_S = 15
DISCONNECT_ERROR_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "page closed",
    "context closed",
    "connection closed",
)


class PlaywrightChatError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        fallback_eligible: bool,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.fallback_eligible = fallback_eligible
        self.attempts = attempts


def _terminal_error(error: PlaywrightChatError, attempts: int) -> PlaywrightChatError:
    return PlaywrightChatError(
        str(error),
        code=error.code,
        retryable=error.retryable,
        fallback_eligible=error.fallback_eligible,
        attempts=attempts,
    )


def _is_page_disconnected(page, error: Exception) -> bool:  # type: ignore[no-untyped-def]
    try:
        if page.is_closed():
            return True
    except Exception:
        pass
    message = str(error).lower()
    return any(marker in message for marker in DISCONNECT_ERROR_MARKERS)


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
        except PlaywrightError:
            continue
        try:
            visible = await button.is_visible(timeout=2_000)
            enabled = visible and await button.is_enabled()
        except PlaywrightError:
            continue
        if enabled:
            await button.click()
            return
    await page.locator(PROMPT_INPUT_SEL).first.press("Enter")


async def _wait_streaming_done(page, timeout_s: int, previous_assistant_count: int) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    response_started = False
    while asyncio.get_event_loop().time() < deadline:
        try:
            assistant_count = await page.locator(ASSISTANT_MSG_SEL).count()
            response_started = response_started or assistant_count > previous_assistant_count
            if response_started and await page.locator(STOP_BUTTON_SEL).count() == 0:
                return
        except PlaywrightError as exc:
            if _is_page_disconnected(page, exc):
                raise PlaywrightChatError(
                    f"ChatGPT page or browser disconnected: {exc}",
                    code="page_disconnected",
                    retryable=True,
                    fallback_eligible=True,
                ) from exc
        await asyncio.sleep(POLL_INTERVAL_S)
    if not response_started:
        raise PlaywrightChatError(
            f"No new assistant response appeared after {timeout_s}s",
            code="response_not_started",
            retryable=True,
            fallback_eligible=True,
        )
    raise PlaywrightChatError(
        f"ChatGPT response still streaming after {timeout_s}s",
        code="response_stream_timeout",
        retryable=True,
        fallback_eligible=True,
    )


async def _wait_conversation_history_stable(page, timeout_s: float = HISTORY_LOAD_TIMEOUT_S) -> None:  # type: ignore[no-untyped-def]
    if "/c/" not in str(page.url):
        return
    deadline = asyncio.get_event_loop().time() + timeout_s
    previous_count = -1
    stable_count = 0
    while asyncio.get_event_loop().time() < deadline:
        count = await page.locator('[data-message-author-role]').count()
        if count > 0 and count == previous_count:
            stable_count += 1
            if stable_count >= HISTORY_STABLE_SAMPLES:
                return
        else:
            previous_count = count
            stable_count = 0
        await asyncio.sleep(HISTORY_STABLE_INTERVAL_S)


async def _get_last_assistant_text(page) -> str:
    locator = page.locator(ASSISTANT_MSG_SEL)
    count = await locator.count()
    if count == 0:
        raise PlaywrightChatError(
            "No assistant response found after prompt submission",
            code="assistant_response_missing",
            retryable=True,
            fallback_eligible=True,
        )
    text = await locator.nth(count - 1).evaluate("(node) => node.innerText || node.textContent || ''", timeout=10_000)
    cleaned = str(text or "").strip()
    if not cleaned:
        raise PlaywrightChatError(
            "Assistant response was empty",
            code="assistant_response_empty",
            retryable=True,
            fallback_eligible=True,
        )
    return cleaned


async def _wait_text_stable(page, timeout_s: int = TEXT_STABLE_TIMEOUT_S) -> str:
    stable_count = 0
    previous = ""
    latest = ""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while stable_count < TEXT_STABLE_SAMPLES and asyncio.get_event_loop().time() < deadline:
        latest = await _get_last_assistant_text(page)
        if latest == previous and latest:
            stable_count += 1
        else:
            stable_count = 0
            previous = latest
        await asyncio.sleep(TEXT_STABLE_INTERVAL_S)
    if not latest:
        raise PlaywrightChatError(
            f"Assistant response did not become readable after {timeout_s}s",
            code="assistant_response_unreadable",
            retryable=True,
            fallback_eligible=True,
        )
    return latest


async def _wait_existing_response_done(page, timeout_s: int) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await page.locator(STOP_BUTTON_SEL).count() == 0:
                return
        except PlaywrightError as exc:
            if _is_page_disconnected(page, exc):
                raise PlaywrightChatError(
                    f"ChatGPT page or browser disconnected: {exc}",
                    code="page_disconnected",
                    retryable=True,
                    fallback_eligible=True,
                ) from exc
        await asyncio.sleep(POLL_INTERVAL_S)
    raise PlaywrightChatError(
        f"ChatGPT response still streaming after {timeout_s}s",
        code="response_stream_timeout",
        retryable=True,
        fallback_eligible=True,
    )


async def _recover_matching_prompt(page, prompt: str, timeout_s: int) -> str | None:  # type: ignore[no-untyped-def]
    """Reuse a response already present in the conversation without resubmitting its prompt."""
    def normalized_message_text(value: object) -> str:
        lines = str(value or "").strip().splitlines()
        while lines and lines[-1].strip() in {"Show more", "Show less", "Edit"}:
            lines.pop()
        return "\n".join(lines).strip()

    messages = page.locator(MESSAGE_SEL)
    count = await messages.count()
    expected = normalized_message_text(prompt)
    for index in range(count - 1, -1, -1):
        item = messages.nth(index)
        if await item.get_attribute("data-message-author-role") != "user":
            continue
        text = await item.evaluate("(node) => node.innerText || node.textContent || ''", timeout=10_000)
        if normalized_message_text(text) != expected:
            continue
        if index + 1 < count:
            response = messages.nth(index + 1)
            if await response.get_attribute("data-message-author-role") == "assistant":
                value = await response.evaluate(
                    "(node) => node.innerText || node.textContent || ''",
                    timeout=10_000,
                )
                cleaned = str(value or "").strip()
                if cleaned:
                    is_latest_message = index + 1 == count - 1
                    if is_latest_message and await page.locator(STOP_BUTTON_SEL).count() > 0:
                        await _wait_existing_response_done(page, timeout_s)
                        return await _wait_text_stable(page)
                    return cleaned
        previous_assistant_count = await page.locator(ASSISTANT_MSG_SEL).count()
        await _wait_streaming_done(page, timeout_s, previous_assistant_count)
        return await _wait_text_stable(page)
    return None


class PlaywrightChatClient:
    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        timeout_s: int = DEFAULT_REPLY_TIMEOUT_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        recovery_timeout_s: int = DEFAULT_RECOVERY_TIMEOUT_S,
        initial_url: str = "https://chatgpt.com/",
        session_file: Path | None = None,
        resume_matching_prompts: bool = False,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("Playwright max_attempts must be at least 1")
        if recovery_timeout_s < 1:
            raise ValueError("Playwright recovery_timeout_s must be at least 1")
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.recovery_timeout_s = recovery_timeout_s
        self.initial_url = initial_url
        self.session_file = session_file
        self.resume_matching_prompts = resume_matching_prompts
        self.last_attempt_count = 0
        self.last_error_code: str | None = None
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
        if self.session_file and self.session_file.exists():
            try:
                payload = json.loads(self.session_file.read_text(encoding="utf-8"))
                cookies = payload.get("cookies", []) if isinstance(payload, dict) else payload
                if cookies:
                    await self._context.add_cookies(cookies)
            except Exception as exc:
                self.logger.warning("Could not restore ChatGPT session cookies from %s: %s", self.session_file, exc)
        await self._page.goto(self.initial_url, wait_until="domcontentloaded")
        try:
            await self._page.locator(PROMPT_INPUT_SEL).first.wait_for(timeout=15_000)
        except Exception as exc:
            raise PlaywrightChatError(
                "ChatGPT prompt box was not found. Open the same profile manually and log in before running GĐ2.",
                code="login_required",
                retryable=False,
                fallback_eligible=False,
            ) from exc
        await _wait_conversation_history_stable(self._page)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()

    @property
    def current_url(self) -> str:
        if self._page is None:
            return self.initial_url
        return str(self._page.url)

    async def ask(self, prompt: str) -> str:
        if self._page is None:
            raise PlaywrightChatError(
                "PlaywrightChatClient is not started",
                code="client_not_started",
                retryable=False,
                fallback_eligible=False,
            )

        self.last_attempt_count = 0
        self.last_error_code = None
        if self.resume_matching_prompts:
            recovered = await _recover_matching_prompt(
                self._page,
                prompt,
                self.recovery_timeout_s,
            )
            if recovered is not None:
                self.last_attempt_count = 1
                return recovered
        previous_assistant_count = 0
        prepared = False
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempt_count = attempt
            try:
                previous_assistant_count = await self._page.locator(ASSISTANT_MSG_SEL).count()
                box = self._page.locator(PROMPT_INPUT_SEL).first
                await box.click()
                await box.fill(prompt)
                prepared = True
                break
            except PlaywrightError as exc:
                if _is_page_disconnected(self._page, exc):
                    error = PlaywrightChatError(
                        f"ChatGPT page or browser disconnected before prompt submission: {exc}",
                        code="page_disconnected",
                        retryable=True,
                        fallback_eligible=True,
                        attempts=attempt,
                    )
                    self.last_error_code = error.code
                    if attempt >= self.max_attempts:
                        raise error from exc
                    self.logger.warning(
                        "ChatGPT browser preparation attempt %d/%d disconnected; retrying before fallback: %s",
                        attempt,
                        self.max_attempts,
                        exc,
                    )
                    await asyncio.sleep(2 ** (attempt - 1))
                    continue
                try:
                    prompt_available = await self._page.locator(PROMPT_INPUT_SEL).first.is_visible(timeout=2_000)
                except PlaywrightError:
                    prompt_available = False
                if not prompt_available:
                    raise PlaywrightChatError(
                        "ChatGPT prompt box is unavailable; the profile may be logged out or the session may be invalid",
                        code="login_required",
                        retryable=False,
                        fallback_eligible=False,
                        attempts=attempt,
                    ) from exc
                error = PlaywrightChatError(
                    f"Could not prepare ChatGPT prompt: {exc}",
                    code="prompt_prepare_failed",
                    retryable=True,
                    fallback_eligible=True,
                    attempts=attempt,
                )
                self.last_error_code = error.code
                if attempt >= self.max_attempts:
                    raise error from exc
                self.logger.warning(
                    "ChatGPT prompt preparation attempt %d/%d failed; retrying before dispatch: %s",
                    attempt,
                    self.max_attempts,
                    exc,
                )
                await asyncio.sleep(2 ** (attempt - 1))

        if not prepared:  # pragma: no cover - guarded by the loop above
            raise PlaywrightChatError(
                "ChatGPT prompt was not prepared",
                code="prompt_prepare_failed",
                retryable=True,
                fallback_eligible=True,
                attempts=self.last_attempt_count,
            )

        try:
            await _click_send(self._page)
        except PlaywrightError as exc:
            # Dispatch may have succeeded before the browser reported a detached/closed element.
            # Never send again; response recovery below determines whether ChatGPT accepted it.
            self.logger.warning(
                "ChatGPT prompt dispatch acknowledgement failed; recovering without resending: %s",
                exc,
            )

        for attempt in range(1, self.max_attempts + 1):
            self.last_attempt_count = attempt
            timeout_s = self.timeout_s if attempt == 1 else self.recovery_timeout_s
            try:
                await _wait_streaming_done(self._page, timeout_s, previous_assistant_count)
                return await _wait_text_stable(self._page)
            except PlaywrightChatError as exc:
                self.last_error_code = exc.code
                if exc.code == "response_not_started":
                    try:
                        prompt_available = await self._page.locator(PROMPT_INPUT_SEL).first.is_visible(timeout=2_000)
                    except PlaywrightError as state_error:
                        if _is_page_disconnected(self._page, state_error):
                            exc = PlaywrightChatError(
                                f"ChatGPT page or browser disconnected while waiting for a response: {state_error}",
                                code="page_disconnected",
                                retryable=True,
                                fallback_eligible=True,
                            )
                        else:
                            prompt_available = False
                    if exc.code == "response_not_started" and not prompt_available:
                        raise PlaywrightChatError(
                            "ChatGPT prompt box became unavailable while waiting for a response; the session may have expired",
                            code="login_required",
                            retryable=False,
                            fallback_eligible=False,
                            attempts=attempt,
                        ) from exc
                self.last_error_code = exc.code
                if not exc.retryable or attempt >= self.max_attempts:
                    raise _terminal_error(exc, attempt) from exc
                self.logger.warning(
                    "ChatGPT response attempt %d/%d failed; recovering the same response for %ds: %s",
                    attempt,
                    self.max_attempts,
                    self.recovery_timeout_s,
                    exc,
                )
                await asyncio.sleep(2 ** (attempt - 1))
            except PlaywrightError as exc:
                error = PlaywrightChatError(
                    f"Transient browser error while reading ChatGPT response: {exc}",
                    code="browser_response_error",
                    retryable=True,
                    fallback_eligible=True,
                    attempts=attempt,
                )
                self.last_error_code = error.code
                if attempt >= self.max_attempts:
                    raise error from exc
                self.logger.warning(
                    "ChatGPT browser response attempt %d/%d failed; recovering without resending: %s",
                    attempt,
                    self.max_attempts,
                    exc,
                )
                await asyncio.sleep(2 ** (attempt - 1))

        raise AssertionError("unreachable")
