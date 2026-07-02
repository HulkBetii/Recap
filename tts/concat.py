from __future__ import annotations

from pathlib import Path

from common.media import concat_audio, generate_silence


def build_concat_inputs(audio_paths: list[Path], pause_s: float, work_dir: Path) -> list[Path]:
    if pause_s <= 0 or len(audio_paths) <= 1:
        return audio_paths
    silence_path = work_dir / "silence.mp3"
    generate_silence(silence_path, pause_s)
    inputs: list[Path] = []
    for index, audio_path in enumerate(audio_paths):
        if index > 0:
            inputs.append(silence_path)
        inputs.append(audio_path)
    return inputs


def concat_voiceover(audio_paths: list[Path], pause_s: float, work_dir: Path, output_path: Path) -> None:
    concat_audio(build_concat_inputs(audio_paths, pause_s, work_dir), output_path)
