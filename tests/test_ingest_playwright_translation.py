from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from common.playwright_chat import PlaywrightChatError
from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from common.schema import TranslatedSegment, TranscriptSegment
from ingest.__main__ import IngestError, build_parser, load_translations, run_ingest
from ingest.cache import StageCache
from ingest.integrity import translation_cache_key, translation_model_identity
from ingest.playwright_translation import (
    PlaywrightTranslationClient,
    PlaywrightTranslationError,
    batch_cache_key,
    validate_translation_response,
)
from orchestrator.config import load_config
from orchestrator.cost_policy import disallowed_openai_stages, resolve_cost_policy
from orchestrator.graph import build_paths
from orchestrator.runner import build_command


def segment(segment_id: int, text: str = "こんにちは") -> TranscriptSegment:
    return TranscriptSegment(
        id=segment_id,
        tc_start=float(segment_id),
        tc_end=float(segment_id + 1),
        ko=text,
    )


class FakeChatClient:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.prompts: list[str] = []
        self.enter_count = 0

    async def __aenter__(self) -> "FakeChatClient":
        self.enter_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(
    tmp_path: Path,
    fake_chat: FakeChatClient,
    *,
    batch_size: int = 2,
) -> PlaywrightTranslationClient:
    return PlaywrightTranslationClient(
        profile_dir=CHATGPT_PLAYWRIGHT_PROFILE_DIR,
        cache_dir=tmp_path / "translation_batches",
        batch_size=batch_size,
        chat_client_factory=lambda: fake_chat,  # type: ignore[arg-type]
    )


def test_translation_response_accepts_fenced_json_and_preserves_exact_ids() -> None:
    batch = [segment(3), segment(7, "行こう")]

    result = validate_translation_response(
        '```json\n{"3":"Hello.","7":"Let us go."}\n```',
        batch,
    )

    assert result == {"3": "Hello.", "7": "Let us go."}


def test_translation_response_allows_unchanged_latin_name() -> None:
    assert validate_translation_response('{"3":"Rimuru"}', [segment(3, "Rimuru")]) == {
        "3": "Rimuru"
    }


@pytest.mark.parametrize(
    "response,match",
    [
        ('{"3":"Hello."}', "coverage mismatch"),
        ('{"3":"Hello.","7":"Go.","9":"extra"}', "coverage mismatch"),
        ('{"3":"こんにちは","7":"行こう"}', "echo Japanese"),
        ("not json", "did not contain"),
    ],
)
def test_translation_response_rejects_invalid_or_japanese_echo(
    response: str,
    match: str,
) -> None:
    with pytest.raises(PlaywrightTranslationError, match=match):
        validate_translation_response(response, [segment(3), segment(7, "行こう")])


def test_playwright_translation_preserves_source_contract_and_reuses_batch_cache(tmp_path: Path) -> None:
    segments = [segment(0), segment(1, "行こう"), segment(2, "大丈夫")]
    first_chat = FakeChatClient(
        [
            '{"0":"Hello.","1":"Let us go."}',
            '{"2":"It is all right."}',
        ]
    )
    client = make_client(tmp_path, first_chat)

    translated, warnings = client.translate_segments(segments, source_language="ja")

    assert warnings == 0
    assert [item.id for item in translated] == [0, 1, 2]
    assert [(item.tc_start, item.tc_end, item.ko) for item in translated] == [
        (item.tc_start, item.tc_end, item.ko) for item in segments
    ]
    assert [item.en for item in translated] == ["Hello.", "Let us go.", "It is all right."]
    assert len(first_chat.prompts) == 2

    def fail_factory():
        raise AssertionError("browser must not start when every batch is cached")

    cached_client = PlaywrightTranslationClient(
        profile_dir=CHATGPT_PLAYWRIGHT_PROFILE_DIR,
        cache_dir=tmp_path / "translation_batches",
        batch_size=2,
        chat_client_factory=fail_factory,  # type: ignore[arg-type]
    )
    cached, cached_warnings = cached_client.translate_segments(segments, source_language="ja")

    assert cached_warnings == 0
    assert [item.en for item in cached] == [item.en for item in translated]


def test_completed_batch_resumes_after_later_browser_failure(tmp_path: Path) -> None:
    segments = [segment(0), segment(1), segment(2)]
    failure = PlaywrightChatError(
        "browser disconnected",
        code="page_disconnected",
        retryable=True,
        fallback_eligible=True,
        attempts=2,
    )
    first_chat = FakeChatClient(['{"0":"Zero.","1":"One."}', failure])

    with pytest.raises(PlaywrightTranslationError, match="page_disconnected"):
        make_client(tmp_path, first_chat).translate_segments(segments, source_language="ja")

    second_chat = FakeChatClient(['{"2":"Two."}'])
    translated, _warnings = make_client(tmp_path, second_chat).translate_segments(
        segments,
        source_language="ja",
    )

    assert len(second_chat.prompts) == 1
    assert [item.en for item in translated] == ["Zero.", "One.", "Two."]


def test_invalid_coverage_retries_only_current_batch(tmp_path: Path) -> None:
    segments = [segment(0), segment(1), segment(2)]
    chat = FakeChatClient(
        [
            '{"0":"Zero."}',
            '{"0":"Zero.","1":"One."}',
            '{"2":"Two."}',
        ]
    )

    translated, _warnings = make_client(tmp_path, chat).translate_segments(
        segments,
        source_language="ja",
    )

    assert len(chat.prompts) == 3
    assert [item.en for item in translated] == ["Zero.", "One.", "Two."]


