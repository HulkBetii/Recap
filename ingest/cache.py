from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class StageCache:
    def __init__(self, work_dir: Path, force: bool = False) -> None:
        self.work_dir = work_dir
        self.force = force
        self.cache_hits: list[str] = []
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def path(self, relative_path: str) -> Path:
        return self.work_dir / relative_path

    def prepare(self) -> None:
        if self.force and self.work_dir.exists():
            for name in (
                "audio.wav",
                "transcript_raw.json",
                "transcript_text.json",
                "transcript_aligned.json",
                "transcript_quality.json",
                "transcript_corrected.json",
                "translated.json",
                "vision.json",
            ):
                target = self.work_dir / name
                if target.exists():
                    target.unlink()
            openai_chunks = self.work_dir / "openai_chunks"
            if openai_chunks.exists():
                shutil.rmtree(openai_chunks)
            local_asr_chunks = self.work_dir / "local_asr_chunks"
            if local_asr_chunks.exists():
                shutil.rmtree(local_asr_chunks)
            frames = self.work_dir / "frames"
            if frames.exists():
                shutil.rmtree(frames)
        (self.work_dir / "frames").mkdir(parents=True, exist_ok=True)

    def has(self, relative_path: str) -> bool:
        path = self.path(relative_path)
        exists = path.exists()
        if exists:
            self.cache_hits.append(relative_path)
        return exists

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.path(relative_path).read_text(encoding="utf-8"))

    def write_json(self, relative_path: str, data: object) -> None:
        path = self.path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, BaseModel):
            text = data.model_dump_json(indent=2)
        elif isinstance(data, list) and all(isinstance(item, BaseModel) for item in data):
            text = "[\n" + ",\n".join(item.model_dump_json(indent=2) for item in data) + "\n]"
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        path.write_text(text + "\n", encoding="utf-8")
