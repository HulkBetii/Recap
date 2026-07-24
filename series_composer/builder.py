from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from common.inputs import load_series_manifest
from common.narration_qa import (
    BLOCKING_NARRATION_QA_CODES,
    analyze_narration_content,
    safe_summary_fragment as qa_safe_summary_fragment,
)
from common.schema import (
    EpisodeTargetPlan,
    EpisodeMemory,
    EpisodeMeta,
    FilmMapMeta,
    FilmMapSegment,
    ReviewBeat,
    SeasonTargetPlan,
    SeriesArcPlan,
    SeriesChapter,
    SeriesComposerQa,
    SeriesEvent,
    SeriesEventBank,
    SeriesManifest,
    SeriesRecapDetailLevel,
    SeriesRecapFormat,
    SeriesReviewBeat,
    SeriesReviewMeta,
    SeriesSourceRef,
    StorySection,
    VideoProfile,
    validate_series_review_script,
)
from review.json_utils import extract_json

MODE_TARGET_RATIOS = {
    "full": 0.12,
    "quick": 0.06,
    "merge": 0.015,
    "skip": 0.0,
}
REVISION_QA_CODES = {
    "empty_script",
    "missing_hook",
    "repeated_events",
    "under_target_length",
    "missing_episode_chapter",
    "episode_under_char_budget",
    "arc_under_char_budget",
    "season_under_char_budget",
    "estimated_duration_exceeds_hard_cap",
    "arc_exceeds_hard_char_budget",
    "non_monotonic_episode_order",
    "non_monotonic_story_order",
    "foreign_language_in_narration",
    "unaccented_vietnamese_narration",
    *BLOCKING_NARRATION_QA_CODES,
}
NON_CANON_TITLE_HINTS = ("ova", "bonus", "special", "recap-only", "recap only")
NON_STORY_EVENT_TYPES = {
    "non_story",
    "opening_theme",
    "ending_theme",
    "next_episode_preview",
    "recap_previous_episode",
    "sponsor_card",
    "studio_logo",
}
DETERMINISTIC_FALLBACK_CHAR_SCALE = 0.86
PLACEHOLDER_EVENT_ID_MARKERS = ("...", "…")
MAX_FINAL_STITCH_PROMPT_CHARS = 20_000

@dataclass(frozen=True)
class SeasonTargetSettings:
    detail_level: SeriesRecapDetailLevel = "standard"
    target_total_min_s: float = 2100.0
    target_total_max_s: float = 2700.0
    target_total_hard_cap_s: float = 3000.0
    episode_min_s: float = 90.0
    episode_normal_s: float = 180.0
    episode_high_s: float = 300.0
    arc_size: int = 3

    def validate(self) -> "SeasonTargetSettings":
        if self.target_total_min_s < 0 or self.target_total_max_s < 0 or self.target_total_hard_cap_s < 0:
            raise ValueError("season target bounds must be >= 0")
        if self.target_total_max_s and self.target_total_max_s < self.target_total_min_s:
            raise ValueError("target_total_max_s must be >= target_total_min_s")
        if self.target_total_hard_cap_s and self.target_total_max_s and self.target_total_hard_cap_s < self.target_total_max_s:
            raise ValueError("target_total_hard_cap_s must be >= target_total_max_s")
        if self.episode_min_s < 0 or self.episode_normal_s < 0 or self.episode_high_s < 0:
            raise ValueError("episode target seconds must be >= 0")
        if self.episode_high_s and self.episode_high_s < self.episode_min_s:
            raise ValueError("episode_high_s must be >= episode_min_s")
        if self.episode_normal_s and self.episode_high_s and self.episode_normal_s > self.episode_high_s:
            raise ValueError("episode_normal_s must be <= episode_high_s")
        if self.arc_size <= 0:
            raise ValueError("arc_size must be > 0")
        return self


@dataclass(frozen=True)
class ComposerLengthPlan:
    min_total_chars: int
    max_total_chars: int
    min_beats: int
    max_beats: int
    per_beat_target_chars: int
    per_beat_min_chars: int
    per_beat_max_chars: int


class ChatClient(Protocol):
    async def ask(self, prompt: str) -> str:
        ...


@dataclass(frozen=True)
class EpisodeArtifacts:
    episode_key: str
    run_dir: Path
    episode_meta: Path
    episode_memory: Path
    film_map: Path
    film_map_meta: Path
    story_map: Path
    video_profile: Path | None
    shots: Path


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def event_src_key(episode_key: str, source_path: str) -> str:
    return f"{episode_key}/{Path(source_path).name}"


def story_duration(duration_s: float, profile: VideoProfile | None) -> float:
    if profile is None:
        return duration_s
    non_story = 0.0
    for item in profile.non_story_ranges:
        non_story += max(0.0, min(duration_s, item.end_s) - max(0.0, item.start_s))
    return max(0.0, duration_s - non_story)

def target_base_duration(duration_s: float, profile: VideoProfile | None, recap_format: SeriesRecapFormat) -> float:
    if recap_format in {"episode_chaptered", "episode_arc_chaptered"}:
        return duration_s
    return story_duration(duration_s, profile)

def episode_target_beats(
    *,
    char_budget: int,
    event_count: int,
    recap_format: SeriesRecapFormat,
) -> int:
    if char_budget <= 0 or event_count <= 0:
        return 0
    if recap_format == "episode_arc_chaptered":
        return min(event_count, max(1, round(char_budget / 700) or 1))
    if recap_format == "episode_chaptered":
        return min(event_count, max(2, round(char_budget / 520) or 1))
    return min(event_count, max(1, round(char_budget / 360) or 1))

def chaptered_episode_targets(bank: SeriesEventBank) -> list[EpisodeTargetPlan]:
    return [target for target in bank.episode_targets if target.char_budget > 0 and target.target_beats > 0]

def is_chaptered_format(recap_format: SeriesRecapFormat) -> bool:
    return recap_format in {"episode_chaptered", "episode_arc_chaptered"}

def is_non_canon_episode(target: EpisodeTargetPlan) -> bool:
    text = " ".join(str(value).lower() for value in (target.episode_key, target.title or "") if value)
    return any(hint in text for hint in NON_CANON_TITLE_HINTS)

def detailed_episode_seconds(target: EpisodeTargetPlan, settings: SeasonTargetSettings) -> float:
    if target.recap_mode == "skip" or target.event_count <= 0 or is_non_canon_episode(target):
        return 0.0
    base_by_mode = {
        "full": settings.episode_high_s,
        "quick": settings.episode_normal_s,
        "merge": settings.episode_min_s,
        "skip": 0.0,
    }
    base = base_by_mode[target.recap_mode]
    event_factor = min(1.0, target.event_count / 8.0)
    factor = 0.78 + target.importance_score * 0.30 + target.continuity_dependency * 0.16 + event_factor * 0.08
    target_s = base * factor
    if target.recap_mode == "merge":
        upper = settings.episode_normal_s or settings.episode_high_s or target_s
        target_s = min(target_s, upper)
    elif settings.episode_high_s:
        target_s = min(target_s, settings.episode_high_s)
    return max(settings.episode_min_s, target_s)

def scale_episode_targets(
    targets: list[EpisodeTargetPlan],
    *,
    desired_total_s: float,
    settings: SeasonTargetSettings,
    tts_cps: float,
) -> list[EpisodeTargetPlan]:
    adjustable = [target for target in targets if target.target_video_s > 0]
    current_total = sum(target.target_video_s for target in adjustable)
    if not adjustable or current_total <= 0 or desired_total_s <= 0:
        return targets
    minimum_total = settings.episode_min_s * len(adjustable)
    if desired_total_s + 1e-6 < minimum_total:
        raise ValueError(
            "season target cannot be met: "
            f"desired {desired_total_s:.3f}s is below the episode minimum floor {minimum_total:.3f}s"
        )

    # Scale toward the requested season bound while preserving the minimum chapter
    # duration for every included episode. A plain proportional scale can leave the
    # result above the bound when several episodes hit that floor.
    if desired_total_s < current_total:
        floor = settings.episode_min_s
        reducible_total = sum(max(0.0, target.target_video_s - floor) for target in adjustable)
        extra_total = max(0.0, desired_total_s - minimum_total)
        factor = extra_total / reducible_total if reducible_total > 0 else 0.0
        target_seconds = {
            target.episode_key: floor + max(0.0, target.target_video_s - floor) * factor
            for target in adjustable
        }
    else:
        factor = desired_total_s / current_total
        target_seconds = {
            target.episode_key: max(settings.episode_min_s, target.target_video_s * factor)
            for target in adjustable
        }

    # Keep rounded episode values from drifting above a hard season bound.
    rounded_total = sum(round(value, 3) for value in target_seconds.values())
    rounding_delta = round(desired_total_s - rounded_total, 3)
    if rounding_delta:
        largest = max(adjustable, key=lambda target: target_seconds[target.episode_key])
        target_seconds[largest.episode_key] = max(
            settings.episode_min_s,
            target_seconds[largest.episode_key] + rounding_delta,
        )

    updated: list[EpisodeTargetPlan] = []
    for target in targets:
        if target.target_video_s <= 0:
            updated.append(target)
            continue
        target_s = max(settings.episode_min_s, target_seconds[target.episode_key])
        char_budget = max(1, int(round(target_s * tts_cps)))
        updated.append(
            target.model_copy(
                update={
                    "target_video_s": round(target_s, 3),
                    "char_budget": char_budget,
                    "min_chars": max(1, int(round(char_budget * 0.75))),
                    "target_beats": episode_target_beats(
                        char_budget=char_budget,
                        event_count=target.event_count,
                        recap_format="episode_arc_chaptered",
                    ),
                }
            )
        )
    return updated

