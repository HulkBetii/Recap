from __future__ import annotations

import asyncio

import review.playwright_chat as playwright_chat


class _CountLocator:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.index = 0

    async def count(self) -> int:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


class _HistoryPage:
    url = "https://chatgpt.com/c/existing-conversation"

    def __init__(self) -> None:
        self.messages = _CountLocator([0, 2, 6, 6, 6, 6])

    def locator(self, selector: str) -> _CountLocator:
        assert selector == '[data-message-author-role]'
        return self.messages


class _TextLocator:
    def __init__(self, values: list[str]) -> None:
        self.values = values
        self.index = 0

    async def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "_TextLocator":
        return self

    async def evaluate(self, _expression: str, timeout: int) -> str:
        assert timeout == 10_000
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


class _FakePage:
    def __init__(self) -> None:
        self.assistant = _CountLocator([2, 2, 3, 3])
        self.stop = _CountLocator([0, 0, 1, 0])

    def locator(self, selector: str) -> _CountLocator:
        if selector == playwright_chat.ASSISTANT_MSG_SEL:
            return self.assistant
        if selector == playwright_chat.STOP_BUTTON_SEL:
            return self.stop
        raise AssertionError(selector)


class _FakeTextPage:
    def __init__(self) -> None:
        self.assistant = _TextLocator(["draft", "final", "final", "final", "final"])

    def locator(self, selector: str) -> _TextLocator:
        assert selector == playwright_chat.ASSISTANT_MSG_SEL
        return self.assistant


class _PromptBox:
    @property
    def first(self) -> "_PromptBox":
        return self

    async def click(self) -> None:
        return None

    async def fill(self, _prompt: str) -> None:
        return None

    async def is_visible(self, timeout: int) -> bool:
        assert timeout == 2_000
        return True


class _AskPage:
    def __init__(self) -> None:
        self.assistant = _CountLocator([2])
        self.prompt = _PromptBox()

    def locator(self, selector: str):  # type: ignore[no-untyped-def]
        if selector == playwright_chat.ASSISTANT_MSG_SEL:
            return self.assistant
        if selector == playwright_chat.PROMPT_INPUT_SEL:
            return self.prompt
        raise AssertionError(selector)


class _DisconnectedPage:
    def __init__(self) -> None:
        self.locator_calls = 0

    def locator(self, _selector: str):  # type: ignore[no-untyped-def]
        self.locator_calls += 1
        raise playwright_chat.PlaywrightError("Target page, context or browser has been closed")

    def is_closed(self) -> bool:
        return True


class _ProgrammingErrorPage:
    def locator(self, _selector: str):  # type: ignore[no-untyped-def]
        raise TypeError("test programming error")


class _UnavailablePromptBox(_PromptBox):
    async def is_visible(self, timeout: int) -> bool:
        assert timeout == 2_000
        return False


class _ExpiredSessionPage(_AskPage):
    def __init__(self) -> None:
        super().__init__()
        self.prompt = _UnavailablePromptBox()