def test_translation_cache_identity_tracks_provider_model_batch_and_content() -> None:
    settings = {
        "translate_mode": "ja-en",
        "translate_model": "ignored-api-model",
        "translation_provider": "chatgpt_playwright",
        "translation_batch_size": 80,
    }
    first = translation_cache_key("transcript-a", settings)

    assert translation_model_identity(settings) == "chatgpt-web"
    assert first != translation_cache_key("transcript-a", {**settings, "translation_batch_size": 40})
    assert first != translation_cache_key("transcript-a", {**settings, "translation_provider": "openai_api"})
    assert first == translation_cache_key("transcript-a", {**settings, "translate_model": "another-api-model"})
    assert batch_cache_key([segment(0)], source_language="ja", model="chatgpt-web") != batch_cache_key(
        [segment(0, "行こう")],
        source_language="ja",
        model="chatgpt-web",
    )


def test_translation_factory_error_becomes_clean_ingest_error(tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work")
    cache.prepare()

    def fail_factory():
        raise PlaywrightTranslationError("profile is unavailable")

    with pytest.raises(IngestError, match="profile is unavailable"):
        load_translations(
            cache,
            [segment(0)],
            None,
            logging.getLogger("test"),
            client_factory=fail_factory,
            translate_mode="ja-en",
            source_language="ja",
        )


def test_top_level_translation_cache_does_not_create_client(tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work")
    cache.prepare()
    cache.write_json(
        "translated.json",
        [TranslatedSegment(id=0, tc_start=0, tc_end=1, ko="こんにちは", en="Hello.")],
    )

    def fail_factory():
        raise AssertionError("translation client factory must stay lazy on top-level cache hit")

    translated, warnings = load_translations(
        cache,
        [segment(0)],
        None,
        logging.getLogger("test"),
        client_factory=fail_factory,
        translate_mode="ja-en",
        source_language="ja",
        translation_required=True,
        translation_min_success_ratio=0.95,
    )

    assert warnings == 0
    assert translated[0].en == "Hello."


def test_run_ingest_playwright_translation_does_not_require_openai_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "episode.mkv"
    input_path.write_bytes(b"video")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("ingest.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("ingest.__main__.probe_duration", lambda _path: 5.0)
    monkeypatch.setattr("ingest.__main__.extract_audio", lambda _src, dst: dst.write_bytes(b"wav"))
    monkeypatch.setattr(
        "ingest.__main__.transcribe_korean",
        lambda *_args, **_kwargs: [segment(0)],
    )
    monkeypatch.setattr(
        "ingest.__main__.OpenAIIngestClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OpenAI client must not be created")),
    )

    class FakeTranslationClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def translate_segments(self, transcript, **_kwargs):  # type: ignore[no-untyped-def]
            return [
                TranslatedSegment(
                    id=item.id,
                    tc_start=item.tc_start,
                    tc_end=item.tc_end,
                    ko=item.ko,
                    en="Hello.",
                )
                for item in transcript
            ], 0

    monkeypatch.setattr("ingest.__main__.PlaywrightTranslationClient", FakeTranslationClient)
    args = build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output",
            str(tmp_path / "film_map.json"),
            "--work-dir",
            str(tmp_path / "work"),
            "--source-language",
            "ja",
            "--translate-mode",
            "ja-en",
            "--translation-provider",
            "chatgpt_playwright",
            "--translation-required",
            "--translation-min-success-ratio",
            "0.95",
            "--vision-provider",
            "off",
            "--max-vision-frames",
            "0",
            "--drop-non-korean-intro-s",
            "0",
        ]
    )

    assert run_ingest(args) == 0
    meta = json.loads((tmp_path / "film_map.meta.json").read_text(encoding="utf-8"))
    assert meta["translation_provider"] == "chatgpt_playwright"
    assert meta["translate_model"] == "chatgpt-web"


def test_orchestrator_propagates_playwright_translation_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "ingest": {
                    "source_language": "ja",
                    "translate_mode": "ja-en",
                    "translation_provider": "chatgpt_playwright",
                    "translation_batch_size": 80,
                    "translation_batch_max_attempts": 3,
                    "translation_chatgpt_profile_dir": str(CHATGPT_PLAYWRIGHT_PROFILE_DIR),
                    "translation_headless": True,
                }
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    command = build_command(
        "ingest",
        build_paths(tmp_path / "run"),
        tmp_path / "episode.mkv",
        config,
        force=False,
        python_exe="python",
    )

    assert command[command.index("--translation-provider") + 1] == "chatgpt_playwright"
    assert command[command.index("--translation-batch-size") + 1] == "80"
    assert command[command.index("--translation-batch-max-attempts") + 1] == "3"
    assert "--translation-headless" in command


def test_vieneu_preset_uses_browser_translation_without_openai_fallback() -> None:
    config = load_config(Path("config.anime.series.vieneu.yaml"))
    _resolved, policy = resolve_cost_policy(config)

    assert config["orchestrator"]["api_budget_guard"] == "block"
    assert config["ingest"]["translation_provider"] == "chatgpt_playwright"
    assert policy.stages["ingest"]["translation_provider"] == "chatgpt_playwright"
    assert policy.stages["ingest"]["openai_uses"] == []
    assert disallowed_openai_stages(policy, {"ingest"}) == []