def group_episode_targets(targets: list[EpisodeTargetPlan], arc_size: int) -> list[list[EpisodeTargetPlan]]:
    if not targets:
        return []
    arc_size = max(1, arc_size)
    distinct_arcs = [arc for arc in dict.fromkeys(target.arc for target in targets if target.arc)]
    groups: list[list[EpisodeTargetPlan]] = []
    if len(distinct_arcs) > 1:
        current: list[EpisodeTargetPlan] = []
        current_arc: str | None = None
        for target in targets:
            target_arc = target.arc
            if current and target_arc != current_arc:
                groups.append(current)
                current = []
            current.append(target)
            current_arc = target_arc
        if current:
            groups.append(current)
    else:
        groups = [targets[index : index + arc_size] for index in range(0, len(targets), arc_size)]

    split_groups: list[list[EpisodeTargetPlan]] = []
    for group in groups:
        if len(group) <= arc_size:
            split_groups.append(group)
            continue
        split_groups.extend(group[index : index + arc_size] for index in range(0, len(group), arc_size))
    return split_groups

def arc_title(group: list[EpisodeTargetPlan], arc_index: int) -> str:
    arcs = [target.arc for target in group if target.arc]
    if arcs and len(set(arcs)) == 1:
        return arcs[0] or f"Arc {arc_index}"
    first = group[0]
    last = group[-1]
    if first.episode_number is not None and last.episode_number is not None:
        return f"Tap {first.episode_number}-{last.episode_number}"
    return f"Arc {arc_index}"

def build_arc_plans(targets: list[EpisodeTargetPlan], settings: SeasonTargetSettings) -> list[SeriesArcPlan]:
    arcs: list[SeriesArcPlan] = []
    for index, group in enumerate(group_episode_targets(targets, settings.arc_size), start=1):
        target_video_s = round(sum(target.target_video_s for target in group), 3)
        char_budget = sum(target.char_budget for target in group)
        arcs.append(
            SeriesArcPlan(
                arc_id=f"arc-{index:02d}",
                title=arc_title(group, index),
                episode_keys=[target.episode_key for target in group],
                target_video_s=target_video_s,
                char_budget=char_budget,
                min_chars=sum(target.min_chars for target in group),
                target_beats=sum(target.target_beats for target in group),
                episodes=group,
            )
        )
    return arcs

def build_season_target_plan(
    *,
    recap_format: SeriesRecapFormat,
    episode_targets: list[EpisodeTargetPlan],
    tts_cps: float,
    settings: SeasonTargetSettings,
) -> tuple[list[EpisodeTargetPlan], SeasonTargetPlan]:
    settings = settings.validate()
    warnings: list[str] = []
    if recap_format != "episode_arc_chaptered":
        total_target_video_s = round(sum(target.target_video_s for target in episode_targets), 3)
        total_char_budget = sum(target.char_budget for target in episode_targets)
        standard_settings = SeasonTargetSettings(
            detail_level="standard",
            target_total_min_s=0.0,
            target_total_max_s=0.0,
            target_total_hard_cap_s=0.0,
            episode_min_s=0.0,
            episode_normal_s=0.0,
            episode_high_s=0.0,
            arc_size=max(1, settings.arc_size),
        )
        arcs = build_arc_plans(episode_targets, standard_settings)
        return episode_targets, SeasonTargetPlan(
            recap_format=recap_format,
            detail_level="standard",
            target_total_min_s=0.0,
            target_total_max_s=0.0,
            target_total_hard_cap_s=0.0,
            episode_min_s=0.0,
            episode_normal_s=0.0,
            episode_high_s=0.0,
            arc_size=standard_settings.arc_size,
            total_target_video_s=total_target_video_s,
            total_char_budget=total_char_budget,
            min_total_chars=max(0, int(round(total_char_budget * 0.85))),
            max_total_chars=max(0, int(round(total_char_budget * 1.15))),
            episode_count=len(episode_targets),
            arc_count=len(arcs),
            arcs=arcs,
            warnings=[],
        )

    detailed_targets: list[EpisodeTargetPlan] = []
    for target in episode_targets:
        target_s = detailed_episode_seconds(target, settings)
        if target.event_count <= 0 and target.recap_mode != "skip":
            warnings.append(f"episode {target.episode_key} has no usable story events; assigned zero season budget")
        char_budget = max(0, int(round(target_s * tts_cps)))
        detailed_targets.append(
            target.model_copy(
                update={
                    "target_video_s": round(target_s, 3),
                    "char_budget": char_budget,
                    "min_chars": max(0, int(round(char_budget * 0.75))),
                    "target_beats": episode_target_beats(
                        char_budget=char_budget,
                        event_count=target.event_count,
                        recap_format=recap_format,
                    ),
                }
            )
        )

    targetable_count = sum(1 for target in detailed_targets if target.target_video_s > 0)
    total_target_video_s = sum(target.target_video_s for target in detailed_targets)
    # Do not inflate small 1-4 episode test ranges to full-season length. The 35-45 minute
    # clamp applies once the selected scope looks like a real season.
    enforce_season_bounds = targetable_count >= max(8, settings.arc_size * 2)
    if enforce_season_bounds and settings.target_total_min_s and total_target_video_s < settings.target_total_min_s:
        detailed_targets = scale_episode_targets(
            detailed_targets,
            desired_total_s=settings.target_total_min_s,
            settings=settings,
            tts_cps=tts_cps,
        )
        warnings.append("season target raised to target_total_min_s")
    total_target_video_s = sum(target.target_video_s for target in detailed_targets)
    if enforce_season_bounds and settings.target_total_max_s and total_target_video_s > settings.target_total_max_s:
        detailed_targets = scale_episode_targets(
            detailed_targets,
            desired_total_s=settings.target_total_max_s,
            settings=settings,
            tts_cps=tts_cps,
        )
        warnings.append("season target lowered to target_total_max_s")
        total_target_video_s = sum(target.target_video_s for target in detailed_targets)
    if settings.target_total_hard_cap_s and total_target_video_s > settings.target_total_hard_cap_s:
        detailed_targets = scale_episode_targets(
            detailed_targets,
            desired_total_s=settings.target_total_hard_cap_s,
            settings=settings,
            tts_cps=tts_cps,
        )
        warnings.append("season target lowered to target_total_hard_cap_s")
        total_target_video_s = sum(target.target_video_s for target in detailed_targets)
    if settings.target_total_hard_cap_s and total_target_video_s > settings.target_total_hard_cap_s + 1e-3:
        raise ValueError(
            "season target exceeds target_total_hard_cap_s after scaling: "
            f"{total_target_video_s:.3f}s > {settings.target_total_hard_cap_s:.3f}s"
        )

    arcs = build_arc_plans(detailed_targets, settings)
    total_target_video_s = round(sum(target.target_video_s for target in detailed_targets), 3)
    total_char_budget = sum(target.char_budget for target in detailed_targets)
    return detailed_targets, SeasonTargetPlan(
        recap_format=recap_format,
        detail_level=settings.detail_level,
        target_total_min_s=settings.target_total_min_s,
        target_total_max_s=settings.target_total_max_s,
        target_total_hard_cap_s=settings.target_total_hard_cap_s,
        episode_min_s=settings.episode_min_s,
        episode_normal_s=settings.episode_normal_s,
        episode_high_s=settings.episode_high_s,
        arc_size=settings.arc_size,
        total_target_video_s=total_target_video_s,
        total_char_budget=total_char_budget,
        min_total_chars=max(0, int(round(total_char_budget * 0.85))),
        max_total_chars=max(0, int(round(total_char_budget * 1.15))),
        episode_count=len(detailed_targets),
        arc_count=len(arcs),
        arcs=arcs,
        warnings=warnings,
    )


def section_segment_span(section: StorySection, film_map: list[FilmMapSegment]) -> tuple[int, int]:
    if section.segment_ids:
        return min(section.segment_ids), max(section.segment_ids)
    overlapping = [
        segment.id
        for segment in film_map
        if max(segment.tc_start, section.tc_start) < min(segment.tc_end, section.tc_end)
    ]
    if overlapping:
        return min(overlapping), max(overlapping)
    nearest = min(film_map, key=lambda segment: abs(segment.tc_start - section.tc_start))
    return nearest.id, nearest.id


def overlaps_non_story(start_s: float, end_s: float, profile: VideoProfile | None) -> bool:
    if profile is None:
        return False
    for item in profile.non_story_ranges:
        if max(start_s, item.start_s) < min(end_s, item.end_s):
            return True
    return False


def event_importance(section: StorySection, episode_meta: EpisodeMeta) -> float:
    base = {
        "setup": 0.45,
        "inciting_incident": 0.75,
        "conflict": 0.65,
        "investigation": 0.55,
        "reveal": 0.9,
        "climax": 1.0,
        "ending": 0.8,
        "non_story": 0.0,
    }.get(section.type, 0.5)
    mode_bonus = {"full": 0.08, "quick": 0.0, "merge": -0.15, "skip": -0.35}[episode_meta.recap_mode]
    return round(max(0.0, min(1.0, base + mode_bonus + episode_meta.importance_score * 0.15)), 4)


def build_episode_artifacts(episode_key: str, run_dir: Path) -> EpisodeArtifacts:
    return EpisodeArtifacts(
        episode_key=episode_key,
        run_dir=run_dir,
        episode_meta=run_dir / "episode_meta.json",
        episode_memory=run_dir / "episode_memory.json",
        film_map=run_dir / "film_map.json",
        film_map_meta=run_dir / "film_map.meta.json",
        story_map=run_dir / "story_map.json",
        video_profile=run_dir / "video_profile.json" if (run_dir / "video_profile.json").is_file() else None,
        shots=run_dir / "shots.json",
    )


