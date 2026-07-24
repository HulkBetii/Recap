from __future__ import annotations

import asyncio
import hashlib
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
STOP_BUTTON_SEL = 'button[data-testid="stop-button"], button[aria-label="Stop generating"]'
ANSWER_NOW_SELS = (
    'button:has-text("Answer now")',
    'a:has-text("Answer now")',
    '[role="button"]:has-text("Answer now")',
)
DEFAULT_REPLY_TIMEOUT_S = 600
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RECOVERY_TIMEOUT_S = 60
POLL_INTERVAL_S = 2
TEXT_STABLE_SAMPLES = 3
TEXT_STABLE_INTERVAL_S = 2
TEXT_STABLE_TIMEOUT_S = 60
PROMPT_INPUT_TIMEOUT_S = 120
PROMPT_VERIFY_TIMEOUT_S = 30
PROMPT_CLEAR_VERIFY_TIMEOUT_S = 5
PROMPT_INSERT_CHUNK_CHARS = 2_000
SEND_BUTTON_ENABLE_TIMEOUT_S = 30
ANSWER_NOW_DELAY_S = 300
ATTACHMENT_REMOVE_SELS = (
    'button[aria-label^="Remove file "]',
    'button[aria-label*="Remove attachment"]',
)
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
    deadline = asyncio.get_event_loop().time() + SEND_BUTTON_ENABLE_TIMEOUT_S
    while asyncio.get_event_loop().time() < deadline:
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
        await asyncio.sleep(0.5)
    raise PlaywrightChatError(
        "ChatGPT send button did not become enabled after prompt preparation",
        code="send_unavailable",
        retryable=True,
        fallback_eligible=True,
    )

def _normalize_prompt_text(text: str) -> str:
    cleaned = (
        text.replace("\r\n", "\n")
        .replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )
    return " ".join(cleaned.split())

async def _read_prompt_text(box) -> str:  # type: ignore[no-untyped-def]
    value = await box.evaluate(
        """
        (node) => {
          if (typeof node.value === 'string') return node.value;
          return node.innerText || node.textContent || '';
        }
        """,
        timeout=10_000,
    )
    return str(value or "")

def _prompt_text_matches(actual: str, expected: str) -> bool:
    return _normalize_prompt_text(actual) == _normalize_prompt_text(expected)


def _prompt_digest(text: str) -> str:
    return hashlib.sha256(_normalize_prompt_text(text).encode("utf-8")).hexdigest()[:16]


async def _wait_prompt_text_loaded(box, prompt: str, timeout_s: int = PROMPT_VERIFY_TIMEOUT_S) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            if _prompt_text_matches(await _read_prompt_text(box), prompt):
                return True
        except PlaywrightError:
            pass
        await asyncio.sleep(0.25)
    return False

async def _insert_prompt_text(page, prompt: str) -> None:  # type: ignore[no-untyped-def]
    for offset in range(0, len(prompt), PROMPT_INSERT_CHUNK_CHARS):
        chunk = prompt[offset : offset + PROMPT_INSERT_CHUNK_CHARS]
        await asyncio.wait_for(page.keyboard.insert_text(chunk), timeout=PROMPT_INPUT_TIMEOUT_S)


async def _remove_prompt_attachments(page) -> None:  # type: ignore[no-untyped-def]
    for selector in ATTACHMENT_REMOVE_SELS:
        try:
            buttons = page.locator(selector)
            count = await buttons.count()
        except PlaywrightError:
            continue
        for index in range(count - 1, -1, -1):
            try:
                await buttons.nth(index).click(timeout=10_000)
            except PlaywrightError as exc:
                raise PlaywrightChatError(
                    f"Could not remove pending ChatGPT attachment: {exc}",
                    code="prompt_attachment_clear_failed",
                    retryable=True,
                    fallback_eligible=True,
                ) from exc
        try:
            remaining = await buttons.count()
        except PlaywrightError:
            remaining = 0
        if remaining:
            raise PlaywrightChatError(
                "ChatGPT composer still contains an attachment after cleanup",
                code="prompt_attachment_clear_failed",
                retryable=True,
                fallback_eligible=True,
            )

async def _clear_prompt_text(page, box) -> None:  # type: ignore[no-untyped-def]
    await _remove_prompt_attachments(page)
    try:
        await asyncio.wait_for(box.click(), timeout=15)
    except PlaywrightError:
        pass
    try:
        await asyncio.wait_for(box.fill(""), timeout=15)
    except PlaywrightError:
        pass
    if await _wait_prompt_text_loaded(box, "", timeout_s=PROMPT_CLEAR_VERIFY_TIMEOUT_S):
        await _remove_prompt_attachments(page)
        return
    try:
        await asyncio.wait_for(page.keyboard.press("Control+A"), timeout=15)
        await asyncio.wait_for(page.keyboard.press("Backspace"), timeout=15)
    except (AttributeError, PlaywrightError):
        pass
    if await _wait_prompt_text_loaded(box, "", timeout_s=PROMPT_CLEAR_VERIFY_TIMEOUT_S):
        await _remove_prompt_attachments(page)
        return
    raise PlaywrightChatError(
        "ChatGPT prompt composer could not be cleared before inserting a new prompt",
        code="prompt_clear_failed",
        retryable=True,
        fallback_eligible=True,
    )

