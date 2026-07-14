from __future__ import annotations

from collections import deque


STAGES = (
    "probe",
    "analyze",
    "shots",
    "stems",
    "segment",
    "plan",
    "write",
    "tts",
    "compose",
    "render",
    "qa",
)

DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "probe": (),
    "analyze": ("probe",),
    "shots": ("probe",),
    "stems": ("probe",),
    "segment": ("analyze", "shots"),
    "plan": ("segment",),
    "write": ("plan",),
    "tts": ("write",),
    "compose": ("segment", "plan", "tts", "stems"),
    "render": ("compose",),
    "qa": ("render",),
}


def _build_downstream() -> dict[str, tuple[str, ...]]:
    downstream: dict[str, list[str]] = {stage: [] for stage in STAGES}
    for stage, dependencies in DEPENDENCIES.items():
        for dependency in dependencies:
            downstream[dependency].append(stage)
    return {stage: tuple(values) for stage, values in downstream.items()}


DOWNSTREAM = _build_downstream()


def stage_range(
    from_stage: str | None = None,
    to_stage: str | None = None,
    only: str | None = None,
) -> set[str]:
    if only is not None:
        if from_stage is not None or to_stage is not None:
            raise ValueError("--only cannot be combined with --from or --to")
        return {only}
    start = STAGES.index(from_stage) if from_stage else 0
    end = STAGES.index(to_stage) if to_stage else len(STAGES) - 1
    if start > end:
        raise ValueError("--from stage must not come after --to stage")
    return set(STAGES[start : end + 1])


def downstream_closure(stage: str) -> set[str]:
    found: set[str] = set()
    pending = deque([stage])
    while pending:
        current = pending.popleft()
        if current in found:
            continue
        found.add(current)
        pending.extend(DOWNSTREAM[current])
    return found


def dependency_closure(stage: str) -> set[str]:
    found: set[str] = set()
    pending = deque(DEPENDENCIES[stage])
    while pending:
        current = pending.popleft()
        if current in found:
            continue
        found.add(current)
        pending.extend(DEPENDENCIES[current])
    return found


def forced_stages(selected: set[str], force: bool, force_stage: list[str]) -> set[str]:
    if force:
        return set(selected)
    forced: set[str] = set()
    for stage in force_stage:
        forced.update(downstream_closure(stage))
    return forced & selected


def missing_selected_dependencies(stage: str, selected: set[str]) -> set[str]:
    return {dependency for dependency in DEPENDENCIES[stage] if dependency not in selected}
