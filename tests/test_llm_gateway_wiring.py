from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from common.llm_gateway import DEFAULT_GATEWAY_MODEL, GatewayChatError, GatewayHealth
from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from common.schema import TranscriptSegment
from ingest.__main__ import IngestError, run_ingest
from ingest.__main__ import build_parser as build_ingest_parser
from ingest.playwright_translation import PlaywrightTranslationClient, PlaywrightTranslationError, batch_cache_key
from orchestrator import runner as orchestrator_runner
from orchestrator.config import load_config
from orchestrator.cost_policy import disallowed_openai_stages, resolve_cost_policy
from orchestrator.graph import build_paths
from orchestrator.runner import OrchestratorError, build_command, require_gateway_ready
from review import __main__ as review_main
from review.integrity import build_review_identity
from series_composer.__main__ import build_parser as build_composer_parser
from series_composer.__main__ import composer_settings


class FakeGatewayReviewClient:
    instances: list["FakeGatewayReviewClient"] = []

    def __init__(self, *, model: str, base_url: str | None = None, timeout_s: int = 600) -> None:
        self.model = model
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.prompts: list[str] = []
        FakeGatewayReviewClient.instances.append(self)

    async def __aenter__(self) -> "FakeGatewayReviewClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "keys: glossary, outline, hook" in prompt:
            return json.dumps(
                {
                    "glossary": [{"name": "Minh", "role": "nhân vật chính"}],
                    "outline": [
                        {"from_seg_id": 3, "to_seg_id": 3, "summary": "hook"},
                        {"from_seg_id": 0, "to_seg_id": 1, "summary": "mở đầu"},
                        {"from_seg_id": 2, "to_seg_id": 3, "summary": "cao trào"},
                    ],
                    "hook": [3],
                },
                ensure_ascii=False,
            )
        if "JSON array of objects" in prompt:
            return json.dumps(
                [
                    {"beat_id": 0, "narration": "Một bí mật kinh hoàng mở ra."},
                    {"beat_id": 1, "narration": "Minh bắt đầu phát hiện mọi thứ không bình thường."},
                    {"beat_id": 2, "narration": "Cuối cùng, sự thật khiến tất cả đảo lộn."},
                ],
                ensure_ascii=False,
            )
        return json.dumps({"pass": True, "issues": [], "notes": "ok"}, ensure_ascii=False)

    def usage_summary(self) -> dict:
        return {"provider": "ninerouter", "model": self.model, "request_count": len(self.prompts), "input_tokens": 0, "output_tokens": 0}


