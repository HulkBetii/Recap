from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from orchestrator.config import load_config
from series_recap.__main__ import (
    episode_config_for,
    manifest_episode_specs,
    run_series_recap,
    select_episodes,
    write_youtube_chapters,
)

def write_manifest(path: Path, source_one: Path, source_two: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "series_id": "grand-blue-s03",
                "series_title": "Grand Blue Season 3",
                "season": 3,
                "episodes": [
                    {
                        "episode_key": "s03e01",
                        "episode_number": 1,
                        "title": "Episode 1",
                        "source_path": str(source_one),
                        "arc": "summer",
                        "spoiler_limit_episode": 1,
                    },
                    {
                        "episode_key": "s03e02",
                        "episode_number": 2,
                        "title": "Episode 2",
                        "source_path": str(source_two),
                        "arc": "summer",
                        "spoiler_limit_episode": 2,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

def write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "tts": {
                    "voice_id": "voice-fixture",
                    "provider_mode": "ai33",
                },
                "series_recap": {
                    "format": "episode_arc_chaptered",
                    "detail_level": "detailed",
                    "target_total_min_s": 2100,
                    "target_total_max_s": 2700,
                    "target_total_hard_cap_s": 3000,
                    "episode_min_s": 90,
                    "episode_normal_s": 180,
                    "episode_high_s": 300,
                    "arc_size": 3,
                    "mode_target_ratios": {
                        "full": 0.22,
                        "quick": 0.14,
                        "merge": 0.05,
                        "skip": 0.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

def test_manifest_selection_supports_episode_range(tmp_path: Path) -> None:
    manifest_path = tmp_path / "series_manifest.json"
    write_manifest(manifest_path, tmp_path / "e01.mp4", tmp_path / "e02.mp4")

    _manifest, specs = manifest_episode_specs(manifest_path)
    selected = select_episodes(specs, "s03e01-s03e02")

    assert [spec.episode_key for spec in selected] == ["s03e01", "s03e02"]

def test_anime_series_preset_defaults_to_detailed_episode_arc_chaptered() -> None:
    config = load_config(Path("config.anime.series.yaml"))

    assert config["series_recap"]["format"] == "episode_arc_chaptered"
    assert config["series_recap"]["detail_level"] == "detailed"
    assert config["series_recap"]["target_total_min_s"] == 2100
    assert config["series_recap"]["target_total_max_s"] == 2700
    assert config["series_recap"]["target_total_hard_cap_s"] == 3000
    assert config["series_recap"]["arc_size"] == 3
    assert config["series_recap"]["mode_target_ratios"]["quick"] == 0.14
    assert config["series_recap"]["tts_cps"] == 24.0
    assert config["series_recap"]["qa_max_revisions"] == 1

def test_practical_anime_series_preset_uses_translation_only_openai() -> None:
    config = load_config(Path("config.anime.series.practical.yaml"))

    assert config["ingest"]["source_language"] == "ja"
    assert config["ingest"]["translate_mode"] == "ja-en"
    assert config["ingest"]["translation_required"] is True
    assert config["ingest"]["translation_min_success_ratio"] == 0.95
    assert config["ingest"]["vision_provider"] == "off"
    assert config["ingest"]["max_vision_frames"] == 0
    assert config["review"]["openai_fallback_model"] is None
    assert config["series_recap"]["format"] == "episode_arc_chaptered"
    assert config["series_recap"]["detail_level"] == "detailed"
    assert config["series_recap"]["target_total_min_s"] == 2100
    assert config["series_recap"]["target_total_max_s"] == 2700

def test_localvision_anime_series_preset_is_optional_and_capped() -> None:
    config = load_config(Path("config.anime.series.localvision.yaml"))

    assert config["ingest"]["translation_required"] is True
    assert config["ingest"]["vision_provider"] == "local_qwen2_5_vl"
    assert config["ingest"]["vision_model"] == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert config["ingest"]["max_vision_frames"] == 30
    assert config["ingest"]["vision_resize_long_edge"] == 768
    assert config["ingest"]["vision_batch_size"] == 1

def test_manifest_rejects_missing_and_duplicate_source_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    missing.write_text(
        json.dumps({"series_id": "show", "episodes": [{"episode_key": "e01", "episode_number": 1}]}),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="source_path"):
        manifest_episode_specs(missing)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        json.dumps(
            {
                "series_id": "show",
                "episodes": [
                    {"episode_key": "e01", "episode_number": 1, "source_path": "same.mp4"},
                    {"episode_key": "e02", "episode_number": 2, "source_path": "same.mp4"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="duplicate source_path"):
        manifest_episode_specs(duplicate)

def test_series_recap_dry_run_shows_episode_and_final_stages_without_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest_path = tmp_path / "series_manifest.json"
    config_path = tmp_path / "config.json"
    run_dir = tmp_path / "runs" / "grand-blue-s03"
    write_manifest(manifest_path, tmp_path / "Grand_Blue.S03E01.mp4", tmp_path / "Grand_Blue.S03E02.mp4")
    write_config(config_path)
    commands: list[list[str]] = []

    args = argparse.Namespace(
        manifest=manifest_path,
        config=config_path,
        episodes="1-2",
        run_dir=run_dir,
        python="python",
        dry_run=True,
        force=False,
        force_final=False,
        log_level="ERROR",
    )

    assert run_series_recap(args, executor=lambda command, _log_path: commands.append(command)) == 0
    output = capsys.readouterr().out

    assert commands == []
    assert "[planned] s03e01:episode_planner" in output
    assert "python run.py" in output
    assert "python -m series_composer" in output
    assert "--format episode_arc_chaptered" in output
    assert "--detail-level detailed" in output
    assert "--target-total-min-s 2100" in output
    assert "--target-total-max-s 2700" in output
    assert "--target-total-hard-cap-s 3000" in output
    assert "--arc-size 3" in output
    assert "--mode-target-ratio quick=0.14" in output
    assert "series_chapters.json" in output
    assert "series_arc_plan.json" in output
    assert "series_composer.qa.json" in output
    assert "[planned] youtube_chapters" in output
    assert "youtube_chapters.txt" in output
    assert "python -m series_match" in output
    assert "--source-map" in output
    assert not (run_dir / "series_recap" / "summary.json").exists()
    assert not (run_dir / "series_recap" / "work" / "episode_configs" / "s03e01.json").exists()

def test_series_recap_practical_dry_run_plans_12_episode_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest_path = tmp_path / "series_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "series_id": "solo-leveling-s01",
                "series_title": "Solo Leveling Season 1",
                "season": 1,
                "episodes": [
                    {
                        "episode_key": f"s01e{episode:02d}",
                        "episode_number": episode,
                        "title": f"Episode {episode}",
                        "source_path": str(tmp_path / f"Solo_Leveling.S01E{episode:02d}.mp4"),
                        "spoiler_limit_episode": episode,
                    }
                    for episode in range(1, 13)
                ],
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        manifest=manifest_path,
        config=Path("config.anime.series.practical.yaml"),
        episodes="1-12",
        run_dir=tmp_path / "runs" / "solo-leveling-s01",
        python="python",
        dry_run=True,
        force=False,
        force_final=False,
        log_level="ERROR",
    )

    assert run_series_recap(args, executor=lambda _command, _log_path: None) == 0
    output = capsys.readouterr().out

    assert output.count(":episode_planner") == 12
    assert output.count(":episode_shots") == 12
    assert "[planned] series_composer" in output
    assert "[planned] tts" in output
    assert "[planned] series_match" in output
    assert "[planned] render" in output

def test_episode_config_auto_discovers_manual_ranges_sidecar(tmp_path: Path) -> None:
    manifest_path = tmp_path / "series_manifest.json"
    manual_ranges = tmp_path / "manual_ranges.s03e01.yaml"
    write_manifest(manifest_path, tmp_path / "Grand_Blue.S03E01.mp4", tmp_path / "Grand_Blue.S03E02.mp4")
    manual_ranges.write_text("non_story_ranges: []\n", encoding="utf-8")
    _manifest, specs = manifest_episode_specs(manifest_path)

    config = episode_config_for(
        base_config={},
        manifest_path=manifest_path,
        spec=specs[0],
        series_memory_dir=tmp_path / "series_memory",
    )

    assert config["preflight"]["manual_ranges"] == str(manual_ranges.resolve())

def test_write_youtube_chapters_from_series_chapters_and_timings(tmp_path: Path) -> None:
    chapters_path = tmp_path / "series_chapters.json"
    timings_path = tmp_path / "beats_timing.json"
    output_path = tmp_path / "youtube_chapters.txt"
    chapters_path.write_text(
        json.dumps(
            [
                {"title": "Mo dau", "start_beat_id": 0, "episode_key": None},
                {"title": "Tap 1", "start_beat_id": 1, "episode_key": "s03e01"},
                {"title": "Tap 2", "start_beat_id": 3, "episode_key": "s03e02"},
            ]
        ),
        encoding="utf-8",
    )
    timings_path.write_text(
        json.dumps(
            [
                {"beat_id": 0, "audio_path": "audio/0.mp3", "tl_start": 0.0, "tl_end": 5.0, "duration": 5.0},
                {"beat_id": 1, "audio_path": "audio/1.mp3", "tl_start": 5.15, "tl_end": 70.15, "duration": 65.0},
                {"beat_id": 2, "audio_path": "audio/2.mp3", "tl_start": 70.3, "tl_end": 120.3, "duration": 50.0},
                {"beat_id": 3, "audio_path": "audio/3.mp3", "tl_start": 120.45, "tl_end": 180.45, "duration": 60.0},
            ]
        ),
        encoding="utf-8",
    )

    write_youtube_chapters(chapters_path=chapters_path, beats_timing_path=timings_path, output_path=output_path)

    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "00:00 Mo dau",
        "00:05 Tap 1",
        "02:00 Tap 2",
    ]
