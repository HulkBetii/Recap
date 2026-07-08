from __future__ import annotations

import pytest

from orchestrator.config import load_config
from orchestrator.cost_policy import disallowed_openai_stages, resolve_cost_policy


def test_balanced_policy_keeps_preset_and_playwright() -> None:
    config = load_config(None)
    resolved, policy = resolve_cost_policy(config)

    assert policy.quality_mode == "balanced"
    assert policy.text_llm_backend == "chatgpt_playwright"
    assert resolved["review"]["llm_backend"] == "chatgpt_playwright"
    assert policy.stages["review"]["backend"] == "chatgpt_playwright"


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

    assert disallowed_openai_stages(policy, {"ingest"}) == ["ingest:translation"]


def test_invalid_quality_mode_fails() -> None:
    config = load_config(None)
    config["orchestrator"]["quality_mode"] = "cheap"
    with pytest.raises(ValueError, match="quality_mode"):
        resolve_cost_policy(config)
