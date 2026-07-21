from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from series_recap.__main__ import episode_config_for, manifest_episode_specs, run_series_recap, select_episodes

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
                }
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
    assert "python -m series_match" in output
    assert "--source-map" in output
    assert not (run_dir / "series_recap" / "summary.json").exists()
    assert not (run_dir / "series_recap" / "work" / "episode_configs" / "s03e01.json").exists()

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