def build_event_bank(
    *,
    manifest_path: Path,
    episode_run_dirs: dict[str, Path],
    tts_cps: float = 15.0,
    mode_target_ratios: dict[str, float] | None = None,
    recap_format: SeriesRecapFormat = "compact",
    detail_level: SeriesRecapDetailLevel = "standard",
    target_total_min_s: float = 2100.0,
    target_total_max_s: float = 2700.0,
    target_total_hard_cap_s: float = 3000.0,
    episode_min_s: float = 90.0,
    episode_normal_s: float = 180.0,
    episode_high_s: float = 300.0,
    arc_size: int = 3,
) -> SeriesEventBank:
    manifest = load_series_manifest(manifest_path)
    ratios = {**MODE_TARGET_RATIOS, **(mode_target_ratios or {})}
    target_settings = SeasonTargetSettings(
        detail_level=detail_level,
        target_total_min_s=target_total_min_s,
        target_total_max_s=target_total_max_s,
        target_total_hard_cap_s=target_total_hard_cap_s,
        episode_min_s=episode_min_s,
        episode_normal_s=episode_normal_s,
        episode_high_s=episode_high_s,
        arc_size=arc_size,
    ).validate()
    events: list[SeriesEvent] = []
    episode_targets: list[EpisodeTargetPlan] = []
    warnings: list[str] = []
    target_video_s = 0.0
    episode_keys: list[str] = []

    for episode_key, run_dir in episode_run_dirs.items():
        artifacts = build_episode_artifacts(episode_key, run_dir)
        missing = [
            path
            for path in (
                artifacts.episode_meta,
                artifacts.episode_memory,
                artifacts.film_map,
                artifacts.film_map_meta,
                artifacts.story_map,
                artifacts.shots,
            )
            if not path.is_file()
        ]
        if missing:
            raise ValueError(f"episode {episode_key} is missing artifact(s): {', '.join(str(path) for path in missing)}")
        meta = EpisodeMeta.model_validate(load_json(artifacts.episode_meta))
        memory = EpisodeMemory.model_validate(load_json(artifacts.episode_memory))
        film_meta = FilmMapMeta.model_validate(load_json(artifacts.film_map_meta))
        film_map = [FilmMapSegment.model_validate(item) for item in load_json(artifacts.film_map)]
        sections = [StorySection.model_validate(item) for item in load_json(artifacts.story_map)]
        profile = VideoProfile.model_validate(load_json(artifacts.video_profile)) if artifacts.video_profile else None
        episode_keys.append(meta.episode_key)
        episode_story_duration = story_duration(film_meta.duration, profile)
        episode_target_video_s = target_base_duration(film_meta.duration, profile, recap_format) * ratios[meta.recap_mode]
        episode_char_budget = max(0, int(round(episode_target_video_s * tts_cps)))
        episode_event_count = 0
        for section in sections:
            if section.type == "non_story" or overlaps_non_story(section.tc_start, section.tc_end, profile):
                continue
            from_seg_id, to_seg_id = section_segment_span(section, film_map)
            event_id = f"{meta.episode_key}:section:{section.section_id}"
            events.append(
                SeriesEvent(
                    event_id=event_id,
                    series_id=meta.series_id,
                    episode_key=meta.episode_key,
                    episode_number=meta.episode_number,
                    title=meta.title,
                    source_path=meta.source_path,
                    arc=meta.arc,
                    recap_mode=meta.recap_mode,
                    summary=section.summary,
                    event_type=section.type,
                    from_seg_id=from_seg_id,
                    to_seg_id=to_seg_id,
                    tc_start=section.tc_start,
                    tc_end=section.tc_end,
                    importance=event_importance(section, meta),
                    is_hook_candidate=section.type in {"reveal", "climax", "inciting_incident"},
                    entity_hooks=memory.current.entity_hooks,
                    arc_hooks=memory.current.arc_hooks,
                )
            )
            episode_event_count += 1
        target_video_s += episode_target_video_s
        episode_targets.append(
            EpisodeTargetPlan(
                episode_key=meta.episode_key,
                episode_number=meta.episode_number,
                title=meta.title,
                arc=meta.arc,
                recap_mode=meta.recap_mode,
                source_duration_s=round(film_meta.duration, 3),
                story_duration_s=round(episode_story_duration, 3),
                importance_score=meta.importance_score,
                continuity_dependency=meta.score_signals.continuity_dependency,
                event_count=episode_event_count,
                target_video_s=round(episode_target_video_s, 3),
                char_budget=episode_char_budget,
                min_chars=max(0, int(round(episode_char_budget * 0.75))),
                target_beats=episode_target_beats(
                    char_budget=episode_char_budget,
                    event_count=episode_event_count,
                    recap_format=recap_format,
                ),
            )
        )
        warnings.extend(f"episode {episode_key}: {warning}" for warning in meta.warnings)

    if not events:
        raise ValueError("series event bank has no story events")
    episode_targets, season_target_plan = build_season_target_plan(
        recap_format=recap_format,
        episode_targets=episode_targets,
        tts_cps=tts_cps,
        settings=target_settings,
    )
    target_video_s = season_target_plan.total_target_video_s
    warnings.extend(season_target_plan.warnings)
    if target_video_s <= 0:
        target_video_s = sum(event.tc_end - event.tc_start for event in events[: min(3, len(events))]) * 0.25
    return SeriesEventBank(
        series_id=manifest.series_id,
        series_title=manifest.series_title,
        recap_format=recap_format,
        episode_keys=episode_keys,
        target_video_s=round(target_video_s, 3),
        char_budget=max(1, int(round(target_video_s * tts_cps))),
        episode_targets=episode_targets,
        season_target_plan=season_target_plan,
        events=events,
        warnings=warnings,
        created_at=datetime.now(timezone.utc),
    )


def composer_length_plan(bank: SeriesEventBank) -> ComposerLengthPlan:
    event_count = max(1, len(bank.events))
    episode_count = max(1, len(bank.episode_keys))
    if is_chaptered_format(bank.recap_format):
        targets = chaptered_episode_targets(bank)
        min_beats = min(event_count, max(1, len(targets) + 1))
        desired_beats = 1 + sum(target.target_beats for target in targets)
        max_beats = min(event_count, max(min_beats, desired_beats))
        divisor = 680 if bank.recap_format == "episode_arc_chaptered" else 480
        target_beats = max(min_beats, min(max_beats, round(bank.char_budget / divisor) or min_beats))
    else:
        if episode_count >= 3:
            base_min, base_max = 9, 11
        elif episode_count == 2:
            base_min, base_max = 6, 8
        else:
            base_min, base_max = 4, 7
        min_beats = min(event_count, base_min)
        max_beats = min(event_count, base_max)
        if min_beats > max_beats:
            min_beats = max_beats
        if bank.char_budget < 700:
            min_beats = max(1, min(event_count, round(bank.char_budget / 180) or 1))
            max_beats = max(min_beats, min(event_count, min_beats + 1))
        target_beats = max(min_beats, min(max_beats, round(bank.char_budget / 360) or min_beats))
    per_beat_target = max(1, int(round(bank.char_budget / max(1, target_beats))))
    return ComposerLengthPlan(
        min_total_chars=int(bank.char_budget * 0.85),
        max_total_chars=int(bank.char_budget * 1.15),
        min_beats=min_beats,
        max_beats=max_beats,
        per_beat_target_chars=per_beat_target,
        per_beat_min_chars=max(120, int(per_beat_target * 0.75)),
        per_beat_max_chars=max(180, int(per_beat_target * 1.25)),
    )