def test_wait_streaming_done_waits_for_a_new_assistant_message(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    asyncio.run(playwright_chat._wait_streaming_done(_FakePage(), 5, previous_assistant_count=2))


def test_wait_conversation_history_stable_before_counting_existing_messages(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    page = _HistoryPage()

    asyncio.run(playwright_chat._wait_conversation_history_stable(page, timeout_s=5))

    assert page.messages.index == 6


def test_wait_text_stable_returns_the_latest_complete_text(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    result = asyncio.run(playwright_chat._wait_text_stable(_FakeTextPage(), timeout_s=5))

    assert result == "final"


def test_ask_recovers_same_response_without_resending(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    waits = 0
    sends = 0

    async def fake_send(_page) -> None:  # type: ignore[no-untyped-def]
        nonlocal sends
        sends += 1

    async def fake_wait(_page, timeout_s: int, _previous_count: int) -> None:  # type: ignore[no-untyped-def]
        nonlocal waits
        waits += 1
        if waits == 1:
            assert timeout_s == 600
            raise playwright_chat.PlaywrightChatError(
                "response timeout",
                code="response_stream_timeout",
                retryable=True,
                fallback_eligible=True,
            )
        assert timeout_s == 60

    async def fake_text(_page) -> str:  # type: ignore[no-untyped-def]
        return "final response"

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat, "_click_send", fake_send)
    monkeypatch.setattr(playwright_chat, "_wait_streaming_done", fake_wait)
    monkeypatch.setattr(playwright_chat, "_wait_text_stable", fake_text)
    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile")
    client._page = _AskPage()

    result = asyncio.run(client.ask("prompt"))

    assert result == "final response"
    assert sends == 1
    assert waits == 2
    assert client.last_attempt_count == 2


def test_dispatch_error_recovers_without_second_send(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    sends = 0

    async def uncertain_send(_page) -> None:  # type: ignore[no-untyped-def]
        nonlocal sends
        sends += 1
        raise playwright_chat.PlaywrightError("element detached after click")

    async def response_arrived(_page, _timeout_s: int, _previous_count: int) -> None:  # type: ignore[no-untyped-def]
        return None

    async def stable_text(_page) -> str:  # type: ignore[no-untyped-def]
        return "accepted response"

    monkeypatch.setattr(playwright_chat, "_click_send", uncertain_send)
    monkeypatch.setattr(playwright_chat, "_wait_streaming_done", response_arrived)
    monkeypatch.setattr(playwright_chat, "_wait_text_stable", stable_text)
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile")
    client._page = _AskPage()

    result = asyncio.run(client.ask("prompt"))

    assert result == "accepted response"
    assert sends == 1


def test_client_not_started_is_not_fallback_eligible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile")

    try:
        asyncio.run(client.ask("prompt"))
    except playwright_chat.PlaywrightChatError as exc:
        assert exc.code == "client_not_started"
        assert exc.fallback_eligible is False
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected PlaywrightChatError")


def test_wait_streaming_classifies_browser_disconnect() -> None:
    try:
        asyncio.run(playwright_chat._wait_streaming_done(_DisconnectedPage(), 5, previous_assistant_count=0))
    except playwright_chat.PlaywrightChatError as exc:
        assert exc.code == "page_disconnected"
        assert exc.retryable is True
        assert exc.fallback_eligible is True
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected PlaywrightChatError")


def test_programming_error_is_not_wrapped_as_fallback_eligible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile")
    client._page = _ProgrammingErrorPage()

    try:
        asyncio.run(client.ask("prompt"))
    except TypeError as exc:
        assert "programming error" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected TypeError")


def test_preparation_disconnect_exhausts_retry_budget(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    page = _DisconnectedPage()
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile", max_attempts=2)
    client._page = page

    try:
        asyncio.run(client.ask("prompt"))
    except playwright_chat.PlaywrightChatError as exc:
        assert exc.code == "page_disconnected"
        assert exc.attempts == 2
        assert exc.fallback_eligible is True
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected PlaywrightChatError")
    assert page.locator_calls == 2


def test_expired_session_after_dispatch_is_not_fallback_eligible(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def fake_send(_page) -> None:  # type: ignore[no-untyped-def]
        return None

    async def no_response(_page, _timeout_s: int, _previous_count: int) -> None:  # type: ignore[no-untyped-def]
        raise playwright_chat.PlaywrightChatError(
            "no response",
            code="response_not_started",
            retryable=True,
            fallback_eligible=True,
        )

    monkeypatch.setattr(playwright_chat, "_click_send", fake_send)
    monkeypatch.setattr(playwright_chat, "_wait_streaming_done", no_response)
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile")
    client._page = _ExpiredSessionPage()

    try:
        asyncio.run(client.ask("prompt"))
    except playwright_chat.PlaywrightChatError as exc:
        assert exc.code == "login_required"
        assert exc.fallback_eligible is False
        assert exc.attempts == 1
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected PlaywrightChatError")
