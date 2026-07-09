from __future__ import annotations

from pathlib import Path

from orchestrator.graph import STAGES, build_paths
from orchestrator.runner import build_command


def test_tts_align_stage_is_between_tts_and_shots() -> None:
    assert STAGES.index("tts") < STAGES.index("tts_align") < STAGES.index("shots")


def test_tts_align_command_wires_outputs(tmp_path: Path) -> None:
    paths = build_paths(tmp_path / "run")
    command = build_command("tts_align", paths, tmp_path / "film.mp4", {"tts_align": {"mode": "auto", "aligner": "none"}}, force=False, python_exe="python")
    assert command[:2] == ["python", "-m"]
    assert command[2] == "tts_align"
    assert str(paths.review_micro) in command
    assert str(paths.micro_policy) in command
    assert "--mode" in command and command[command.index("--mode") + 1] == "auto"
