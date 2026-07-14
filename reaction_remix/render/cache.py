from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from common.integrity import atomic_write_json, stable_hash


class RemixRenderCache:
    def __init__(self, work_dir: Path, *, force: bool = False) -> None:
        self.work_dir = work_dir
        self.video_dir = work_dir / "video_clips"
        self.audio_dir = work_dir / "audio_clips"
        self.force = force
        self.cache_hits: list[str] = []

    def prepare(self) -> None:
        if self.force and self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    def video_path(self, identity: object) -> tuple[str, Path]:
        key = stable_hash(identity)
        return key, self.video_dir / f"{key}.mp4"

    def audio_path(self, identity: object) -> tuple[str, Path]:
        key = stable_hash(identity)
        return key, self.audio_dir / f"{key}.wav"

    def use(self, path: Path) -> list[list[str]] | None:
        if not path.is_file() or path.stat().st_size <= 0:
            return None
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".commands.json").unlink(missing_ok=True)
            return None
        command_path = path.with_suffix(path.suffix + ".commands.json")
        try:
            payload = json.loads(command_path.read_text(encoding="utf-8"))
            commands = payload["commands"]
            if not isinstance(commands, list) or not all(isinstance(item, list) for item in commands):
                return None
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None
        self.cache_hits.append(path.relative_to(self.work_dir).as_posix())
        return [[str(arg) for arg in command] for command in commands]

    def record_commands(self, path: Path, commands: list[list[str]]) -> None:
        command_path = path.with_suffix(path.suffix + ".commands.json")
        atomic_write_json(command_path, {"commands": commands})
