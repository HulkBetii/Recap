from __future__ import annotations

import pytest

from orchestrator.config import ConfigError, load_config
from orchestrator.graph import forced_stages, stage_range


def test_stage_range_full_and_only() -> None:
    assert stage_range() == {"preflight", "ingest", "review", "tts", "shots", "match", "render"}
    assert stage_range(only="match") == {"match"}


def test_stage_range_from_tts_to_match_includes_shots_by_order() -> None:
    assert stage_range("tts", "match") == {"tts", "shots", "match"}


def test_only_cannot_combine_with_range() -> None:
    with pytest.raises(ValueError):
        stage_range("ingest", None, "match")


def test_force_stage_invalidates_downstream() -> None:
    selected = set(stage_range())
    assert forced_stages(selected, False, ["match"]) == {"match", "render"}
    assert forced_stages({"tts", "shots", "match"}, False, ["tts"]) == {"tts", "match"}


def test_config_rejects_unknown_key(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"ingest":{"unknown":1}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(path)
