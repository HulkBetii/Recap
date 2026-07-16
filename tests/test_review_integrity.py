from __future__ import annotations

import json
from pathlib import Path

from review.integrity import build_review_identity


def settings() -> dict:
    return {
        "target_ratio": "auto",
        "tts_cps": 15,
        "auto_max_ratio": 0.40,
        "auto_soft_cap_s": 2100,
        "auto_hard_cap_s": 2700,
        "auto_long_score_threshold": 0.80,
        "min_coverage": 0.85,
        "max_qa_iterations": 2,
        "max_qa_rewrites_per_iteration": 6,
        "content_type": "movie",
        "hook_mode": "setup",
        "opening_coherence_qa": True,
        "micro_beats": False,
        "target_beat_audio_s": 12,
        "max_beat_audio_s": 18,
        "style_preset": "viral-recap-vi",
        "style_strength": "strong",
        "style_qa": True,
        "target_sentence_chars": 160,
        "max_sentence_chars": 220,
        "drop_non_story_beats": True,
        "non_story_tail_s": 300,
        "llm_backend": "chatgpt_playwright",
    }


def test_review_identity_hashes_file_contents(tmp_path: Path) -> None:
    film_map = tmp_path / "film_map.json"
    film_map.write_text(json.dumps([{"id": 0}]), encoding="utf-8")
    film_map.with_name("film_map.meta.json").write_text(json.dumps({"duration": 1}), encoding="utf-8")
    style = tmp_path / "style.txt"
    style.write_text("style one", encoding="utf-8")

    first = build_review_identity(
        film_map_path=film_map,
        settings=settings(),
        style_sample_path=style,
        story_map_path=None,
        video_profile_path=None,
    )
    film_map.write_text(json.dumps([{"id": 1}]), encoding="utf-8")
    second = build_review_identity(
        film_map_path=film_map,
        settings=settings(),
        style_sample_path=style,
        story_map_path=None,
        video_profile_path=None,
    )

    assert first.core_input_hash != second.core_input_hash
    assert first.cache_key != second.cache_key


def test_operational_browser_settings_do_not_change_review_cache(tmp_path: Path) -> None:
    film_map = tmp_path / "film_map.json"
    film_map.write_text("[]", encoding="utf-8")
    style = tmp_path / "style.txt"
    style.write_text("style", encoding="utf-8")
    first_settings = settings() | {"headless": False, "reply_timeout_s": 600, "chatgpt_profile_dir": "profile-a"}
    second_settings = settings() | {"headless": True, "reply_timeout_s": 900, "chatgpt_profile_dir": "profile-b"}

    first = build_review_identity(film_map_path=film_map, settings=first_settings, style_sample_path=style, story_map_path=None, video_profile_path=None)
    second = build_review_identity(film_map_path=film_map, settings=second_settings, style_sample_path=style, story_map_path=None, video_profile_path=None)

    assert first.cache_key == second.cache_key

def test_auto_duration_policy_change_invalidates_review_cache(tmp_path: Path) -> None:
    film_map = tmp_path / "film_map.json"
    film_map.write_text("[]", encoding="utf-8")
    style = tmp_path / "style.txt"
    style.write_text("style", encoding="utf-8")

    first = build_review_identity(film_map_path=film_map, settings=settings(), style_sample_path=style, story_map_path=None, video_profile_path=None)
    second = build_review_identity(
        film_map_path=film_map,
        settings=settings() | {"auto_soft_cap_s": 1800},
        style_sample_path=style,
        story_map_path=None,
        video_profile_path=None,
    )

    assert first.cache_key != second.cache_key

def test_micro_beat_policy_change_invalidates_review_cache(tmp_path: Path) -> None:
    film_map = tmp_path / "film_map.json"
    film_map.write_text("[]", encoding="utf-8")
    style = tmp_path / "style.txt"
    style.write_text("style", encoding="utf-8")

    first = build_review_identity(film_map_path=film_map, settings=settings(), style_sample_path=style, story_map_path=None, video_profile_path=None)
    enabled = build_review_identity(
        film_map_path=film_map,
        settings=settings() | {"micro_beats": True},
        style_sample_path=style,
        story_map_path=None,
        video_profile_path=None,
    )
    retuned = build_review_identity(
        film_map_path=film_map,
        settings=settings() | {"target_beat_audio_s": 10, "max_beat_audio_s": 16},
        style_sample_path=style,
        story_map_path=None,
        video_profile_path=None,
    )

    assert first.cache_key != enabled.cache_key
    assert first.cache_key != retuned.cache_key

def test_chatgpt_model_labels_change_review_identity(tmp_path: Path) -> None:
    film_map = tmp_path / "film_map.json"
    film_map.write_text("[]", encoding="utf-8")
    style = tmp_path / "style.txt"
    style.write_text("style", encoding="utf-8")

    first = build_review_identity(
        film_map_path=film_map,
        settings=settings() | {"chatgpt_model_label": "GPT-5.6 Sol", "chatgpt_intelligence_label": "Instant"},
        style_sample_path=style,
        story_map_path=None,
        video_profile_path=None,
    )
    second = build_review_identity(
        film_map_path=film_map,
        settings=settings() | {"chatgpt_model_label": "GPT-5.6 Pro", "chatgpt_intelligence_label": "Instant"},
        style_sample_path=style,
        story_map_path=None,
        video_profile_path=None,
    )

    assert first.core_input_hash != second.core_input_hash
    assert first.cache_key != second.cache_key