async def _set_prompt_text(  # type: ignore[no-untyped-def]
    page,
    box,
    prompt: str,
    logger: logging.Logger | None = None,
) -> None:
    try:
        await _clear_prompt_text(page, box)
        try:
            await asyncio.wait_for(box.fill(prompt), timeout=PROMPT_INPUT_TIMEOUT_S)
            if await _wait_prompt_text_loaded(box, prompt):
                return
            raise PlaywrightError("ChatGPT prompt fill did not verify")
        except (asyncio.TimeoutError, PlaywrightError) as exc:
            if logger:
                logger.warning("Direct ChatGPT prompt fill failed; falling back to chunked insert_text: %s", exc)
            await _clear_prompt_text(page, box)
            await asyncio.wait_for(box.click(), timeout=15)
            await _insert_prompt_text(page, prompt)
            if await _wait_prompt_text_loaded(box, prompt):
                return
            actual = await _read_prompt_text(box)
            raise PlaywrightChatError(
                "ChatGPT prompt verification failed after all input methods "
                f"(expected_chars={len(_normalize_prompt_text(prompt))}, "
                f"actual_chars={len(_normalize_prompt_text(actual))}, "
                f"expected_sha256={_prompt_digest(prompt)}, actual_sha256={_prompt_digest(actual)})",
                code="prompt_verify_failed",
                retryable=True,
                fallback_eligible=True,
            )
    except asyncio.TimeoutError as exc:
        raise PlaywrightChatError(
            f"Timed out while preparing ChatGPT prompt after {PROMPT_INPUT_TIMEOUT_S}s",
            code="prompt_prepare_timeout",
            retryable=True,
            fallback_eligible=True,
        ) from exc


async def _click_answer_now_if_visible(page) -> bool:  # type: ignore[no-untyped-def]
    for selector in ANSWER_NOW_SELS:
        try:
            button = page.locator(selector).first
            visible = await button.is_visible(timeout=1_000)
            enabled = visible and await button.is_enabled()
            if visible and enabled:
                await button.click()
                return True
        except PlaywrightError:
            continue
    try:
        button = page.get_by_text("Answer now", exact=True).first
        visible = await button.is_visible(timeout=1_000)
        enabled = visible and await button.is_enabled()
        if visible and enabled:
            await button.click()
            return True
    except (AttributeError, PlaywrightError):
        pass
    return False


async def _wait_streaming_done(page, timeout_s: int, previous_assistant_count: int) -> None:
    started_at = asyncio.get_event_loop().time()
    deadline = started_at + timeout_s
    response_started = False
    answer_now_clicked = False
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
        if (
            not response_started
            and not answer_now_clicked
            and asyncio.get_event_loop().time() - started_at >= ANSWER_NOW_DELAY_S
            and await _click_answer_now_if_visible(page)
        ):
            logging.getLogger("review.playwright").info(
                "Clicked ChatGPT Answer now after %ds without an assistant response",
                ANSWER_NOW_DELAY_S,
            )
            answer_now_clicked = True
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
        previous_assistant_count = 0
        prepared = False
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempt_count = attempt
            try:
                previous_assistant_count = await self._page.locator(ASSISTANT_MSG_SEL).count()
                box = self._page.locator(PROMPT_INPUT_SEL).first
                await box.click()
                self.logger.info("Preparing ChatGPT prompt (%d chars)", len(prompt))
                await _set_prompt_text(self._page, box, prompt, self.logger)
                prepared = True
                break
            except PlaywrightChatError as exc:
                self.last_error_code = exc.code
                if not exc.retryable or attempt >= self.max_attempts:
                    raise _terminal_error(exc, attempt) from exc
                self.logger.warning(
                    "ChatGPT prompt preparation attempt %d/%d failed; retrying before dispatch: %s",
                    attempt,
                    self.max_attempts,
                    exc,
                )
                await asyncio.sleep(2 ** (attempt - 1))
                continue
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
            self.logger.info("Submitting ChatGPT prompt")
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
                self.logger.info("Waiting for ChatGPT response attempt %d/%d", attempt, self.max_attempts)
                await _wait_streaming_done(self._page, timeout_s, previous_assistant_count)
                response = await _wait_text_stable(self._page)
                self.logger.info("ChatGPT response received (%d chars)", len(response))
                return response
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
