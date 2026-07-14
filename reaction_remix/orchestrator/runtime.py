from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

from common.media import require_ffmpeg


class ReactionOrchestratorError(RuntimeError):
    pass


class StageExecutionError(ReactionOrchestratorError):
    def __init__(self, message: str, *, returncode: int, log_path: Path) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.log_path = log_path


def _require_module(name: str, extra: str) -> None:
    if importlib.util.find_spec(name) is None:
        raise ReactionOrchestratorError(
            f"required module {name!r} is unavailable; install the project with [{extra}]"
        )


def validate_runtime_requirements(
    film: Path,
    selected: set[str],
    config: dict,
    *,
    dry_run: bool = False,
) -> None:
    if not film.is_file():
        raise ReactionOrchestratorError(f"input video does not exist: {film}")
    if dry_run or not config["orchestrator"]["runtime_preflight"]:
        return
    if selected & {"probe", "analyze", "shots", "stems", "tts", "render", "qa"}:
        require_ffmpeg()
    if "analyze" in selected:
        for module in ("faster_whisper", "lingua", "sklearn", "speechbrain", "torch", "torchaudio"):
            _require_module(module, "reaction-analysis")
    if "stems" in selected and config["stems"]["enabled"]:
        _require_module("demucs", "reaction-audio")
    if "tts" in selected and not os.environ.get("VIVOO_API_KEY"):
        raise ReactionOrchestratorError("VIVOO_API_KEY is required for strict AI33 reaction-remix TTS")
    if selected & {"plan", "write"}:
        profile = Path(config["plan"]["chatgpt_profile_dir"])
        if not profile.is_dir():
            raise ReactionOrchestratorError(f"ChatGPT persistent profile does not exist: {profile}")
    cuda_stages = {
        stage
        for stage, section in (("analyze", "analyze"), ("stems", "stems"))
        if stage in selected and config[section].get("device") == "cuda"
    }
    if cuda_stages:
        import torch

        if not torch.cuda.is_available():
            raise ReactionOrchestratorError(
                f"CUDA is required by configured stage(s): {', '.join(sorted(cuda_stages))}"
            )


def execute(command: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    body = ""
    if result.stdout:
        body += result.stdout
    if result.stderr:
        body += result.stderr
    log_path.write_text(body, encoding="utf-8")
    if result.returncode != 0:
        last_line = next((line for line in reversed(body.splitlines()) if line.strip()), "stage failed")
        raise StageExecutionError(
            f"command failed with exit code {result.returncode}: {last_line} (log: {log_path})",
            returncode=result.returncode,
            log_path=log_path,
        )
