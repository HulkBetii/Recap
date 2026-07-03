from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

STAGES = ("ingest", "review", "tts", "shots", "match", "render")
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "ingest": (),
    "review": ("ingest",),
    "tts": ("review",),
    "shots": (),
    "match": ("review", "tts", "shots"),
    "render": ("match", "tts"),
}
DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "ingest": ("review", "tts", "match", "render"),
    "review": ("tts", "match", "render"),
    "tts": ("match", "render"),
    "shots": ("match", "render"),
    "match": ("render",),
    "render": (),
}

@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    film_map: Path
    film_map_meta: Path
    review_script: Path
    review_meta: Path
    review_meta_alias: Path
    voiceover: Path
    beats_timing: Path
    tts_meta: Path
    audio_dir: Path
    shots: Path
    shots_meta: Path
    shots_dir: Path
    edl: Path
    edl_meta: Path
    edl_qa: Path
    recap: Path
    render_meta: Path
    work_dir: Path
    run_log: Path
    summary: Path


def build_paths(run_dir: Path) -> RunPaths:
    return RunPaths(
        run_dir=run_dir,
        film_map=run_dir / "film_map.json",
        film_map_meta=run_dir / "film_map.meta.json",
        review_script=run_dir / "review_script.json",
        review_meta=run_dir / "review_script.meta.json",
        review_meta_alias=run_dir / "review_meta.json",
        voiceover=run_dir / "voiceover.mp3",
        beats_timing=run_dir / "beats_timing.json",
        tts_meta=run_dir / "tts_meta.json",
        audio_dir=run_dir / "audio",
        shots=run_dir / "shots.json",
        shots_meta=run_dir / "shots.meta.json",
        shots_dir=run_dir / "shots",
        edl=run_dir / "edl.json",
        edl_meta=run_dir / "edl.meta.json",
        edl_qa=run_dir / "edl.qa.json",
        recap=run_dir / "recap.mp4",
        render_meta=run_dir / "render.meta.json",
        work_dir=run_dir / "work",
        run_log=run_dir / "run.log",
        summary=run_dir / "summary.json",
    )


def validate_stage_name(name: str) -> str:
    if name not in STAGES:
        raise ValueError(f"unknown stage: {name}")
    return name


def stage_range(from_stage: str | None = None, to_stage: str | None = None, only: str | None = None) -> set[str]:
    if only:
        validate_stage_name(only)
        if from_stage or to_stage:
            raise ValueError("--only cannot be combined with --from or --to")
        return {only}
    start = STAGES.index(validate_stage_name(from_stage)) if from_stage else 0
    end = STAGES.index(validate_stage_name(to_stage)) if to_stage else len(STAGES) - 1
    if start > end:
        raise ValueError("--from stage must not come after --to stage")
    selected = set(STAGES[start:end + 1])
    return selected


def forced_stages(selected: set[str], force: bool, force_stage: list[str]) -> set[str]:
    if force:
        return set(selected)
    forced: set[str] = set()
    for stage in force_stage:
        validate_stage_name(stage)
        forced.add(stage)
        forced.update(DOWNSTREAM[stage])
    return forced & selected
