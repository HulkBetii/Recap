from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from common.inputs import load_series_manifest
from common.schema import (
    EpisodeTargetPlan,
    EpisodeMemory,
    EpisodeMeta,
    FilmMapMeta,
    FilmMapSegment,
    ReviewBeat,
    SeriesChapter,
    SeriesEvent,
    SeriesEventBank,
    SeriesManifest,
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
    "non_monotonic_episode_order",
    "non_monotonic_story_order",
}


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
    if recap_format == "episode_chaptered":
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
    if recap_format == "episode_chaptered":
        return min(event_count, max(2, round(char_budget / 520) or 1))
    return min(event_count, max(1, round(char_budget / 360) or 1))

def chaptered_episode_targets(bank: SeriesEventBank) -> list[EpisodeTargetPlan]:
    return [target for target in bank.episode_targets if target.char_budget > 0 and target.target_beats > 0]


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
) -> SeriesEventBank:
    manifest = load_series_manifest(manifest_path)
    ratios = {**MODE_TARGET_RATIOS, **(mode_target_ratios or {})}
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
                recap_mode=meta.recap_mode,
                source_duration_s=round(film_meta.duration, 3),
                story_duration_s=round(episode_story_duration, 3),
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
        events=events,
        warnings=warnings,
        created_at=datetime.now(timezone.utc),
    )


def composer_length_plan(bank: SeriesEventBank) -> ComposerLengthPlan:
    event_count = max(1, len(bank.events))
    episode_count = max(1, len(bank.episode_keys))
    if bank.recap_format == "episode_chaptered":
        targets = chaptered_episode_targets(bank)
        min_beats = min(event_count, max(1, len(targets) + 1))
        desired_beats = 1 + sum(target.target_beats for target in targets)
        max_beats = min(event_count, max(min_beats, desired_beats))
        target_beats = max(min_beats, min(max_beats, round(bank.char_budget / 480) or min_beats))
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
- No OP/ED/theme-song/preview/recap-only content.
- No verbatim dialogue or lyrics; transform into Vietnamese commentary.
- Target total narration around {bank.char_budget} Vietnamese characters and {bank.target_video_s:.1f}s; stay between {length.min_total_chars} and {length.max_total_chars} characters unless the event bank is too small.
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
- No OP/ED/theme-song/preview/recap-only content.
- No verbatim dialogue or lyrics; transform into Vietnamese commentary.
- Target total narration around {bank.char_budget} Vietnamese characters and {bank.target_video_s:.1f}s; stay between {length.min_total_chars} and {length.max_total_chars} characters unless the event bank is too small.
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


def parse_composer_response(payload: object, bank: SeriesEventBank) -> list[SeriesReviewBeat]:
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
            if event_id not in events_by_id:
                raise ValueError(f"series composer selected unknown event_id: {event_id}")
            source_refs.append(source_ref_from_event(events_by_id[str(event_id)]))
        beats.append(
            SeriesReviewBeat(
                beat_id=index,
                narration=str(raw.get("narration", "")),
                source_refs=source_refs,
                is_hook=bool(raw.get("is_hook", index == 0)),
            )
        )
    if beats:
        beats[0] = beats[0].model_copy(update={"is_hook": True})
    return validate_series_review_script(beats)

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

    total_chars = sum(len(beat.narration) for beat in beats)
    if total_chars < bank.char_budget * 0.85:
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

    if bank.recap_format == "episode_chaptered":
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
            if events_by_id.get(event_id) is not None and events_by_id[event_id].event_type == "non_story"
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
- Total narration must be {length.min_total_chars}-{length.max_total_chars} Vietnamese characters.
- Keep {length.min_beats}-{length.max_beats} beats and make most beats {length.per_beat_min_chars}-{length.per_beat_max_chars} characters.
- Keep beat 0 as the cold-open hook; after beat 0, continue chronological episode/story order.
- If format is episode_chaptered, every episode in EPISODE_TARGET_PLAN with target_beats > 0 must have a non-hook chapter after the hook.
- If QA_REPORT contains under_target_length or episode_under_char_budget, expand the episode chapters with concrete cause/effect details. Being concise is a failure for episode_chaptered mode.
- Do not reuse any event_id, including the hook event.
- Return ONLY JSON with the same schema.

REVISION_NUMBER: {revision_number}
QA_REPORT:
{json.dumps(qa_report, ensure_ascii=False)}

PREVIOUS_DRAFT:
{json.dumps(beats_to_prompt_payload(beats), ensure_ascii=False)}
""".strip()

async def compose_with_client(
    client: ChatClient,
    bank: SeriesEventBank,
    *,
    qa_max_revisions: int = 1,
) -> tuple[list[SeriesReviewBeat], SeriesReviewMeta]:
    if qa_max_revisions < 0:
        raise ValueError("qa_max_revisions must be >= 0")
    prompt = build_composer_prompt(bank)
    revision_count = 0
    beats: list[SeriesReviewBeat] = []
    qa_report: list[dict[str, object]] = []
    for attempt in range(qa_max_revisions + 1):
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
    selected_event_ids = list(dict.fromkeys(ref.event_id for beat in beats for ref in beat.source_refs))
    meta = SeriesReviewMeta(
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
        },
        warnings=bank.warnings + [str(item["message"]) for item in qa_report if item.get("level") == "warning"],
        created_at=datetime.now(timezone.utc),
    )
    return beats, meta


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
