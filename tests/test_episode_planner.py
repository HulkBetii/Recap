from __future__ import annotations

import json
from pathlib import Path

from common.inputs import load_review_context
from episode_planner.planner import EpisodePlanSettings, build_episode_plan, parse_episode_from_filename, select_recap_mode
from review.llm_flow import build_outline_prompt

def write_film_map(path: Path, text: str = "Aki waits for the next clue.") -> None:
    path.write_text(
        json.dumps(
            [
                {"id": 0, "type": "speech", "tc_start": 0.0, "tc_end": 5.0, "ko": text, "en": text, "scene_desc": None},
                {"id": 1, "type": "speech", "tc_start": 5.0, "tc_end": 10.0, "ko": text, "en": text, "scene_desc": None},
            ]
        ),
        encoding="utf-8",
    )
    path.with_name("film_map.meta.json").write_text(
        json.dumps(
            {
                "input_path": "episode.mp4",
                "duration": 10.0,
                "created_at": "2026-07-02T00:00:00Z",
                "whisper_model": "large-v3",
                "translate_model": "gpt-4.1-mini",
                "vision_model": "gpt-4.1-mini",
                "gap_threshold": 4.0,
                "max_vision_frames": 0,
                "speech_count": 2,
                "visual_count": 0,
                "cache_hits": [],
                "warnings_count": 0,
            }
        ),
        encoding="utf-8",
    )

def write_manifest(path: Path, *, episode_number: int, source_path: Path, spoiler_limit_episode: int | None = None) -> None:
    path.write_text(
        "\n".join(
            [
                "series_id: test-series",
                f"episode_key: e{episode_number:02d}",
                f"episode_number: {episode_number}",
                f"title: Episode {episode_number}",
                f"source_path: {json.dumps(str(source_path))}",
                "arc: opening",
                f"spoiler_limit_episode: {spoiler_limit_episode or episode_number}",
                "",
            ]
        ),
        encoding="utf-8",
    )

def test_episode_filename_parser_is_sanity_fallback() -> None:
    key, episode_number = parse_episode_from_filename(Path("My_Show.S02E07.1080p.mp4"))

    assert key == "s02e07"
    assert episode_number == 7

def test_recap_mode_thresholds_are_locked() -> None:
    settings = EpisodePlanSettings()

    assert select_recap_mode(0.70, settings) == "full"
    assert select_recap_mode(0.50, settings) == "quick"
    assert select_recap_mode(0.20, settings) == "merge"
    assert select_recap_mode(0.05, settings) == "skip"

def test_episode_memory_index_respects_spoiler_limit(tmp_path: Path) -> None:
    film = tmp_path / "show.S01E05.mp4"
    film.write_bytes(b"film")
    film_map = tmp_path / "film_map.json"
    write_film_map(film_map)
    memory_dir = tmp_path / "series_memory"

    for episode_number in range(1, 5):
        manifest = tmp_path / f"series_manifest_{episode_number}.yaml"
        write_manifest(manifest, episode_number=episode_number, source_path=film)
        build_episode_plan(
            film=film,
            film_map_path=film_map,
            output_meta_path=tmp_path / f"episode_{episode_number}.meta.json",
            output_memory_path=tmp_path / f"episode_{episode_number}.memory.json",
            settings=EpisodePlanSettings(recap_mode="auto"),
            series_manifest_path=manifest,
            series_memory_dir=memory_dir,
            video_profile_path=None,
            story_map_path=None,
            anime_context_path=None,
        )

    current_manifest = tmp_path / "series_manifest_5.yaml"
    write_manifest(current_manifest, episode_number=5, source_path=film, spoiler_limit_episode=3)
    meta, memory = build_episode_plan(
        film=film,
        film_map_path=film_map,
        output_meta_path=tmp_path / "episode_5.meta.json",
        output_memory_path=tmp_path / "episode_5.memory.json",
        settings=EpisodePlanSettings(recap_mode="auto"),
        series_manifest_path=current_manifest,
        series_memory_dir=memory_dir,
        video_profile_path=None,
        story_map_path=None,
        anime_context_path=None,
    )

    assert meta.previous_memory_count == 3
    assert [item.episode_number for item in memory.previous] == [1, 2, 3]

def test_episode_memory_context_is_available_to_review_prompt(tmp_path: Path) -> None:
    film = tmp_path / "show.S01E01.mp4"
    film.write_bytes(b"film")
    film_map = tmp_path / "film_map.json"
    write_film_map(film_map, "Aki discovers a secret clue and promises to remember it.")
    manifest = tmp_path / "series_manifest.yaml"
    write_manifest(manifest, episode_number=1, source_path=film)
    _meta, memory = build_episode_plan(
        film=film,
        film_map_path=film_map,
        output_meta_path=tmp_path / "episode_meta.json",
        output_memory_path=tmp_path / "episode_memory.json",
        settings=EpisodePlanSettings(recap_mode="quick"),
        series_manifest_path=manifest,
        series_memory_dir=tmp_path / "series_memory",
        video_profile_path=None,
        story_map_path=None,
        anime_context_path=None,
    )

    bundle = load_review_context(tmp_path / "episode_memory.json")
    prompt = build_outline_prompt(
        film_map_view="0 | 0.0-5.0 | speech | Aki discovers a secret clue.",
        target_video_s=60,
        char_budget=900,
        min_coverage=0.45,
        content_type="anime_series",
        hook_mode="cold_open",
        episode_memory=bundle.episode_memory,
    )

    assert bundle.episode_memory == memory
    assert "EPISODE MEMORY" in prompt
    assert "Quick recap mode" in prompt
    assert "spoiler_limit_episode" in prompt
