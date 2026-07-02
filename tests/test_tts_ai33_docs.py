from __future__ import annotations

import asyncio

from tts import providers


def test_submit_ai33_accepts_id_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VIVOO_API_KEY", "test-key")
    monkeypatch.setattr(providers, "http_json", lambda *args, **kwargs: {"id": "task-id"})

    assert providers.submit_ai33("hello", "voice", 1.0) == "task-id"


def test_poll_ai33_treats_doing_as_running(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VIVOO_API_KEY", "test-key")
    responses = iter([
        {"id": "task-id", "status": "doing", "metadata": {}, "progress": 60},
        {"id": "task-id", "status": "done", "metadata": {"audio_url": "https://example.com/a.mp3"}},
    ])
    monkeypatch.setattr(providers, "http_json", lambda *args, **kwargs: next(responses))

    assert asyncio.run(providers.poll_ai33("task-id", timeout_s=5, interval_s=0)) == "https://example.com/a.mp3"


def test_download_file_sends_user_agent_for_cdn(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    captured = {}

    class FakeResponse:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
            return None

        def read(self):  # type: ignore[no-untyped-def]
            return b"mp3"

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "a.mp3"

    providers.download_file("https://cdn.ai33.pro/v3/tts/a.mp3", output)

    assert output.read_bytes() == b"mp3"
    assert captured["headers"]["User-agent"] == "Mozilla/5.0"