def build_composer_prompt(bank: SeriesEventBank) -> str:
    length = composer_length_plan(bank)
    payload = [
        {
            "event_id": event.event_id,
            "episode_key": event.episode_key,
            "episode_number": event.episode_number,
            "arc": event.arc,
            "recap_mode": event.recap_mode,
            "summary": event.summary,
            "event_type": event.event_type,
            "importance": event.importance,
            "is_hook_candidate": event.is_hook_candidate,
            "entity_hooks": event.entity_hooks,
            "arc_hooks": event.arc_hooks,
        }
        for event in bank.events
    ]
    if bank.recap_format == "episode_arc_chaptered":
        target_payload = [
            {
                "episode_key": target.episode_key,
                "episode_number": target.episode_number,
                "title": target.title,
                "arc": target.arc,
                "recap_mode": target.recap_mode,
                "importance_score": target.importance_score,
                "continuity_dependency": target.continuity_dependency,
                "event_count": target.event_count,
                "target_video_s": target.target_video_s,
                "char_budget": target.char_budget,
                "min_chars": target.min_chars,
                "target_beats": target.target_beats,
            }
            for target in bank.episode_targets
            if target.char_budget > 0
        ]
        season_payload = bank.season_target_plan.model_dump(mode="json") if bank.season_target_plan else {}
        return f"""
You are composing one detailed Vietnamese recap video for a 12-episode anime season.
Format: episode_arc_chaptered. Return ONLY JSON with key "beats": [{{"event_ids": [string], "narration": string, "is_hook": boolean}}].

Rules:
- Choose event_ids only from EVENT_BANK; do not invent timecodes, source paths, or episode ids.
- Beat 0 is one shared cold-open hook for the whole video. It may use a strong event from any selected episode.
- After beat 0, return to the earliest selected episode and recap every episode as a connected chapter in monotonic order.
- The structure should feel like arcs of about three episodes: setup, escalation, payoff, then a bridge into the next arc.
- Every episode in EPISODE_TARGET_PLAN with target_beats > 0 must have at least one non-hook beat after the hook.
- Use every selected event_id at most once. Do not reuse the hook event later.
- Never use placeholder event_ids like ... or ellipses; copy exact event_id strings from EVENT_BANK.
- No OP/ED/theme-song/preview/recap-only content.
- No verbatim dialogue or lyrics; transform into Vietnamese commentary.
- Write pure Vietnamese with proper diacritics. Do not copy raw English/Japanese phrases from the event summaries; paraphrase them in Vietnamese instead.
- Target total narration around {bank.char_budget} Vietnamese characters and {bank.target_video_s:.1f}s; do not go below {length.min_total_chars} characters unless the event bank is too small. {length.max_total_chars} characters is only a soft advisory; longer output is acceptable if the story needs it.
- Return {length.min_beats}-{length.max_beats} beats. Most beats should be {length.per_beat_min_chars}-{length.per_beat_max_chars} characters, around {length.per_beat_target_chars} characters each.
- For each episode, stay near its char_budget and target_beats from EPISODE_TARGET_PLAN. Never go below min_chars unless the episode has too few usable events.
- Use 2-4 Vietnamese sentences per beat and make cause/effect explicit so the season recap feels detailed, not compressed.

SEASON_TARGET_PLAN:
{json.dumps(season_payload, ensure_ascii=False)}

EPISODE_TARGET_PLAN:
{json.dumps(target_payload, ensure_ascii=False)}

EVENT_BANK:
{json.dumps(payload, ensure_ascii=False)}
""".strip()
    if bank.recap_format == "episode_chaptered":
        target_payload = [
            {
                "episode_key": target.episode_key,
                "episode_number": target.episode_number,
                "title": target.title,
                "recap_mode": target.recap_mode,
                "target_video_s": target.target_video_s,
                "char_budget": target.char_budget,
                "min_chars": target.min_chars,
                "target_beats": target.target_beats,
            }
            for target in bank.episode_targets
            if target.char_budget > 0
        ]
        return f"""
You are composing one Vietnamese recap video for a multi-episode anime season.
Format: episode_chaptered. Return ONLY JSON with key "beats": [{{"event_ids": [string], "narration": string, "is_hook": boolean}}].

Rules:
- Choose event_ids only from EVENT_BANK; do not invent timecodes, source paths, or episode ids.
- Beat 0 is one shared cold-open hook for the whole video. It may use a strong event from any selected episode.
- After beat 0, recap the episodes as connected chapters in episode order: episode 1, then episode 2, then episode 3, and so on.
- Every episode in EPISODE_TARGET_PLAN with target_beats > 0 must have at least one non-hook beat after the hook.
- Treat quick episodes as real chapter recaps, not tiny bridges. Use merge episodes as short continuity chapters. Skip episodes with a zero budget.
- Chapter transitions should connect cause and effect between episodes; do not restart each episode like a separate video.
- Use every selected event_id at most once. Do not reuse the hook event later.
- Never use placeholder event_ids like ... or ellipses; copy exact event_id strings from EVENT_BANK.
- No OP/ED/theme-song/preview/recap-only content.
- No verbatim dialogue or lyrics; transform into Vietnamese commentary.
- Write pure Vietnamese with proper diacritics. Do not copy raw English/Japanese phrases from the event summaries; paraphrase them in Vietnamese instead.
- Target total narration around {bank.char_budget} Vietnamese characters and {bank.target_video_s:.1f}s; do not go below {length.min_total_chars} characters unless the event bank is too small. {length.max_total_chars} characters is only a soft advisory; longer output is acceptable if the story needs it.
- Return {length.min_beats}-{length.max_beats} beats. Most beats should be {length.per_beat_min_chars}-{length.per_beat_max_chars} characters, around {length.per_beat_target_chars} characters each.
- For each episode, try to stay near its char_budget and target_beats from EPISODE_TARGET_PLAN. Never go below min_chars unless the episode has too few usable events.
- Use 2-3 Vietnamese sentences per beat; do not collapse an episode chapter into one generic sentence.

EPISODE_TARGET_PLAN:
{json.dumps(target_payload, ensure_ascii=False)}

EVENT_BANK:
{json.dumps(payload, ensure_ascii=False)}
""".strip()
    return f"""
You are composing one Vietnamese recap video for a multi-episode anime arc.
Return ONLY JSON with key "beats": [{{"event_ids": [string], "narration": string, "is_hook": boolean}}].

Rules:
- Choose event_ids only from EVENT_BANK; do not invent timecodes or episode ids.
- Write one continuous story, not a mechanical "episode 1, episode 2" list.
- Use every selected event_id at most once. Do not reuse the hook event later.
- The first beat must be a hook. After that hook, continue in chronological episode/story order.
- Use quick/merge episodes as short continuity bridges; if every episode in scope is quick, still build a complete but tight arc.
- Never use placeholder event_ids like ... or ellipses; copy exact event_id strings from EVENT_BANK.
- No OP/ED/theme-song/preview/recap-only content.
- No verbatim dialogue or lyrics; transform into Vietnamese commentary.
- Write pure Vietnamese with proper diacritics. Do not copy raw English/Japanese phrases from the event summaries; paraphrase them in Vietnamese instead.
- Target total narration around {bank.char_budget} Vietnamese characters and {bank.target_video_s:.1f}s; do not go below {length.min_total_chars} characters unless the event bank is too small. {length.max_total_chars} characters is only a soft advisory; longer output is acceptable if the story needs it.
- Return {length.min_beats}-{length.max_beats} beats. Most beats should be {length.per_beat_min_chars}-{length.per_beat_max_chars} characters, around {length.per_beat_target_chars} characters each.
- Use 2-3 Vietnamese sentences per beat; do not collapse a multi-event beat into one generic sentence.
- When grouping adjacent events, mention the cause/effect progression from each selected event so the season recap feels connected and complete.

EVENT_BANK:
{json.dumps(payload, ensure_ascii=False)}
""".strip()


def source_ref_from_event(event: SeriesEvent) -> SeriesSourceRef:
    return SeriesSourceRef(
        event_id=event.event_id,
        episode_key=event.episode_key,
        src=event_src_key(event.episode_key, event.source_path),
        source_path=event.source_path,
        from_seg_id=event.from_seg_id,
        to_seg_id=event.to_seg_id,
        src_tc_start=event.tc_start,
        src_tc_end=event.tc_end,
    )


def parse_composer_response(
    payload: object,
    bank: SeriesEventBank,
    *,
    require_hook: bool = True,
) -> list[SeriesReviewBeat]:
    if not isinstance(payload, dict) or not isinstance(payload.get("beats"), list):
        raise ValueError("series composer response must be an object with beats[]")
    events_by_id = {event.event_id: event for event in bank.events}
    beats: list[SeriesReviewBeat] = []
    for index, raw in enumerate(payload["beats"]):
        if not isinstance(raw, dict):
            raise ValueError("series composer beat must be an object")
        event_ids = raw.get("event_ids")
        if not isinstance(event_ids, list) or not event_ids:
            raise ValueError("series composer beat requires event_ids")
        source_refs: list[SeriesSourceRef] = []
        for event_id in event_ids:
            event_id_text = str(event_id)
            if any(marker in event_id_text for marker in PLACEHOLDER_EVENT_ID_MARKERS):
                continue
            if event_id_text not in events_by_id:
                raise ValueError(f"series composer selected unknown event_id: {event_id}")
            source_refs.append(source_ref_from_event(events_by_id[event_id_text]))
        if not source_refs:
            continue
        beats.append(
            SeriesReviewBeat(
                beat_id=index,
                narration=str(raw.get("narration", "")),
                source_refs=source_refs,
                is_hook=bool(raw.get("is_hook", index == 0 and require_hook)),
            )
        )
    if beats and require_hook:
        beats[0] = beats[0].model_copy(update={"is_hook": True})
        return validate_series_review_script(beats)
    return [beat.model_copy(update={"is_hook": False}) for beat in beats]

