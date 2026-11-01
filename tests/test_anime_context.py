from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.schema import ReviewBeat
from common.inputs import load_anime_context, load_manual_non_story_ranges
from review.__main__ import anime_context_glossary
from review.llm_flow import build_outline_prompt, build_qa_prompt

def test_load_anime_context_example_yaml() -> None:
    context = load_anime_context(Path("examples/anime/anime_context.example.yaml"))

    assert context.kind == "anime_series"
    assert context.episode_number == 1
    assert context.characters[0].name_vi == "Aki"
    assert {item.label for item in context.non_story_ranges} >= {"opening_theme", "ending_theme", "next_episode_preview"}

def test_anime_context_builds_review_glossary_entries() -> None:
    context = load_anime_context(Path("examples/anime/anime_context.example.yaml"))

    glossary = anime_context_glossary(context)

    assert glossary[0]["canonical_name"] == "Aki"
    assert "Aki-kun" in glossary[0]["aliases"]
    assert any(item["canonical_name"] == "Hikari Gate" for item in glossary)

def test_anime_context_is_included_in_review_prompts() -> None:
    context = load_anime_context(Path("examples/anime/anime_context.example.yaml"))
    outline_prompt = build_outline_prompt(
        film_map_view="0 | 10.0-20.0 | speech | hello",
        target_video_s=120,
        char_budget=1800,
        min_coverage=0.85,
        content_type="anime_series",
        hook_mode="cold_open",
        anime_context=context,
    )
    qa_prompt = build_qa_prompt(
        film_map_view="0 | 10.0-20.0 | speech | hello",
        beats=[
            ReviewBeat(
                beat_id=0,
                narration="Aki phát hiện cánh cổng đầu tiên.",
                from_seg_id=0,
                to_seg_id=0,
                src_tc_start=10,
                src_tc_end=20,
                is_hook=True,
            )
        ],
        glossary=[],
        char_budget=1800,
        coverage_pct=1.0,
        content_type="anime_series",
        hook_mode="cold_open",
        anime_context=context,
    )

    assert "ANIME CONTEXT" in outline_prompt
    assert "opening_theme" in outline_prompt
    assert "future-episode spoilers" in outline_prompt
    assert "reject OP/ED/theme-song/preview" in qa_prompt
    assert "unclear episode continuity" in qa_prompt

def test_load_manual_non_story_ranges_accepts_list_json(tmp_path: Path) -> None:
    ranges_path = tmp_path / "ranges.json"
    ranges_path.write_text(
        json.dumps([{"start_s": 0, "end_s": 10, "label": "opening_theme", "confidence": 1.0}]),
        encoding="utf-8",
    )

    ranges = load_manual_non_story_ranges(ranges_path)

    assert ranges[0].label == "opening_theme"
    assert ranges[0].start_s == 0

def test_load_manual_non_story_ranges_rejects_unsupported_label(tmp_path: Path) -> None:
    ranges_path = tmp_path / "ranges.json"
    ranges_path.write_text(
        json.dumps([{"start_s": 0, "end_s": 10, "label": "commercial_break", "confidence": 1.0}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_manual_non_story_ranges(ranges_path)
