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


def test_wait_streaming_done_waits_for_a_new_assistant_message(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    asyncio.run(playwright_chat._wait_streaming_done(_FakePage(), 5, previous_assistant_count=2))


def test_wait_text_stable_returns_the_latest_complete_text(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(playwright_chat.asyncio, "sleep", no_sleep)
    result = asyncio.run(playwright_chat._wait_text_stable(_FakeTextPage(), timeout_s=5))

    assert result == "final"