def clean_event_summary(summary: str, limit: int = 420) -> str:
    cleaned = " ".join(summary.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "..."

def summary_looks_safe_for_tts(summary: str) -> bool:
    cleaned = " ".join(summary.split())
    return bool(cleaned) and bool(qa_safe_summary_fragment(cleaned, limit=320))

def safe_summary_fragment(summary: str, limit: int = 240) -> str:
    return qa_safe_summary_fragment(summary, limit=limit)

def fallback_arc_draft(bank: SeriesEventBank, arc: SeriesArcPlan, *, arc_index: int) -> list[SeriesReviewBeat]:
    return validated_fallback_arc_draft(bank, arc, arc_index=arc_index)

def validated_fallback_arc_draft(bank: SeriesEventBank, arc: SeriesArcPlan, *, arc_index: int) -> list[SeriesReviewBeat]:
    events = [
        event
        for event in events_for_arc(bank, arc)
        if event.event_type not in NON_STORY_EVENT_TYPES and event.tc_end > event.tc_start
    ]
    if not events:
        return []
    beats: list[SeriesReviewBeat] = []
    used: set[str] = set()
    if arc_index == 0:
        hook_event = next(
            (event for event in global_hook_candidates(bank, limit=6) if event.event_type not in NON_STORY_EVENT_TYPES),
            events[0],
        )
        used.add(hook_event.event_id)
        beats.append(
            SeriesReviewBeat(
                beat_id=0,
                narration=fallback_text(
                    "Mở đầu bằng bước ngoặt nổi bật của mùa",
                    hook_event.summary,
                    target_chars=420,
                ),
                source_refs=[source_ref_from_event(hook_event)],
                is_hook=True,
            )
        )

    for target in arc.episodes:
        if target.target_beats <= 0 or target.char_budget <= 0:
            continue
        episode_events = [
            event
            for event in events
            if event.episode_key == target.episode_key and event.event_id not in used
        ]
        episode_events = sorted(episode_events, key=lambda event: (event.tc_start, -event.importance))
        quota = max(1, min(len(episode_events), target.target_beats or 1))
        episode_label = f"tập {target.episode_number}" if target.episode_number is not None else target.episode_key
        for event in episode_events[:quota]:
            used.add(event.event_id)
            beats.append(
                SeriesReviewBeat(
                    beat_id=len(beats),
                    narration=fallback_text(
                        f"Ở {episode_label}, mạch truyện giữ mốc quan trọng này",
                        event.summary,
                        target_chars=max(320, int(target.char_budget * 0.45) if target.char_budget > 0 else 320),
                    ),
                    source_refs=[source_ref_from_event(event)],
                    is_hook=False,
                )
            )
    renumbered = renumber_beats(beats)
    if arc_index == 0:
        return normalize_series_beats(renumbered)
    return [beat.model_copy(update={"is_hook": False}) for beat in renumbered]

def safe_fallback_arc_draft(bank: SeriesEventBank, arc: SeriesArcPlan, *, arc_index: int) -> list[SeriesReviewBeat]:
    return validated_fallback_arc_draft(bank, arc, arc_index=arc_index)
    events = [
        event
        for event in events_for_arc(bank, arc)
        if event.event_type not in NON_STORY_EVENT_TYPES and event.tc_end > event.tc_start
    ]
    if not events:
        return []
    beats: list[SeriesReviewBeat] = []
    used: set[str] = set()
    if arc_index == 0:
        hook_event = next(
            (event for event in global_hook_candidates(bank, limit=6) if event.event_type not in NON_STORY_EVENT_TYPES),
            events[0],
        )
        used.add(hook_event.event_id)
        beats.append(
            SeriesReviewBeat(
                beat_id=0,
                narration=f"Má»Ÿ Ä‘áº§u báº±ng bÆ°á»›c ngoáº·t ná»•i báº­t cá»§a mÃ¹a: {clean_event_summary(hook_event.summary)}",
                source_refs=[source_ref_from_event(hook_event)],
                is_hook=True,
            )
        )
    for target in arc.episodes:
        if target.target_beats <= 0 or target.char_budget <= 0:
            continue
        episode_events = [
            event
            for event in events
            if event.episode_key == target.episode_key and event.event_id not in used
        ]
        episode_events = sorted(episode_events, key=lambda event: (event.tc_start, -event.importance))
        quota = max(1, min(len(episode_events), target.target_beats or 1))
        episode_label = f"táº­p {target.episode_number}" if target.episode_number is not None else target.episode_key
        for event in episode_events[:quota]:
            used.add(event.event_id)
            beats.append(
                SeriesReviewBeat(
                    beat_id=len(beats),
                    narration=(
                        f"á»ž {episode_label}, máº¡ch truyá»‡n giá»¯ má»‘c quan trá»ng nÃ y: "
                        f"{clean_event_summary(event.summary)} "
                        "Diá»…n biáº¿n áº¥y trá»Ÿ thÃ nh ná»n Ä‘á»ƒ ná»‘i tiáº¿p xung Ä‘á»™t cá»§a mÃ¹a."
                    ),
                    source_refs=[source_ref_from_event(event)],
                    is_hook=False,
            )
            )
    return renumber_beats(beats)

def fallback_text(prefix: str, summary: str, *, target_chars: int) -> str:
    cleaned_summary = qa_safe_summary_fragment(summary, limit=320)
    sentences = []
    if cleaned_summary:
        sentences.append(f"{prefix}: {cleaned_summary}.")
    else:
        sentences.append(
            f"{prefix}: đây là một bước chuyển của mạch truyện, khi tình thế đổi hướng và nhân vật phải bước sang hệ quả tiếp theo."
        )
    sentences.extend(
        [
            "Mốc này giữ nguyên nguyên nhân và hệ quả thay vì kể lướt một cách máy móc.",
            "Nó còn tạo đủ ngữ cảnh để recap nối sang phần sau mà không bị đứt mạch.",
            "Vì vậy, chi tiết này nên được hiểu như ý nghĩa câu chuyện chứ không phải nguyên văn đối thoại.",
        ]
    )
    text = " ".join(sentences)
    if len(text) <= target_chars:
        return text.strip()
    trimmed = text[:target_chars].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return trimmed + "."

def deterministic_series_draft(bank: SeriesEventBank) -> list[SeriesReviewBeat]:
    events = [
        event
        for event in bank.events
        if event.event_type not in NON_STORY_EVENT_TYPES and event.tc_end > event.tc_start
    ]
    if not events:
        return []

    events_by_episode: dict[str, list[SeriesEvent]] = {}
    for event in sorted(events, key=lambda item: (bank.episode_keys.index(item.episode_key), item.tc_start)):
        events_by_episode.setdefault(event.episode_key, []).append(event)

    beats: list[SeriesReviewBeat] = []
    used: set[str] = set()
    hook_event = next(
        (event for event in global_hook_candidates(bank, limit=10) if event.event_type not in NON_STORY_EVENT_TYPES),
        events[0],
    )
    used.add(hook_event.event_id)
    beats.append(
        SeriesReviewBeat(
            beat_id=0,
            narration=fallback_text(
                "Mở đầu mùa phim",
                hook_event.summary,
                target_chars=max(
                    360,
                    min(
                        720,
                        int(bank.char_budget * DETERMINISTIC_FALLBACK_CHAR_SCALE) // max(8, len(bank.episode_keys)),
                    ),
                ),
            ),
            source_refs=[source_ref_from_event(hook_event)],
            is_hook=True,
        )
    )

    targets = chaptered_episode_targets(bank)
    if not targets:
        per_event_chars = max(
            320,
            int(bank.char_budget * DETERMINISTIC_FALLBACK_CHAR_SCALE) // max(1, len(events)),
        )
        for event in events:
            if event.event_id in used:
                continue
            beats.append(
                SeriesReviewBeat(
                    beat_id=len(beats),
                    narration=fallback_text("Câu nối mạch truyện", event.summary, target_chars=per_event_chars),
                    source_refs=[source_ref_from_event(event)],
                    is_hook=False,
                )
            )
        return normalize_series_beats(beats)

    for target in targets:
        episode_events = [event for event in events_by_episode.get(target.episode_key, []) if event.event_id not in used]
        if not episode_events:
            episode_events = list(events_by_episode.get(target.episode_key, []))
        if not episode_events:
            continue
        quota = max(1, min(len(episode_events), target.target_beats or len(episode_events)))
        per_beat_chars = max(
            320,
            int(target.char_budget * DETERMINISTIC_FALLBACK_CHAR_SCALE) // quota if quota else target.char_budget,
        )
        episode_label = f"tập {target.episode_number}" if target.episode_number is not None else target.episode_key
        for event in episode_events[:quota]:
            used.add(event.event_id)
            beats.append(
                SeriesReviewBeat(
                    beat_id=len(beats),
                    narration=fallback_text(
                        f"Ở {episode_label}, chuyện cần giữ lại là",
                        event.summary,
                        target_chars=per_beat_chars,
                    ),
                    source_refs=[source_ref_from_event(event)],
                    is_hook=False,
                )
            )
    return normalize_series_beats(beats)

def compose_deterministic_fallback(
    bank: SeriesEventBank,
    *,
    reason: str,
    prompt_count: int = 0,
    revision_count: int = 0,
) -> tuple[list[SeriesReviewBeat], SeriesReviewMeta]:
    beats = deterministic_series_draft(bank)
    qa_report = [
        {
            "level": "warning",
            "code": "deterministic_composer_fallback",
            "message": "Series composer used deterministic event-bank fallback",
            "reason": reason,
        },
        *composer_qa_report(beats, bank),
    ]
    meta = build_review_meta(
        bank=bank,
        beats=beats,
        qa_report=qa_report,
        revision_count=revision_count,
        prompt_count=prompt_count,
    )
    return beats, meta

def renumber_beats(beats: list[SeriesReviewBeat]) -> list[SeriesReviewBeat]:
    return [beat.model_copy(update={"beat_id": index}) for index, beat in enumerate(beats)]

def normalize_series_beats(beats: list[SeriesReviewBeat]) -> list[SeriesReviewBeat]:
    if not beats:
        return []
    normalized = [
        beat.model_copy(update={"is_hook": index == 0})
        for index, beat in enumerate(beats)
    ]
    return validate_series_review_script(renumber_beats(normalized))

def beats_to_prompt_payload(beats: list[SeriesReviewBeat]) -> dict[str, object]:
    return {
        "beats": [
            {
                "event_ids": [ref.event_id for ref in beat.source_refs],
                "narration": beat.narration,
                "is_hook": beat.is_hook,
            }
            for beat in beats
        ]
    }


def composer_qa_report(beats: list[SeriesReviewBeat], bank: SeriesEventBank) -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    if not beats:
        report.append({"level": "error", "code": "empty_script", "message": "Composer returned no beats"})
        return report
    if not beats[0].is_hook:
        report.append({"level": "error", "code": "missing_hook", "message": "First beat is not marked as hook"})

    selected = [ref.event_id for beat in beats for ref in beat.source_refs]
    duplicated = sorted(event_id for event_id in set(selected) if selected.count(event_id) > 1)
    if duplicated:
        report.append(
            {
                "level": "warning",
                "code": "repeated_events",
                "message": "Composer reused events; review for repetitive recap flow",
                "event_ids": duplicated,
            }
        )

    report.extend(analyze_narration_content(beats))

    total_chars = sum(len(beat.narration) for beat in beats)
    if bank.recap_format == "episode_arc_chaptered" and total_chars < bank.char_budget * 0.85:
        report.append(
            {
                "level": "error",
                "code": "season_under_char_budget",
                "message": "Detailed season narration is below 85% of the season character budget",
                "total_chars": total_chars,
                "char_budget": bank.char_budget,
            }
        )
    elif total_chars < bank.char_budget * 0.85:
        report.append(
            {
                "level": "warning",
                "code": "under_target_length",
                "message": "Composer narration is much shorter than target",
                "total_chars": total_chars,
                "char_budget": bank.char_budget,
            }
        )

    events_by_id = {event.event_id: event for event in bank.events}
    event_order = {event.event_id: index for index, event in enumerate(bank.events)}
    chronological_beats = beats[1:] if beats and beats[0].is_hook else beats
    previous = -1
    for beat in chronological_beats:
        first_event = beat.source_refs[0].event_id
        current = event_order.get(first_event, previous)
        if current < previous:
            report.append(
                {
                    "level": "warning",
                    "code": "non_monotonic_story_order",
                    "message": "Composer selected an earlier event after a later one",
                    "beat_id": beat.beat_id,
                    "event_id": first_event,
                }
            )
            break
        previous = current

    if is_chaptered_format(bank.recap_format):
        targets = chaptered_episode_targets(bank)
        episode_order = {episode_key: index for index, episode_key in enumerate(bank.episode_keys)}
        non_hook_episode_chars: dict[str, int] = {target.episode_key: 0 for target in targets}
        non_hook_episode_beats: dict[str, int] = {target.episode_key: 0 for target in targets}
        previous_episode_order = -1
        for beat in chronological_beats:
            first_ref = beat.source_refs[0]
            episode_key = first_ref.episode_key
            current_episode_order = episode_order.get(episode_key, previous_episode_order)
            if current_episode_order < previous_episode_order:
                report.append(
                    {
                        "level": "warning",
                        "code": "non_monotonic_episode_order",
                        "message": "Composer moved to an earlier episode after a later chapter",
                        "beat_id": beat.beat_id,
                        "episode_key": episode_key,
                    }
                )
                break
            previous_episode_order = current_episode_order
            if episode_key in non_hook_episode_chars:
                non_hook_episode_chars[episode_key] += len(beat.narration)
                non_hook_episode_beats[episode_key] += 1
        for target in targets:
            if non_hook_episode_beats.get(target.episode_key, 0) <= 0:
                report.append(
                    {
                        "level": "error",
                        "code": "missing_episode_chapter",
                        "message": "Composer omitted a non-hook chapter for an episode",
                        "episode_key": target.episode_key,
                    }
                )
                continue
            chars = non_hook_episode_chars.get(target.episode_key, 0)
            if chars < target.min_chars:
                report.append(
                    {
                        "level": "warning",
                        "code": "episode_under_char_budget",
                        "message": "Episode chapter narration is below its minimum character budget",
                        "episode_key": target.episode_key,
                        "chars": chars,
                        "min_chars": target.min_chars,
                        "char_budget": target.char_budget,
                    }
                )
        non_story_events = [
            event_id
            for event_id in selected
            if events_by_id.get(event_id) is not None and events_by_id[event_id].event_type in NON_STORY_EVENT_TYPES
        ]
        if non_story_events:
            report.append(
                {
                    "level": "error",
                    "code": "non_story_event_selected",
                    "message": "Composer selected non-story events",
                    "event_ids": non_story_events,
                }
            )
        if bank.recap_format == "episode_arc_chaptered" and bank.season_target_plan is not None:
            plan = bank.season_target_plan
            if plan.target_total_hard_cap_s and bank.char_budget:
                estimated_duration_s = (total_chars / bank.char_budget) * bank.target_video_s
                if estimated_duration_s > plan.target_total_hard_cap_s:
                    report.append(
                        {
                            "level": "warning",
                            "code": "estimated_duration_exceeds_hard_cap",
                            "message": "Estimated final duration exceeds the season hard cap",
                            "estimated_duration_s": round(estimated_duration_s, 3),
                            "target_total_hard_cap_s": plan.target_total_hard_cap_s,
                        }
                    )
            episode_to_arc = {
                episode_key: arc.arc_id
                for arc in plan.arcs
                for episode_key in arc.episode_keys
            }
            arc_chars: dict[str, int] = {arc.arc_id: 0 for arc in plan.arcs}
            for beat in chronological_beats:
                if beat.is_hook and plan.arcs:
                    arc_id = plan.arcs[0].arc_id
                else:
                    episode_key = beat.source_refs[0].episode_key
                    arc_id = episode_to_arc.get(episode_key)
                if arc_id is not None:
                    arc_chars[arc_id] += len(beat.narration)
            for arc_index, arc in enumerate(plan.arcs):
                if arc.char_budget <= 0:
                    continue
                chars = arc_chars.get(arc.arc_id, 0)
                if chars < arc.min_chars:
                    report.append(
                        {
                            "level": "warning",
                            "code": "arc_under_char_budget",
                            "message": "Arc narration is below its minimum character budget",
                            "arc_id": arc.arc_id,
                            "chars": chars,
                            "min_chars": arc.min_chars,
                            "char_budget": arc.char_budget,
                        }
                    )
                hard_max_chars = arc_hard_max_chars(bank, arc_index)
                if chars > hard_max_chars:
                    report.append(
                        {
                            "level": "warning",
                            "code": "arc_exceeds_hard_char_budget",
                            "message": "Arc narration exceeds its hard character budget",
                            "arc_id": arc.arc_id,
                            "chars": chars,
                            "hard_max_chars": hard_max_chars,
                        }
                    )
            if len(arc_chars) > 1 and total_chars > 0:
                dominant_arc, dominant_chars = max(arc_chars.items(), key=lambda item: item[1])
                dominant_ratio = dominant_chars / total_chars
                if dominant_ratio > 0.45:
                    report.append(
                        {
                            "level": "warning",
                            "code": "arc_dominates_too_much",
                            "message": "One arc dominates too much of the detailed season script",
                            "arc_id": dominant_arc,
                            "char_ratio": round(dominant_ratio, 4),
                        }
                    )
    else:
        bridge_events = {
            event.event_id
            for event in bank.events
            if event.recap_mode in {"quick", "merge", "skip"}
        }
        non_bridge_events = {event.event_id for event in bank.events} - bridge_events
        bridge_count = sum(1 for event_id in selected if event_id in bridge_events)
        if selected and non_bridge_events and bridge_count > max(2, len(selected) // 2):
            report.append(
                {
                    "level": "warning",
                    "code": "too_many_bridge_events",
                    "message": "Quick/merge/skip episodes dominate the final recap",
                    "bridge_event_count": bridge_count,
                }
            )
    return report

def needs_revision(qa_report: list[dict[str, object]]) -> bool:
    return any(item.get("level") == "error" or item.get("code") in REVISION_QA_CODES for item in qa_report)

def build_revision_prompt(
    *,
    bank: SeriesEventBank,
    beats: list[SeriesReviewBeat],
    qa_report: list[dict[str, object]],
    revision_number: int,
) -> str:
    length = composer_length_plan(bank)
    return f"""
{build_composer_prompt(bank)}

The previous draft failed deterministic QA. Rewrite the full JSON from scratch.

Hard revision requirements:
- Fix every issue in QA_REPORT.
- Total narration must not fall below {length.min_total_chars} Vietnamese characters. {length.max_total_chars} is a soft advisory; longer output is acceptable if the story needs it.
- Keep {length.min_beats}-{length.max_beats} beats and make most beats {length.per_beat_min_chars}-{length.per_beat_max_chars} characters.
- Keep beat 0 as the cold-open hook; after beat 0, continue chronological episode/story order.
- If format is episode_chaptered or episode_arc_chaptered, every episode in EPISODE_TARGET_PLAN with target_beats > 0 must have a non-hook chapter after the hook.
- If QA_REPORT contains under_target_length, season_under_char_budget, or episode_under_char_budget, expand the episode chapters with concrete cause/effect details. Being concise is a failure for chaptered modes.
- Do not reuse any event_id, including the hook event.
- Never use placeholder event_ids like ... or ellipses; copy exact event_id strings from EVENT_BANK.
- Write pure Vietnamese with proper diacritics. Do not copy raw English/Japanese phrases from the event summaries; paraphrase them in Vietnamese instead.
- Return ONLY JSON with the same schema.

REVISION_NUMBER: {revision_number}
QA_REPORT:
{json.dumps(qa_report, ensure_ascii=False)}

PREVIOUS_DRAFT:
{json.dumps(beats_to_prompt_payload(beats), ensure_ascii=False)}
""".strip()

def compact_text_list(values: list[str], *, limit: int = 3, item_limit: int = 28) -> list[str]:
    items: list[str] = []
    for value in values[:limit]:
        cleaned = safe_summary_fragment(str(value), limit=item_limit)
        if cleaned:
            items.append(cleaned)
    return items

def event_prompt_payload(events: list[SeriesEvent]) -> list[dict[str, object]]:
    return [
        {
            "event_id": event.event_id,
            "episode_key": event.episode_key,
            "episode_number": event.episode_number,
            "arc": event.arc,
            "recap_mode": event.recap_mode,
            "summary": clean_event_summary(event.summary, limit=220),
            "event_type": event.event_type,
            "importance": event.importance,
            "is_hook_candidate": event.is_hook_candidate,
            "entity_hooks": compact_text_list(event.entity_hooks, limit=3),
            "arc_hooks": compact_text_list(event.arc_hooks, limit=3),
        }
        for event in events
    ]

def episode_target_prompt_payload(targets: list[EpisodeTargetPlan]) -> list[dict[str, object]]:
    return [
        {
            "episode_key": target.episode_key,
            "episode_number": target.episode_number,
            "title": target.title,
            "arc": target.arc,
            "recap_mode": target.recap_mode,
            "importance_score": target.importance_score,
            "continuity_dependency": target.continuity_dependency,
            "event_count": target.event_count,
            "target_video_s": target.target_video_s,
            "char_budget": target.char_budget,
            "min_chars": target.min_chars,
            "target_beats": target.target_beats,
        }
        for target in targets
        if target.char_budget > 0
    ]

def events_for_arc(bank: SeriesEventBank, arc: SeriesArcPlan) -> list[SeriesEvent]:
    episode_keys = set(arc.episode_keys)
    return [event for event in bank.events if event.episode_key in episode_keys]

def global_hook_candidates(bank: SeriesEventBank, limit: int = 10) -> list[SeriesEvent]:
    candidates = [event for event in bank.events if event.is_hook_candidate]
    if not candidates:
        candidates = list(bank.events)
    return sorted(candidates, key=lambda event: event.importance, reverse=True)[:limit]

def arc_hard_max_chars(bank: SeriesEventBank, arc_index: int) -> int:
    plan = bank.season_target_plan
    if plan is None or not plan.arcs:
        return max(1, bank.char_budget)
    if not plan.target_total_hard_cap_s or bank.target_video_s <= 0 or bank.char_budget <= 0:
        return max(1, plan.arcs[arc_index].char_budget)

    hard_total_chars = int(plan.target_total_hard_cap_s * bank.char_budget / bank.target_video_s)
    cumulative_budget = sum(arc.char_budget for arc in plan.arcs[: arc_index + 1])
    previous_budget = sum(arc.char_budget for arc in plan.arcs[:arc_index])
    cumulative_limit = hard_total_chars * cumulative_budget // bank.char_budget
    previous_limit = hard_total_chars * previous_budget // bank.char_budget
    return max(1, cumulative_limit - previous_limit)

def build_arc_composer_prompt(
    *,
    bank: SeriesEventBank,
    arc: SeriesArcPlan,
    arc_index: int,
    qa_report: list[dict[str, object]] | None = None,
    previous_arc_beats: list[SeriesReviewBeat] | None = None,
    revision_number: int = 0,
) -> str:
    is_first_arc = arc_index == 0
    hard_max_chars = arc_hard_max_chars(bank, arc_index)
    hook_rule = (
        "Include beat 0 as the single global cold-open hook. It may use GLOBAL_HOOK_CANDIDATES from any episode."
        if is_first_arc
        else "Do not include a hook beat. Every returned beat must have is_hook=false."
    )
    revision_block = ""
    if qa_report is not None:
        revision_block = f"""

REVISION_NUMBER: {revision_number}
QA_REPORT:
{json.dumps(qa_report, ensure_ascii=False)}

PREVIOUS_ARC_DRAFT:
{json.dumps(beats_to_prompt_payload(previous_arc_beats or []), ensure_ascii=False)}
""".rstrip()
    global_hook_block = ""
    if is_first_arc:
        global_hook_block = f"""

GLOBAL_HOOK_CANDIDATES:
{json.dumps(event_prompt_payload(global_hook_candidates(bank, limit=4)), ensure_ascii=False)}
""".rstrip()
    return f"""
You are drafting one arc of a detailed Vietnamese anime season recap.
Return ONLY JSON with key "beats": [{{"event_ids": [string], "narration": string, "is_hook": boolean}}].

Arc: {arc.arc_id} - {arc.title}
Rules:
- Choose event_ids only from ARC_EVENT_BANK, except the first arc may use GLOBAL_HOOK_CANDIDATES for the hook.
- {hook_rule}
- After the hook, recap only this arc's episodes in episode order.
- Every episode in ARC_EPISODE_TARGETS with target_beats > 0 must have at least one non-hook beat.
- Stay near the arc target of {arc.char_budget} Vietnamese characters and {arc.target_video_s:.1f}s.
- HARD MAXIMUM: all narration returned for this arc must total no more than {hard_max_chars} characters. Shorten wording before returning JSON if needed.
- Each episode must reach at least its min_chars unless there are too few usable events.
- Use event_ids at most once inside this arc draft.
- Never use placeholder event_ids like ... or ellipses; if you cannot name an exact event_id, omit that beat.
- No OP/ED/theme-song/preview/recap-only content.
- No verbatim dialogue or lyrics; transform into Vietnamese commentary.
- Write pure Vietnamese with proper diacritics. Do not copy raw English/Japanese phrases from the event summaries; paraphrase them in Vietnamese instead.
- Write detailed cause/effect narration. Do not summarize an episode as a tiny bridge unless its recap_mode is merge.

ARC_EPISODE_TARGETS:
{json.dumps(episode_target_prompt_payload(arc.episodes), ensure_ascii=False)}

ARC_EVENT_BANK:
{json.dumps(event_prompt_payload(events_for_arc(bank, arc)), ensure_ascii=False)}

{global_hook_block}
{revision_block}
""".strip()

def build_final_stitch_prompt(
    *,
    bank: SeriesEventBank,
    beats: list[SeriesReviewBeat],
    qa_report: list[dict[str, object]] | None = None,
    revision_number: int = 0,
) -> str:
    length = composer_length_plan(bank)
    draft = beats_to_prompt_payload(beats)
    selected_event_ids = list(dict.fromkeys(ref.event_id for beat in beats for ref in beat.source_refs))
    revision_block = ""
    if qa_report is not None:
        revision_block = f"""

REVISION_NUMBER: {revision_number}
QA_REPORT:
{json.dumps(qa_report, ensure_ascii=False)}
""".rstrip()
    return f"""
You are doing the final stitch pass for one detailed Vietnamese anime season recap.
Return ONLY JSON with key "beats": [{{"event_ids": [string], "narration": string, "is_hook": boolean}}].

Rules:
- Smooth transitions, remove repetition, and keep the season as one continuous story.
- Use only event_ids from SELECTED_EVENT_IDS. Do not add new event_ids, timecodes, source paths, or episode ids.
- Keep beat 0 as the single hook. After beat 0, episode order must be monotonic from earliest to latest.
- Every episode already represented in the arc drafts must keep at least one non-hook beat.
- Do not go below {length.min_total_chars} Vietnamese characters. {length.max_total_chars} is a soft advisory; longer output is acceptable if the story needs it.
- Do not collapse detailed episode chapters into generic summaries.
- Never use placeholder event_ids like ... or ellipses; keep only exact event_ids from SELECTED_EVENT_IDS.
- No OP/ED/theme-song/preview/recap-only content. No verbatim dialogue or lyrics.
- Write pure Vietnamese with proper diacritics. Do not copy raw English/Japanese phrases from the event summaries; paraphrase them in Vietnamese instead.

SELECTED_EVENT_IDS:
{json.dumps(selected_event_ids, ensure_ascii=False)}

ARC_DRAFTS:
{json.dumps(draft, ensure_ascii=False)}
{revision_block}
""".strip()

def combine_arc_drafts(arc_drafts: list[list[SeriesReviewBeat]]) -> list[SeriesReviewBeat]:
    combined: list[SeriesReviewBeat] = []
    for arc_index, arc_beats in enumerate(arc_drafts):
        if arc_index == 0:
            combined.extend(arc_beats)
        else:
            combined.extend(beat.model_copy(update={"is_hook": False}) for beat in arc_beats)
    return normalize_series_beats(combined)

def validate_stitch_event_subset(beats: list[SeriesReviewBeat], allowed_event_ids: set[str]) -> None:
    selected = {ref.event_id for beat in beats for ref in beat.source_refs}
    unknown = sorted(selected - allowed_event_ids)
    if unknown:
        raise ValueError(f"final stitch selected event_id outside arc drafts: {unknown[:10]}")

def arc_indexes_for_revision(
    qa_report: list[dict[str, object]],
    bank: SeriesEventBank,
    beats: list[SeriesReviewBeat],
) -> set[int]:
    plan = bank.season_target_plan
    if plan is None:
        return set()
    episode_to_arc = {
        episode_key: index
        for index, arc in enumerate(plan.arcs)
        for episode_key in arc.episode_keys
    }
    event_to_arc = {
        event.event_id: episode_to_arc[event.episode_key]
        for event in bank.events
        if event.episode_key in episode_to_arc
    }
    beats_by_id = {beat.beat_id: beat for beat in beats}
    result: set[int] = set()
    for item in qa_report:
        code = item.get("code")
        if code in {
            "empty_script",
            "missing_hook",
            "repeated_events",
            "under_target_length",
            "season_under_char_budget",
            "estimated_duration_exceeds_hard_cap",
            "non_monotonic_episode_order",
            "non_monotonic_story_order",
        }:
            result.update(range(len(plan.arcs)))
        arc_id = item.get("arc_id")
        if isinstance(arc_id, str):
            for index, arc in enumerate(plan.arcs):
                if arc.arc_id == arc_id:
                    result.add(index)
        episode_key = item.get("episode_key")
        if isinstance(episode_key, str) and episode_key in episode_to_arc:
            result.add(episode_to_arc[episode_key])
        beat_ids = item.get("beat_ids")
        if isinstance(beat_ids, list):
            for beat_id in beat_ids:
                if not isinstance(beat_id, int) or beat_id not in beats_by_id:
                    continue
                beat = beats_by_id[beat_id]
                if beat.is_hook and plan.arcs:
                    result.add(0)
                result.update(
                    episode_to_arc[ref.episode_key]
                    for ref in beat.source_refs
                    if ref.episode_key in episode_to_arc
                )
        event_ids = item.get("event_ids")
        if isinstance(event_ids, list):
            result.update(
                event_to_arc[event_id]
                for event_id in event_ids
                if isinstance(event_id, str) and event_id in event_to_arc
            )
    return result

def build_review_meta(
    *,
    bank: SeriesEventBank,
    beats: list[SeriesReviewBeat],
    qa_report: list[dict[str, object]],
    revision_count: int,
    prompt_count: int,
) -> SeriesReviewMeta:
    selected_event_ids = list(dict.fromkeys(ref.event_id for beat in beats for ref in beat.source_refs))
    detail_level = bank.season_target_plan.detail_level if bank.season_target_plan is not None else "standard"
    arc_count = bank.season_target_plan.arc_count if bank.season_target_plan is not None else 0
    return SeriesReviewMeta(
        series_id=bank.series_id,
        target_video_s=bank.target_video_s,
        char_budget=bank.char_budget,
        est_total_chars=sum(len(beat.narration) for beat in beats),
        n_events=len(bank.events),
        selected_event_ids=selected_event_ids,
        qa_report=qa_report,
        model_versions={
            "llm": "chatgpt_playwright",
            "qa_revisions": str(revision_count),
            "format": bank.recap_format,
            "detail_level": detail_level,
            "prompt_count": str(prompt_count),
            "arc_count": str(arc_count),
        },
        warnings=bank.warnings + [str(item["message"]) for item in qa_report if item.get("level") == "warning"],
        created_at=datetime.now(timezone.utc),
    )

async def compose_episode_arc_chaptered_with_client(
    client: ChatClient,
    bank: SeriesEventBank,
    *,
    qa_max_revisions: int,
) -> tuple[list[SeriesReviewBeat], SeriesReviewMeta]:
    if bank.season_target_plan is None or not bank.season_target_plan.arcs:
        raise ValueError("episode_arc_chaptered requires season_target_plan arcs")
    arc_drafts: list[list[SeriesReviewBeat]] = []
    prompt_count = 0
    revision_count = 0
    qa_report: list[dict[str, object]] = []
    for arc_index, arc in enumerate(bank.season_target_plan.arcs):
        prompt = build_arc_composer_prompt(bank=bank, arc=arc, arc_index=arc_index)
        prompt_count += 1
        response = await client.ask(prompt)
        try:
            parsed_arc = parse_composer_response(extract_json(response), bank, require_hook=arc_index == 0)
            if not parsed_arc:
                raise ValueError("series composer returned no beats for this arc")
        except (ValueError, json.JSONDecodeError) as exc:
            qa_report.append(
                {
                    "level": "warning",
                    "code": "invalid_arc_json",
                    "message": f"Arc {arc.arc_id} returned invalid/empty JSON; used deterministic fallback",
                    "error": str(exc),
                    "arc_id": arc.arc_id,
                }
            )
            try:
                parsed_arc = fallback_arc_draft(bank, arc, arc_index=arc_index)
            except Exception as fallback_exc:  # pragma: no cover - defensive smoke fallback
                qa_report.append(
                    {
                        "level": "warning",
                        "code": "fallback_arc_failed",
                        "message": f"Arc {arc.arc_id} fallback failed; used bare deterministic draft",
                        "error": str(fallback_exc),
                        "arc_id": arc.arc_id,
                    }
                )
                parsed_arc = safe_fallback_arc_draft(bank, arc, arc_index=arc_index)
        arc_drafts.append(parsed_arc)

    beats = combine_arc_drafts(arc_drafts)
    stitch_prompt = build_final_stitch_prompt(bank=bank, beats=beats)
    if len(stitch_prompt) <= MAX_FINAL_STITCH_PROMPT_CHARS:
        prompt_count += 1
        stitch_response = await client.ask(stitch_prompt)
        try:
            allowed_event_ids = {ref.event_id for beat in beats for ref in beat.source_refs}
            stitched_beats = parse_composer_response(extract_json(stitch_response), bank)
            validate_stitch_event_subset(stitched_beats, allowed_event_ids)
            beats = normalize_series_beats(stitched_beats)
        except (ValueError, json.JSONDecodeError) as exc:
            qa_report.append(
                {
                    "level": "warning",
                    "code": "invalid_final_stitch_json",
                    "message": "Final stitch returned invalid JSON; kept combined arc draft",
                    "error": str(exc),
                }
            )
    else:
        qa_report.append(
            {
                "level": "warning",
                "code": "final_stitch_skipped_prompt_too_large",
                "message": "Final stitch prompt was too large for reliable ChatGPT browser input; kept combined arc draft",
                "prompt_chars": len(stitch_prompt),
                "max_prompt_chars": MAX_FINAL_STITCH_PROMPT_CHARS,
            }
        )
    qa_report = [*qa_report, *composer_qa_report(beats, bank)]

    for _attempt in range(qa_max_revisions):
        if not needs_revision(qa_report):
            break
        revision_count += 1
        arc_indexes = arc_indexes_for_revision(qa_report, bank, beats)
        if arc_indexes:
            for arc_index in sorted(arc_indexes):
                arc = bank.season_target_plan.arcs[arc_index]
                prompt = build_arc_composer_prompt(
                    bank=bank,
                    arc=arc,
                    arc_index=arc_index,
                    qa_report=qa_report,
                    previous_arc_beats=arc_drafts[arc_index],
                    revision_number=revision_count,
                )
                prompt_count += 1
                response = await client.ask(prompt)
                try:
                    parsed_arc = parse_composer_response(
                        extract_json(response),
                        bank,
                        require_hook=arc_index == 0,
                    )
                    if not parsed_arc:
                        raise ValueError("series composer returned no beats for this arc")
                except (ValueError, json.JSONDecodeError) as exc:
                    qa_report = [
                        *qa_report,
                        {
                            "level": "warning",
                            "code": "invalid_revision_json",
                            "message": "Composer revision returned invalid/empty JSON; used deterministic fallback for that arc",
                            "error": str(exc),
                        },
                    ]
                    try:
                        parsed_arc = fallback_arc_draft(bank, arc, arc_index=arc_index)
                    except Exception as fallback_exc:  # pragma: no cover - defensive smoke fallback
                        qa_report = [
                            *qa_report,
                            {
                                "level": "warning",
                                "code": "fallback_arc_failed",
                                "message": f"Revision fallback failed; used bare deterministic draft for arc {arc.arc_id}",
                                "error": str(fallback_exc),
                                "arc_id": arc.arc_id,
                            },
                        ]
                        parsed_arc = safe_fallback_arc_draft(bank, arc, arc_index=arc_index)
                arc_drafts[arc_index] = parsed_arc
            beats = combine_arc_drafts(arc_drafts)
        retained_warning = [
            item
            for item in qa_report
            if item.get("code")
            in {
                "invalid_arc_json",
                "invalid_revision_json",
                "invalid_final_stitch_json",
                "final_stitch_skipped_prompt_too_large",
            }
        ]
        stitch_prompt = build_final_stitch_prompt(
            bank=bank,
            beats=beats,
            qa_report=qa_report,
            revision_number=revision_count,
        )
        if len(stitch_prompt) <= MAX_FINAL_STITCH_PROMPT_CHARS:
            prompt_count += 1
            stitch_response = await client.ask(stitch_prompt)
            try:
                allowed_event_ids = {ref.event_id for beat in beats for ref in beat.source_refs}
                stitched_beats = parse_composer_response(extract_json(stitch_response), bank)
                validate_stitch_event_subset(stitched_beats, allowed_event_ids)
                beats = normalize_series_beats(stitched_beats)
                qa_report = retained_warning
            except (ValueError, json.JSONDecodeError) as exc:
                qa_report = [
                    *retained_warning,
                    {
                        "level": "warning",
                        "code": "invalid_revision_json",
                        "message": "Composer revision returned invalid JSON; kept previous valid draft",
                        "error": str(exc),
                    },
                ]
                break
        else:
            qa_report = [
                *retained_warning,
                {
                    "level": "warning",
                    "code": "final_stitch_skipped_prompt_too_large",
                    "message": "Revision final stitch prompt was too large for reliable ChatGPT browser input; kept combined arc draft",
                    "prompt_chars": len(stitch_prompt),
                    "max_prompt_chars": MAX_FINAL_STITCH_PROMPT_CHARS,
                },
            ]
        qa_report = [*qa_report, *composer_qa_report(beats, bank)]

    meta = build_review_meta(
        bank=bank,
        beats=beats,
        qa_report=qa_report,
        revision_count=revision_count,
        prompt_count=prompt_count,
    )
    return beats, meta

async def compose_with_client(
    client: ChatClient,
    bank: SeriesEventBank,
    *,
    qa_max_revisions: int = 1,
) -> tuple[list[SeriesReviewBeat], SeriesReviewMeta]:
    if qa_max_revisions < 0:
        raise ValueError("qa_max_revisions must be >= 0")
    if bank.recap_format == "episode_arc_chaptered":
        return await compose_episode_arc_chaptered_with_client(client, bank, qa_max_revisions=qa_max_revisions)
    prompt = build_composer_prompt(bank)
    revision_count = 0
    prompt_count = 0
    beats: list[SeriesReviewBeat] = []
    qa_report: list[dict[str, object]] = []
    for attempt in range(qa_max_revisions + 1):
        prompt_count += 1
        response = await client.ask(prompt)
        try:
            beats = parse_composer_response(extract_json(response), bank)
        except (ValueError, json.JSONDecodeError) as exc:
            if not beats:
                raise
            qa_report = [
                *qa_report,
                {
                    "level": "warning",
                    "code": "invalid_revision_json",
                    "message": "Composer revision returned invalid JSON; kept previous valid draft",
                    "error": str(exc),
                },
            ]
            break
        qa_report = composer_qa_report(beats, bank)
        if not needs_revision(qa_report) or attempt >= qa_max_revisions:
            break
        revision_count += 1
        prompt = build_revision_prompt(
            bank=bank,
            beats=beats,
            qa_report=qa_report,
            revision_number=revision_count,
        )
    meta = build_review_meta(
        bank=bank,
        beats=beats,
        qa_report=qa_report,
        revision_count=revision_count,
        prompt_count=prompt_count,
    )
    return beats, meta

def build_series_arc_plan(bank: SeriesEventBank) -> SeasonTargetPlan:
    if bank.season_target_plan is not None:
        return bank.season_target_plan
    arcs = build_arc_plans(bank.episode_targets, SeasonTargetSettings(arc_size=max(1, len(bank.episode_targets) or 1)))
    return SeasonTargetPlan(
        recap_format=bank.recap_format,
        detail_level="standard",
        target_total_min_s=0.0,
        target_total_max_s=0.0,
        target_total_hard_cap_s=0.0,
        episode_min_s=0.0,
        episode_normal_s=0.0,
        episode_high_s=0.0,
        arc_size=max(1, len(bank.episode_targets) or 1),
        total_target_video_s=bank.target_video_s,
        total_char_budget=bank.char_budget,
        min_total_chars=max(0, int(round(bank.char_budget * 0.85))),
        max_total_chars=max(0, int(round(bank.char_budget * 1.15))),
        episode_count=len(bank.episode_targets),
        arc_count=len(arcs),
        arcs=arcs,
        warnings=[],
    )

def build_series_composer_qa(
    *,
    bank: SeriesEventBank,
    meta: SeriesReviewMeta,
    tts_cps: float,
) -> SeriesComposerQa:
    plan = build_series_arc_plan(bank)
    revision_count = int(meta.model_versions.get("qa_revisions", "0"))
    prompt_count = int(meta.model_versions.get("prompt_count", "0"))
    return SeriesComposerQa(
        series_id=bank.series_id,
        recap_format=bank.recap_format,
        detail_level=plan.detail_level,
        target_video_s=bank.target_video_s,
        target_total_hard_cap_s=plan.target_total_hard_cap_s or None,
        char_budget=bank.char_budget,
        est_total_chars=meta.est_total_chars,
        estimated_duration_s=round(meta.est_total_chars / tts_cps, 3) if tts_cps > 0 else 0.0,
        n_events=len(bank.events),
        selected_event_ids=meta.selected_event_ids,
        qa_report=meta.qa_report,
        revision_count=revision_count,
        prompt_count=prompt_count,
        arc_count=plan.arc_count,
        warnings=meta.warnings,
        created_at=datetime.now(timezone.utc),
    )


def chapter_title(target: EpisodeTargetPlan) -> str:
    label = f"Tap {target.episode_number}" if target.episode_number is not None else target.episode_key
    if target.title and target.title.lower() not in {target.episode_key.lower(), str(target.episode_number).lower()}:
        return f"{label} - {target.title}"
    return label

def build_series_chapters(beats: list[SeriesReviewBeat], bank: SeriesEventBank) -> list[SeriesChapter]:
    if not beats:
        return []
    chapters = [SeriesChapter(title="Mo dau", start_beat_id=beats[0].beat_id, episode_key=None)]
    targets_by_episode = {target.episode_key: target for target in bank.episode_targets}
    seen: set[str] = set()
    for beat in beats[1:]:
        episode_key = beat.source_refs[0].episode_key
        if episode_key in seen:
            continue
        target = targets_by_episode.get(episode_key)
        title = chapter_title(target) if target is not None else episode_key
        chapters.append(SeriesChapter(title=title, start_beat_id=beat.beat_id, episode_key=episode_key))
        seen.add(episode_key)
    return chapters

def to_tts_review_script(beats: list[SeriesReviewBeat]) -> list[ReviewBeat]:
    output: list[ReviewBeat] = []
    for beat in beats:
        first = beat.source_refs[0]
        output.append(
            ReviewBeat(
                beat_id=beat.beat_id,
                narration=beat.narration,
                from_seg_id=first.from_seg_id,
                to_seg_id=first.to_seg_id,
                src_tc_start=first.src_tc_start,
                src_tc_end=first.src_tc_end,
                is_hook=beat.is_hook,
            )
        )
    return output
