from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.integrity import atomic_write_json


@dataclass
class ReactionStageSummary:
    stage: str
    status: str
    command: list[str]
    outputs: list[str]
    cache_hit: bool = False
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class ReactionRunSummary:
    source: str
    run_dir: str
    stages: list[ReactionStageSummary] = field(default_factory=list)
    repair_rounds: list[dict[str, Any]] = field(default_factory=list)
    final_qa_status: str | None = None
    deliverable: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def write(self, path: Path) -> None:
        payload = asdict(self)
        atomic_write_json(path, payload)

