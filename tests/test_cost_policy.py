from __future__ import annotations

from pathlib import Path

import pytest

from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from orchestrator.config import load_config
from orchestrator.cost_policy import disallowed_openai_stages, resolve_cost_policy


def test_balanced_policy_keeps_preset_and_playwright() -> None:
    config = load_config(None)
    resolved, policy = resolve_cost_policy(config)

    assert policy.quality_mode == "balanced"
    assert policy.text_llm_backend == "chatgpt_playwright"
    assert resolved["review"]["llm_backend"] == "chatgpt_playwright"
    assert policy.stages["review"]["backend"] == "chatgpt_playwright"


def test_tts_cost_policy_lists_only_available_auto_providers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VIVOO_API_KEY", "ai33")
    monkeypatch.delenv("GENMAX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    config = load_config(None)
    config["tts"].update({"voice_id": "vbee", "genmax_voice_id": "genmax"})

    _resolved, policy = resolve_cost_policy(config)

    assert policy.stages["tts"]["available_providers"] == ["ai33", "openai"]


def test_low_cost_policy_uses_local_asr_and_disables_vision() -> None:
    config = load_config(None)
    config["orchestrator"]["quality_mode"] = "low_cost"
    config["ingest"]["translate_mode"] = "none"
    resolved, policy = resolve_cost_policy(config)

    assert resolved["ingest"]["asr_policy"] == "local_first"
    assert resolved["ingest"]["asr_provider"] == "faster-whisper"
    assert resolved["ingest"]["max_vision_frames"] == 0
    assert policy.stages["ingest"]["openai_uses"] == []


def test_budget_guard_blocks_low_cost_openai_usage() -> None:
    config = load_config(None)
    config["orchestrator"].update({"quality_mode": "low_cost", "api_budget_guard": "block"})
    config["ingest"]["translate_mode"] = "ko-en"
    _resolved, policy = resolve_cost_policy(config)

    blocked = disallowed_openai_stages(policy, {"ingest"})
    assert len(blocked) == 1
    assert "translation" in blocked[0]


@pytest.mark.parametrize("quality_mode", ["balanced", "max_quality"])
def test_budget_guard_blocks_ingest_openai_in_every_quality_mode(quality_mode: str) -> None:
    config = load_config(None)
    config["orchestrator"].update({"quality_mode": quality_mode, "api_budget_guard": "block"})
    config["ingest"].update({"translate_mode": "ko-en", "max_vision_frames": 0})
    _resolved, policy = resolve_cost_policy(config)

    blocked = disallowed_openai_stages(policy, {"ingest"})
    assert len(blocked) == 1
    assert "translation" in blocked[0]


def test_budget_guard_marks_review_fallback_blocked_without_blocking_playwright() -> None:
    config = load_config(None)
    config["orchestrator"]["api_budget_guard"] = "block"
    config["review"]["openai_fallback_model"] = "gpt-test"
    _resolved, policy = resolve_cost_policy(config)

    review = policy.stages["review"]
    assert review["openai_fallback_configured"] is True
    assert review["openai_fallback_allowed"] is False
    assert review["openai_fallback_blocked"] is True
    assert disallowed_openai_stages(policy, {"review"}) == []


@pytest.mark.parametrize("backend", ["openai_api", "off"])
def test_direct_review_backend_is_rejected(backend: str) -> None:
    config = load_config(None)
    config["orchestrator"]["text_llm_backend"] = backend
    with pytest.raises(ValueError, match="must be chatgpt_playwright"):
        resolve_cost_policy(config)

    config = load_config(None)
    config["review"]["llm_backend"] = backend
    with pytest.raises(ValueError, match="review.llm_backend"):
        resolve_cost_policy(config)


@pytest.mark.parametrize(
    "preset",
    [
        "config.example.yaml",
        "config.movie.stable.yaml",
        "config.movie.visual.yaml",
        "config.movie.production.yaml",
        "config.vi.stable.yaml",
        "config.vi.low_openai.yaml",
        "config.vi.balanced.auto.yaml",
    ],
)
def test_shipped_presets_keep_playwright_as_review_primary(preset: str) -> None:
    resolved, policy = resolve_cost_policy(load_config(Path(preset)))

    assert policy.text_llm_backend == "chatgpt_playwright"
    assert resolved["review"]["llm_backend"] == "chatgpt_playwright"
    assert Path(resolved["review"]["chatgpt_profile_dir"]) == CHATGPT_PLAYWRIGHT_PROFILE_DIR


def test_invalid_quality_mode_fails() -> None:
    config = load_config(None)
    config["orchestrator"]["quality_mode"] = "cheap"
    with pytest.raises(ValueError, match="quality_mode"):
        resolve_cost_policy(config)