def write_film_map(tmp_path: Path) -> Path:
    film_map = [
        {"id": 0, "type": "speech", "tc_start": 0.0, "tc_end": 1.0, "ko": "a", "en": "A boy enters."},
        {"id": 1, "type": "speech", "tc_start": 1.0, "tc_end": 2.0, "ko": "b", "en": "He sees a clue."},
        {"id": 2, "type": "visual", "tc_start": 2.0, "tc_end": 3.0, "scene_desc": "A dark hallway."},
        {"id": 3, "type": "speech", "tc_start": 3.0, "tc_end": 4.0, "ko": "c", "en": "The secret is revealed."},
    ]
    path = tmp_path / "film_map.json"
    path.write_text(json.dumps(film_map, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "film_map.meta.json").write_text(json.dumps({"duration_s": 4.0}), encoding="utf-8")
    return path


def test_review_runs_through_gateway_without_chatgpt_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGatewayReviewClient.instances.clear()
    monkeypatch.setattr(review_main, "GatewayChatClient", FakeGatewayReviewClient)
    film_map = write_film_map(tmp_path)
    work_dir = tmp_path / "work" / "review"
    args = review_main.build_parser().parse_args(
        [
            "--film-map", str(film_map),
            "--output", str(tmp_path / "review_script.json"),
            "--target-ratio", "0.33",
            "--max-qa-iterations", "1",
            "--llm-backend", "ninerouter",
            "--llm-model", "ag/test-model",
            # A non-canonical profile would raise if the Playwright path were touched.
            "--chatgpt-profile-dir", str(tmp_path / "not-the-locked-profile"),
            "--work-dir", str(work_dir),
            "--log-level", "ERROR",
        ]
    )

    assert asyncio.run(review_main.run_review(args)) == 0

    client = FakeGatewayReviewClient.instances[-1]
    assert client.model == "ag/test-model"
    assert (tmp_path / "review_script.json").is_file()
    usage = json.loads((work_dir / "gateway_usage.json").read_text(encoding="utf-8"))
    assert usage["provider"] == "ninerouter"
    assert not (work_dir / "chat_session_meta.json").exists()
    meta = json.loads((tmp_path / "review_script.meta.json").read_text(encoding="utf-8"))
    assert meta["llm_backend"] == "ninerouter"
    assert meta["model_versions"]["llm"] == "ninerouter:ag/test-model"


def test_review_identity_keys_on_model_only_for_gateway(tmp_path: Path) -> None:
    film_map = write_film_map(tmp_path)

    def config_hash(settings: dict) -> str:
        return build_review_identity(
            film_map_path=film_map,
            settings=settings,
            style_sample_path=None,
            story_map_path=None,
            video_profile_path=None,
        ).config_hash

    playwright = config_hash({"llm_backend": "chatgpt_playwright"})
    assert config_hash({"llm_backend": "chatgpt_playwright", "llm_model": "ag/x"}) == playwright
    gateway_default = config_hash({"llm_backend": "ninerouter", "llm_model": None})
    assert gateway_default == config_hash({"llm_backend": "ninerouter", "llm_model": DEFAULT_GATEWAY_MODEL})
    assert gateway_default != config_hash({"llm_backend": "ninerouter", "llm_model": "ag/other"})
    assert gateway_default != playwright


def test_cost_policy_accepts_gateway_and_disables_openai_fallback() -> None:
    config = load_config(None)
    config["orchestrator"].update({"text_llm_backend": "ninerouter", "api_budget_guard": "block"})
    config["review"].update({"llm_model": "ag/x", "openai_fallback_model": "gpt-4.1-mini"})
    config["ingest"].update({"translate_mode": "ko-en", "translation_provider": "ninerouter", "max_vision_frames": 0, "asr_provider": "faster-whisper"})

    resolved, policy = resolve_cost_policy(config)

    assert resolved["review"]["llm_backend"] == "ninerouter"
    review = policy.stages["review"]
    assert review["cost"] == "gateway"
    assert review["llm_model"] == "ag/x"
    assert review["openai_fallback_configured"] is False
    ingest = policy.stages["ingest"]
    assert ingest["backend"] == "ninerouter"
    assert ingest["cost"] == "gateway"
    assert disallowed_openai_stages(policy, {"ingest", "review"}) == []


def test_orchestrator_forwards_gateway_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "orchestrator": {"text_llm_backend": "ninerouter"},
                "review": {"llm_backend": "ninerouter", "llm_model": "ag/x", "llm_base_url": "http://gw.test/v1"},
                "ingest": {"translation_provider": "ninerouter", "translate_model": "ag/y", "translation_concurrency": 3},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    paths = build_paths(tmp_path / "run")
    review_command = build_command("review", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")
    ingest_command = build_command("ingest", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert review_command[review_command.index("--llm-backend") + 1] == "ninerouter"
    assert review_command[review_command.index("--llm-model") + 1] == "ag/x"
    assert review_command[review_command.index("--llm-base-url") + 1] == "http://gw.test/v1"
    assert ingest_command[ingest_command.index("--translation-provider") + 1] == "ninerouter"
    assert ingest_command[ingest_command.index("--translation-concurrency") + 1] == "3"
    review_args = review_main.build_parser().parse_args(review_command[3:])
    assert review_args.llm_model == "ag/x"


def test_gateway_preflight_requires_key_and_listed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NINEROUTER_API_KEY", raising=False)
    with pytest.raises(OrchestratorError, match="NINEROUTER_API_KEY"):
        require_gateway_ready(purpose="review", model="ag/x", base_url=None)

    monkeypatch.setenv("NINEROUTER_API_KEY", "k-test")
    monkeypatch.setattr(
        orchestrator_runner,
        "check_gateway_health",
        lambda **_kwargs: GatewayHealth(reachable=True, model_available=False, reason="LLM gateway does not list model ag/x"),
    )
    with pytest.raises(OrchestratorError, match="does not list model"):
        require_gateway_ready(purpose="review", model="ag/x", base_url=None)

    monkeypatch.setattr(orchestrator_runner, "check_gateway_health", lambda **_kwargs: GatewayHealth(reachable=True, model_available=True))
    # Listed but not served upstream: the live probe must still block the run.
    monkeypatch.setattr(
        orchestrator_runner,
        "probe_gateway_model",
        lambda **_kwargs: GatewayHealth(reachable=True, model_available=False, reason="model ag/x probe failed (upstream_rejected)"),
    )
    with pytest.raises(OrchestratorError, match="upstream_rejected"):
        require_gateway_ready(purpose="review", model="ag/x", base_url=None)

    monkeypatch.setattr(orchestrator_runner, "probe_gateway_model", lambda **_kwargs: GatewayHealth(reachable=True, model_available=True))
    require_gateway_ready(purpose="review", model="ag/x", base_url=None)


def test_composer_cli_accepts_gateway_and_keys_cache_on_model() -> None:
    base = [
        "--manifest", "m.yaml",
        "--episode-run-dir", "e01=run",
        "--output-event-bank", "bank.json",
        "--output", "script.json",
        "--output-tts-script", "tts.json",
    ]
    gateway = build_composer_parser().parse_args(base + ["--llm-backend", "ninerouter", "--llm-model", "ag/x"])
    playwright = build_composer_parser().parse_args(base)

    assert composer_settings(gateway, {})["llm_model"] == "ag/x"
    assert "llm_model" not in composer_settings(playwright, {})


def test_ui_health_checks_gateway_and_skips_unneeded_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from recap_ui import health as health_module

    probed: list[tuple[str | None, str | None]] = []

    def fake_health(*, model: str | None, base_url: str | None = None) -> GatewayHealth:
        probed.append((model, base_url))
        return GatewayHealth(reachable=True, model_available=model != "ag/missing")

    monkeypatch.setattr(health_module, "check_gateway_health", fake_health)
    monkeypatch.setenv("NINEROUTER_API_KEY", "k-test")
    config = load_config(None)
    config["orchestrator"]["text_llm_backend"] = "ninerouter"
    config["review"].update({"llm_backend": "ninerouter", "llm_model": "ag/x"})
    config["series_recap"]["llm_backend"] = "ninerouter"
    config["ingest"].update({"translate_mode": "ko-en", "translation_provider": "ninerouter", "translate_model": "ag/x"})
    config["tts"].update({"provider_mode": "capcut", "voice_id": "BV074_streaming"})

    report = health_module.collect_runtime_health(tmp_path, profile_dir=tmp_path / "missing-profile", config=config)
    checks = {check.code: check for check in report.runtime}

    assert checks["llm_gateway"].status.value == "pass"
    assert checks["chatgpt_profile"].status.value == "pass"
    assert checks["chatgpt_profile"].details["required"] is False
    assert checks["tts_voice"].status.value == "pass"
    assert report.providers["ninerouter"] is True
    assert probed == [("ag/x", None), (DEFAULT_GATEWAY_MODEL, None)]

    config["review"]["llm_model"] = "ag/missing"
    report = health_module.collect_runtime_health(tmp_path, profile_dir=tmp_path / "missing-profile", config=config)
    assert {check.code: check for check in report.runtime}["llm_gateway"].status.value == "block"


def segment(segment_id: int) -> TranscriptSegment:
    return TranscriptSegment(id=segment_id, tc_start=float(segment_id), tc_end=float(segment_id + 1), ko=f"안녕 {segment_id}")


class ConcurrentFakeChat:
    def __init__(self, *, fail_on_id: int | None = None) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_on_id = fail_on_id
        self.cancelled = 0

    async def __aenter__(self) -> "ConcurrentFakeChat":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    async def ask(self, prompt: str) -> str:
        ids = json.loads(prompt.split("EXPECTED_IDS: ", 1)[1].split("\n", 1)[0])
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.05 if self.fail_on_id is None or str(self.fail_on_id) in ids else 0.5)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.in_flight -= 1
        if self.fail_on_id is not None and str(self.fail_on_id) in ids:
            raise GatewayChatError("upstream exploded", code="upstream_error", retryable=True, attempts=4)
        return json.dumps({segment_id: f"Hello {segment_id}" for segment_id in ids})


def gateway_translation_client(tmp_path: Path, chat: ConcurrentFakeChat, *, concurrency: int) -> PlaywrightTranslationClient:
    return PlaywrightTranslationClient(
        profile_dir=CHATGPT_PLAYWRIGHT_PROFILE_DIR,
        cache_dir=tmp_path / "translation_batches",
        model="ag/y",
        batch_size=2,
        chat_client_factory=lambda: chat,
        provider="ninerouter",
        concurrency=concurrency,
    )


def test_gateway_translation_runs_batches_concurrently(tmp_path: Path) -> None:
    chat = ConcurrentFakeChat()
    segments = [segment(index) for index in range(10)]

    translated, warnings = gateway_translation_client(tmp_path, chat, concurrency=3).translate_segments(segments, source_language="ko")

    assert warnings == 0
    assert [item.en for item in translated] == [f"Hello {index}" for index in range(10)]
    assert chat.max_in_flight == 3
    cached = sorted((tmp_path / "translation_batches").glob("*.json"))
    assert len(cached) == 5
    assert json.loads(cached[0].read_text(encoding="utf-8"))["provider"] == "ninerouter"


def test_gateway_translation_failure_cancels_siblings(tmp_path: Path) -> None:
    chat = ConcurrentFakeChat(fail_on_id=0)
    with pytest.raises(PlaywrightTranslationError, match="ninerouter translation failed"):
        gateway_translation_client(tmp_path, chat, concurrency=3).translate_segments([segment(index) for index in range(6)], source_language="ko")
    assert chat.cancelled == 2


def test_translation_cache_key_separates_providers() -> None:
    batch = [segment(0)]
    assert batch_cache_key(batch, source_language="ko", model="m") != batch_cache_key(
        batch, source_language="ko", model="m", provider="ninerouter"
    )


def test_playwright_translation_rejects_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency=1"):
        PlaywrightTranslationClient(
            profile_dir=CHATGPT_PLAYWRIGHT_PROFILE_DIR,
            cache_dir=tmp_path,
            chat_client_factory=lambda: ConcurrentFakeChat(),
            concurrency=2,
        )


def test_ingest_cli_rejects_concurrency_without_gateway(tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"")
    args = build_ingest_parser().parse_args(
        [
            "--input", str(video),
            "--output", str(tmp_path / "film_map.json"),
            "--translation-provider", "chatgpt_playwright",
            "--translation-concurrency", "2",
            "--work-dir", str(tmp_path / "work"),
        ]
    )
    with pytest.raises(IngestError, match="requires --translation-provider ninerouter"):
        run_ingest(args)
