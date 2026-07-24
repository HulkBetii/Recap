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
        self.answer_now = _AnswerNowLocator(visible=False)

    def locator(self, selector: str):  # type: ignore[no-untyped-def]
        if selector == playwright_chat.ASSISTANT_MSG_SEL:
            return self.assistant
        if selector == playwright_chat.STOP_BUTTON_SEL:
            return self.stop
        if selector in playwright_chat.ANSWER_NOW_SELS:
            return self.answer_now
        raise AssertionError(selector)


class _AnswerNowLocator:
    def __init__(self, *, visible: bool) -> None:
        self.visible = visible
        self.clicks = 0

    @property
    def first(self) -> "_AnswerNowLocator":
        return self

    async def is_visible(self, timeout: int) -> bool:
        assert timeout == 1_000
        return self.visible

    async def is_enabled(self) -> bool:
        return self.visible

    async def click(self) -> None:
        self.clicks += 1


class _AnswerNowPage(_FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.assistant = _CountLocator([2, 3])
        self.stop = _CountLocator([0])
        self.answer_now = _AnswerNowLocator(visible=True)


class _AnswerNowTextPage(_FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.answer_now_text = _AnswerNowLocator(visible=True)

    def get_by_text(self, text: str, *, exact: bool) -> _AnswerNowLocator:
        assert text == "Answer now"
        assert exact is True
        return self.answer_now_text


class _FakeTextPage:
    def __init__(self) -> None:
        self.assistant = _TextLocator(["draft", "final", "final", "final", "final"])

    def locator(self, selector: str) -> _TextLocator:
        assert selector == playwright_chat.ASSISTANT_MSG_SEL
        return self.assistant


class _PromptBox:
    def __init__(self) -> None:
        self.fills: list[str] = []
        self.text = ""

    @property
    def first(self) -> "_PromptBox":
        return self

    async def click(self) -> None:
        return None

    async def fill(self, prompt: str) -> None:
        self.fills.append(prompt)
        self.text = prompt
        return None

    async def is_visible(self, timeout: int) -> bool:
        assert timeout == 2_000
        return True

    async def evaluate(self, _expression: str, arg=None, timeout: int = 10_000) -> str | None:
        assert timeout == 10_000
        if arg is not None:
            self.text = str(arg)
            return None
        return self.text


class _EmptyLocator:
    async def count(self) -> int:
        return 0


class _AskPage:
    def __init__(self) -> None:
        self.assistant = _CountLocator([2])
        self.prompt = _PromptBox()

    def locator(self, selector: str):  # type: ignore[no-untyped-def]
        if selector == playwright_chat.ASSISTANT_MSG_SEL:
            return self.assistant
        if selector == playwright_chat.PROMPT_INPUT_SEL:
            return self.prompt
        if selector in playwright_chat.ATTACHMENT_REMOVE_SELS:
            return _EmptyLocator()
        raise AssertionError(selector)

class _Keyboard:
    def __init__(self, page: "_LongPromptPage") -> None:
        self.page = page
        self.presses: list[str] = []

    async def press(self, key: str) -> None:
        self.presses.append(key)
        if key == "Backspace":
            self.page.prompt.text = ""

    async def insert_text(self, text: str) -> None:
        self.page.prompt.text += text

class _LongPromptPage(_AskPage):
    def __init__(self) -> None:
        super().__init__()
        self.keyboard = _Keyboard(self)


class _AttachmentButton:
    def __init__(self, page: "_AttachmentPage", index: int) -> None:
        self.page = page
        self.index = index

    async def click(self, timeout: int) -> None:
        assert timeout == 10_000
        self.page.attachments.pop(self.index)


class _AttachmentLocator:
    def __init__(self, page: "_AttachmentPage") -> None:
        self.page = page

    async def count(self) -> int:
        return len(self.page.attachments)

    def nth(self, index: int) -> _AttachmentButton:
        return _AttachmentButton(self.page, index)


class _AttachmentPage(_AskPage):
    def __init__(self) -> None:
        super().__init__()
        self.attachments = ["Pasted text.txt"]

    def locator(self, selector: str):  # type: ignore[no-untyped-def]
        if selector in playwright_chat.ATTACHMENT_REMOVE_SELS:
            return _AttachmentLocator(self)
        return super().locator(selector)


class _TruncatingPromptBox(_PromptBox):
    async def fill(self, prompt: str) -> None:
        self.fills.append(prompt)
        self.text = prompt[:-16] if prompt else ""


class _TruncatingKeyboard(_Keyboard):
    async def insert_text(self, text: str) -> None:
        self.page.prompt.text += text.replace("END_MARKER", "")


class _TruncatingPage(_LongPromptPage):
    def __init__(self) -> None:
        super().__init__()
        self.prompt = _TruncatingPromptBox()
        self.keyboard = _TruncatingKeyboard(self)


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


def test_wait_streaming_clicks_answer_now_once_for_extended_reasoning(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat, "ANSWER_NOW_DELAY_S", 0)
    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    page = _AnswerNowPage()

    asyncio.run(playwright_chat._wait_streaming_done(page, 5, previous_assistant_count=2))

    assert page.answer_now.clicks == 1


def test_answer_now_falls_back_to_exact_text_locator() -> None:
    page = _AnswerNowTextPage()

    clicked = asyncio.run(playwright_chat._click_answer_now_if_visible(page))

    assert clicked is True
    assert page.answer_now_text.clicks == 1


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


def test_prompt_text_match_rejects_duplicated_prompt() -> None:
    prompt = "ARC_EVENT_BANK: " + ("event_id s01e01 section " * 200) + "GLOBAL_HOOK_CANDIDATES"

    assert not playwright_chat._prompt_text_matches(prompt + prompt, prompt)
    assert playwright_chat._prompt_text_matches(prompt, prompt)


def test_prompt_text_match_normalizes_prosemirror_blank_lines_but_requires_complete_suffix() -> None:
    prompt = "BEGIN_MARKER\nARC_EVENT_BANK\nGLOBAL_HOOK_CANDIDATES_COMPLETE"
    prosemirror_text = "BEGIN_MARKER\n\nARC_EVENT_BANK\n\nGLOBAL_HOOK_CANDIDATES_COMPLETE"

    assert playwright_chat._prompt_text_matches(prosemirror_text, prompt)
    assert not playwright_chat._prompt_text_matches(prosemirror_text.removesuffix("_COMPLETE"), prompt)


def test_clear_prompt_removes_pending_attachment() -> None:
    page = _AttachmentPage()

    asyncio.run(playwright_chat._clear_prompt_text(page, page.prompt))

    assert page.attachments == []


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

def test_ask_uses_verified_fill_for_multiline_long_prompt(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def fake_send(_page) -> None:  # type: ignore[no-untyped-def]
        return None

    async def fake_wait(_page, _timeout_s: int, _previous_count: int) -> None:  # type: ignore[no-untyped-def]
        return None

    async def fake_text(_page) -> str:  # type: ignore[no-untyped-def]
        return "final response"

    monkeypatch.setattr(playwright_chat, "_click_send", fake_send)
    monkeypatch.setattr(playwright_chat, "_wait_streaming_done", fake_wait)
    monkeypatch.setattr(playwright_chat, "_wait_text_stable", fake_text)
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile")
    page = _LongPromptPage()
    client._page = page
    suffix = "\nEND_MARKER_GLOBAL_HOOK_CANDIDATES_COMPLETE"
    body = "ARC_EVENT_BANK event_id s01e01 chronology Vietnamese narration constraints.\n" * 400
    prompt = ("BEGIN_MARKER\n" + body)[: 12_860 - len(suffix)] + suffix

    result = asyncio.run(client.ask(prompt))

    assert result == "final response"
    assert page.prompt.fills == ["", prompt]
    assert "Control+V" not in page.keyboard.presses


def test_prompt_verification_failure_never_submits(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    sends = 0

    async def fake_send(_page) -> None:  # type: ignore[no-untyped-def]
        nonlocal sends
        sends += 1

    async def immediate_verification(box, prompt: str, timeout_s: int = 30) -> bool:  # type: ignore[no-untyped-def]
        del timeout_s
        return playwright_chat._prompt_text_matches(await playwright_chat._read_prompt_text(box), prompt)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat, "_click_send", fake_send)
    monkeypatch.setattr(playwright_chat, "_wait_prompt_text_loaded", immediate_verification)
    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    client = playwright_chat.PlaywrightChatClient(tmp_path / "profile", max_attempts=2)
    client._page = _TruncatingPage()

    try:
        asyncio.run(client.ask("BEGIN_MARKER\nARC_EVENT_BANK\nEND_MARKER"))
    except playwright_chat.PlaywrightChatError as exc:
        assert exc.code == "prompt_verify_failed"
        assert exc.attempts == 2
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected PlaywrightChatError")
    assert sends == 0


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
