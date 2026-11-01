from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

from common.schema import (
    AudioAsset,
    AudioAssetManifest,
    BeatEditOverride,
    BeatTiming,
    EditAudioMix,
    EditOverrides,
    EditPlan,
    EditPlanMeta,
    EditPlanQa,
    EditSpeedSegment,
    EdlPlacement,
    MusicCue,
    MusicMood,
    PlacementEdit,
    PlacementEditOverride,
    SeriesEvent,
    SeriesEventBank,
    SeriesReviewBeat,
    SfxCue,
    Shot,
)
ALGORITHM_VERSION = "anime-postprocess-v2"
MIN_TRACK_DURATION_S = 20.0
MUSIC_CROSSFADE_S = 1.5
FREEZE_DURATION_S = 0.45
FREEZE_SPACING_S = 15.0
WHOOSH_SPACING_S = 6.0
IMPACT_SPACING_S = 15.0
MAX_WHOOSH_PER_MINUTE = 6
ASPECT_RATIO = 2.35
ASPECT_MAX_RATIO = 0.08

EVENT_MOODS: dict[str, MusicMood] = {
    "setup": "calm",
    "inciting_incident": "tension",
    "conflict": "tension",
    "investigation": "mystery",
    "action": "action",
    "reveal": "suspense",
    "climax": "action",
    "ending": "emotional",
    "bridge": "calm",
    "transition": "calm",
}
MOOD_PRIORITY = {
    "action": 6,
    "suspense": 5,
    "tension": 4,
    "mystery": 3,
    "emotional": 2,
    "calm": 1,
    "default": 0,
}
GRADE_BY_MOOD: dict[MusicMood, tuple[float, float, float, float]] = {
    "default": (1.06, 1.08, 0.0, 0.0),
    "calm": (1.06, 1.08, 0.0, 0.0),
    "tension": (1.08, 1.02, -0.01, 0.0),
    "mystery": (1.08, 1.02, -0.01, 0.0),
    "suspense": (1.08, 1.02, -0.01, 0.0),
    "action": (1.10, 1.12, 0.0, 0.0),
    "emotional": (1.05, 1.06, 0.0, 0.08),
}
STRONG_GRADE_EVENT_TYPES = {"action", "reveal", "climax"}


def _stable_unit(seed: int, *parts: object) -> float:
    payload = ":".join([str(seed), *(str(part) for part in parts)])
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _event_mood(event: SeriesEvent) -> MusicMood:
    return EVENT_MOODS.get(event.event_type.lower(), "default")


def _events_for_beat(beat: SeriesReviewBeat, events_by_id: dict[str, SeriesEvent]) -> list[SeriesEvent]:
    return [events_by_id[ref.event_id] for ref in beat.source_refs if ref.event_id in events_by_id]


def _beat_mood(beat: SeriesReviewBeat, events: list[SeriesEvent]) -> MusicMood:
    if beat.is_hook:
        return "tension"
    moods = [_event_mood(event) for event in events]
    return max(moods, key=lambda mood: MOOD_PRIORITY[mood], default="default")


def _beat_role(beat: SeriesReviewBeat, events: list[SeriesEvent]) -> str:
    if beat.is_hook:
        return "hook"
    event_types = {event.event_type.lower() for event in events}
    if "climax" in event_types:
        return "climax"
    if "reveal" in event_types:
        return "reveal"
    if event_types & {"bridge", "transition"} or len({ref.episode_key for ref in beat.source_refs}) > 1:
        return "transition"
    if "action" in event_types:
        return "action"
    if "conflict" in event_types or "inciting_incident" in event_types:
        return "conflict"
    if "ending" in event_types:
        return "ending"
    if "investigation" in event_types:
        return "investigation"
    if "setup" in event_types:
        return "setup"
    return "neutral"


def _beat_importance(events: list[SeriesEvent]) -> float:
    return max((event.importance for event in events), default=0.5)


def _chapter_transition_beat_ids(
    beats: list[SeriesReviewBeat], events_by_id: dict[str, SeriesEvent]
) -> set[int]:
    """Beats that open a new episode chapter.

    Chaptered season scripts keep every beat inside one episode, so `_beat_role`
    never sees a multi-episode beat and the transition ramp would never fire.
    The chapter opening is the real transition in the edit; reveal/climax beats
    stay excluded because they never receive an auto ramp.
    """
    transition_ids: set[int] = set()
    previous_episode: str | None = None
    for beat in sorted(beats, key=lambda item: item.beat_id):
        if not beat.source_refs:
            continue
        opening_episode = beat.source_refs[0].episode_key
        if (
            previous_episode is not None
            and opening_episode != previous_episode
            and not beat.is_hook
            and _beat_role(beat, _events_for_beat(beat, events_by_id)) not in {"reveal", "climax"}
        ):
            transition_ids.add(beat.beat_id)
        previous_episode = beat.source_refs[-1].episode_key
    return transition_ids


def _visual_grade(
    mood: MusicMood,
    role: str,
    events: list[SeriesEvent],
) -> tuple[float, float, float, float]:
    event_types = {event.event_type.lower() for event in events}
    if role in STRONG_GRADE_EVENT_TYPES or event_types & STRONG_GRADE_EVENT_TYPES:
        return GRADE_BY_MOOD["action"]
    return GRADE_BY_MOOD[mood]


def _shot_for_placement(
    placement: EdlPlacement,
    beat: SeriesReviewBeat,
    shots_by_episode: dict[str, list[Shot]],
) -> tuple[str | None, Shot | None]:
    episode_keys = [ref.episode_key for ref in beat.source_refs if ref.src == placement.src]
    for episode_key in dict.fromkeys(episode_keys):
        shots = shots_by_episode.get(episode_key, [])
        if placement.shot_index < len(shots):
            shot = shots[placement.shot_index]
            if shot.index == placement.shot_index:
                return episode_key, shot
        for shot in shots:
            if shot.index == placement.shot_index:
                return episode_key, shot
    return None, None


def _speed_ramp() -> list[EditSpeedSegment]:
    return [
        EditSpeedSegment(start_ratio=0.0, end_ratio=0.733333, speed=1.0),
        EditSpeedSegment(start_ratio=0.733333, end_ratio=0.866667, speed=1.15),
        EditSpeedSegment(start_ratio=0.866667, end_ratio=1.0, speed=1.35),
    ]


def _ramp_extension(duration_s: float, segments: list[EditSpeedSegment]) -> float:
    extension = sum(duration_s * (segment.end_ratio - segment.start_ratio) * (segment.speed - 1.0) for segment in segments)
    return round(max(0.0, extension), 6)


def _neutralize(edit: PlacementEdit) -> PlacementEdit:
    return edit.model_copy(
        update={
            "source_extension_s": 0.0,
            "zoom_start": 1.0,
            "zoom_end": 1.0,
            "pan_x": 0.0,
            "pan_y": 0.0,
            "aspect_ratio": None,
            "contrast": 1.0,
            "saturation": 1.0,
            "brightness": 0.0,
            "warmth": 0.0,
            "speed_ramp": [],
            "freeze_duration_s": 0.0,
            "disabled": True,
        }
    )


def _override_values(override: BeatEditOverride | PlacementEditOverride) -> dict[str, Any]:
    values = override.model_dump(
        exclude={"beat_id", "placement_index", "track_id", "ramp", "freeze", "disable_effects", "mood"}
    )
    return {key: value for key, value in values.items() if value is not None}


def _apply_visual_override(
    edit: PlacementEdit,
    override: BeatEditOverride | PlacementEditOverride,
    *,
    placement: EdlPlacement,
    shot: Shot | None,
    skipped_effects: list[dict[str, Any]],
) -> PlacementEdit:
    if override.disable_effects is True:
        return _neutralize(edit)
    update: dict[str, Any] = {}
    if override.mood is not None:
        contrast, saturation, brightness, warmth = _visual_grade(override.mood, edit.role, [])
        if edit.role == "hook" and edit.contrast >= 1.10 and edit.saturation >= 1.12:
            contrast, saturation, brightness, warmth = GRADE_BY_MOOD["action"]
        update.update(
            {
                "mood": override.mood,
                "contrast": contrast,
                "saturation": saturation,
                "brightness": brightness,
                "warmth": warmth,
            }
        )
    update.update(_override_values(override))
    if override.disable_effects is False:
        update["disabled"] = False
    edit = edit.model_copy(update=update)
    if override.ramp is False:
        edit = edit.model_copy(update={"speed_ramp": [], "source_extension_s": 0.0})
    elif override.ramp is True:
        segments = _speed_ramp()
        extension = _ramp_extension(placement.tl_end - placement.tl_start, segments)
        if shot is not None and shot.tc_end - placement.src_out >= extension - 1e-6:
            edit = edit.model_copy(update={"speed_ramp": segments, "source_extension_s": extension})
        else:
            skipped_effects.append(
                {
                    "placement_index": edit.placement_index,
                    "effect": "speed_ramp",
                    "reason": "override_requested_without_source_headroom",
                }
            )
    if override.freeze is False:
        edit = edit.model_copy(update={"freeze_duration_s": 0.0})
    elif override.freeze is True:
        duration = placement.tl_end - placement.tl_start
        if duration >= 2.0 and not placement.reused and shot is not None and shot.motion_score > 0.01:
            edit = edit.model_copy(update={"freeze_duration_s": FREEZE_DURATION_S})
        else:
            skipped_effects.append(
                {
                    "placement_index": edit.placement_index,
                    "effect": "freeze",
                    "reason": "override_requested_on_short_reused_or_static_placement",
                }
            )
    return PlacementEdit.model_validate(edit.model_dump(mode="json"))


def _build_base_edits(
    *,
    placements: list[EdlPlacement],
    beats_by_id: dict[int, SeriesReviewBeat],
    events_by_id: dict[str, SeriesEvent],
    shots_by_episode: dict[str, list[Shot]],
    overrides: EditOverrides,
    seed: int,
    chapter_transition_beat_ids: set[int],
    skipped_effects: list[dict[str, Any]],
    source_bound_checks: list[dict[str, Any]],
) -> tuple[list[PlacementEdit], dict[int, str]]:
    beat_overrides = {item.beat_id: item for item in overrides.beats}
    placement_overrides = {item.placement_index: item for item in overrides.placements}
    track_overrides: dict[int, str] = {item.beat_id: item.track_id for item in overrides.beats if item.track_id}
    placement_indexes_by_beat: dict[int, list[int]] = defaultdict(list)
    shot_by_placement: dict[int, Shot | None] = {}
    placement_models: list[PlacementEdit] = []

    for index, placement in enumerate(placements):
        beat = beats_by_id[placement.beat_id]
        events = _events_for_beat(beat, events_by_id)
        mood = _beat_mood(beat, events)
        role = _beat_role(beat, events)
        contrast, saturation, brightness, warmth = _visual_grade(mood, role, events)
        zoom = round(1.02 + 0.03 * _stable_unit(seed, "zoom", index), 4)
        pan_direction = -1.0 if index % 2 else 1.0
        pan_x = round(pan_direction * 0.04 * _stable_unit(seed, "pan", index), 4)
        episode_key, shot = _shot_for_placement(placement, beat, shots_by_episode)
        shot_by_placement[index] = shot
        in_bounds = bool(
            shot is not None
            and placement.src_in >= shot.tc_start - 1e-3
            and placement.src_out <= shot.tc_end + 1e-3
        )
        source_bound_checks.append(
            {
                "placement_index": index,
                "episode_key": episode_key,
                "shot_index": placement.shot_index,
                "in_bounds": in_bounds,
                "source_headroom_s": round(max(0.0, shot.tc_end - placement.src_out), 3) if shot else 0.0,
            }
        )
        if not in_bounds:
            skipped_effects.append(
                {"placement_index": index, "effect": "source_bound", "reason": "placement_not_within_indexed_shot"}
            )
        placement_models.append(
            PlacementEdit(
                placement_index=index,
                beat_id=placement.beat_id,
                mood=mood,
                role=role,
                src_in=placement.src_in,
                src_out=placement.src_out,
                zoom_start=zoom,
                zoom_end=zoom,
                pan_x=pan_x,
                contrast=contrast,
                saturation=saturation,
                brightness=brightness,
                warmth=warmth,
            )
        )
        placement_indexes_by_beat[placement.beat_id].append(index)

    freeze_cursor = -FREEZE_SPACING_S
    for beat_id, indexes in placement_indexes_by_beat.items():
        beat = beats_by_id[beat_id]
        events = _events_for_beat(beat, events_by_id)
        role = _beat_role(beat, events)
        importance = _beat_importance(events)
        key_beat = role in {"hook", "reveal", "climax"} or importance >= 0.85
        target_index = indexes[-1]
        target = placements[target_index]
        edit = placement_models[target_index]
        if key_beat:
            edit = edit.model_copy(update={"zoom_start": 1.02, "zoom_end": 1.08})
            target_duration = target.tl_end - target.tl_start
            freeze_at = target.tl_end - FREEZE_DURATION_S
            target_shot = shot_by_placement[target_index]
            if (
                target_duration >= 2.0
                and not target.reused
                and freeze_at - freeze_cursor >= FREEZE_SPACING_S - 1e-6
                and target_shot is not None
                and target_shot.motion_score > 0.01
            ):
                edit = edit.model_copy(update={"freeze_duration_s": FREEZE_DURATION_S})
                freeze_cursor = freeze_at
            else:
                skipped_effects.append(
                    {
                        "placement_index": target_index,
                        "effect": "freeze",
                        "reason": "short_reused_static_missing_frames_or_spacing",
                    }
                )
        if role == "transition" or beat_id in chapter_transition_beat_ids:
            segments = _speed_ramp()
            extension = _ramp_extension(target.tl_end - target.tl_start, segments)
            shot = shot_by_placement[target_index]
            if edit.freeze_duration_s > 0:
                # A freeze already owns the tail of this placement; ramping it too
                # would fight the same frames.
                skipped_effects.append(
                    {"placement_index": target_index, "effect": "speed_ramp", "reason": "freeze_conflict"}
                )
            elif shot is not None and shot.tc_end - target.src_out >= extension - 1e-6:
                edit = edit.model_copy(update={"speed_ramp": segments, "source_extension_s": extension})
            else:
                skipped_effects.append(
                    {"placement_index": target_index, "effect": "speed_ramp", "reason": "insufficient_source_headroom"}
                )
        placement_models[target_index] = PlacementEdit.model_validate(edit.model_dump(mode="json"))

    aspect_limit = int(len(placement_models) * ASPECT_MAX_RATIO)
    aspect_candidates = [edit.placement_index for edit in placement_models if edit.role in {"hook", "reveal", "climax"}]
    aspect_candidates.sort(key=lambda index: _stable_unit(seed, "aspect", index))
    used_aspect_beats: set[int] = set()
    for index in aspect_candidates:
        if len(used_aspect_beats) >= aspect_limit:
            break
        edit = placement_models[index]
        if edit.beat_id in used_aspect_beats:
            continue
        placement_models[index] = edit.model_copy(update={"aspect_ratio": ASPECT_RATIO})
        used_aspect_beats.add(edit.beat_id)

    for index, edit in enumerate(placement_models):
        placement = placements[index]
        shot = shot_by_placement[index]
        beat_override = beat_overrides.get(edit.beat_id)
        if beat_override is not None:
            if index != placement_indexes_by_beat[edit.beat_id][-1]:
                beat_override = beat_override.model_copy(update={"ramp": None, "freeze": None})
            edit = _apply_visual_override(
                edit,
                beat_override,
                placement=placement,
                shot=shot,
                skipped_effects=skipped_effects,
            )
        placement_override = placement_overrides.get(index)
        if placement_override is not None:
            edit = _apply_visual_override(
                edit,
                placement_override,
                placement=placement,
                shot=shot,
                skipped_effects=skipped_effects,
            )
            if placement_override.track_id:
                track_overrides[edit.beat_id] = placement_override.track_id
        placement_models[index] = edit

    last_freeze_at = -FREEZE_SPACING_S
    frozen_beats: set[int] = set()
    for index, edit in enumerate(placement_models):
        if edit.freeze_duration_s <= 0:
            continue
        freeze_at = placements[index].tl_end - edit.freeze_duration_s
        if edit.beat_id in frozen_beats or freeze_at - last_freeze_at < FREEZE_SPACING_S - 1e-6:
            placement_models[index] = edit.model_copy(update={"freeze_duration_s": 0.0})
            skipped_effects.append(
                {
                    "placement_index": index,
                    "effect": "freeze",
                    "reason": "freeze_limit_per_beat_or_global_spacing",
                }
            )
            continue
        frozen_beats.add(edit.beat_id)
        last_freeze_at = freeze_at
    return placement_models, track_overrides


def _music_assets(manifest: AudioAssetManifest) -> dict[MusicMood, list[AudioAsset]]:
    result: dict[MusicMood, list[AudioAsset]] = defaultdict(list)
    for asset in manifest.assets:
        if asset.kind == "music" and asset.mood is not None:
            result[asset.mood].append(asset)
    for assets in result.values():
        assets.sort(key=lambda asset: asset.asset_id)
    return result


def _select_track(
    *,
    mood: MusicMood,
    selection_index: int,
    seed: int,
    music: dict[MusicMood, list[AudioAsset]],
    override_id: str | None,
    assets_by_id: dict[str, AudioAsset],
    asset_warnings: list[str],
) -> AudioAsset:
    if override_id:
        override = assets_by_id.get(override_id)
        if override is not None and override.kind == "music" and override.loopable:
            return override
        asset_warnings.append(f"music override {override_id} is missing, not music, or not loopable; used fallback")
    candidates = [asset for asset in music.get(mood, []) if asset.loopable]
    if not candidates:
        candidates = [asset for asset in music["default"] if asset.loopable]
        asset_warnings.append(f"no loopable music for mood={mood}; used default")
    seed_offset = int(_stable_unit(seed, "music", mood) * len(candidates)) % len(candidates)
    return candidates[(seed_offset + selection_index) % len(candidates)]


def _build_music_cues(
    *,
    beats: list[SeriesReviewBeat],
    timings: list[BeatTiming],
    total_duration_s: float,
    moods_by_beat: dict[int, MusicMood],
    track_overrides: dict[int, str],
    manifest: AudioAssetManifest,
    seed: int,
    asset_warnings: list[str],
) -> list[MusicCue]:
    timing_by_beat = {timing.beat_id: timing for timing in timings}
    music = _music_assets(manifest)
    assets_by_id = {asset.asset_id: asset for asset in manifest.assets}
    first_beat = beats[0]
    cue_start = 0.0
    cue_mood = moods_by_beat[first_beat.beat_id]
    cue_beat_id = first_beat.beat_id
    cues: list[MusicCue] = []
    selection_counts: dict[MusicMood, int] = defaultdict(int)
    for beat in beats[1:]:
        timing = timing_by_beat[beat.beat_id]
        mood = moods_by_beat[beat.beat_id]
        explicit_track = track_overrides.get(beat.beat_id)
        should_switch = (
            mood != cue_mood
            and timing.tl_start - cue_start >= MIN_TRACK_DURATION_S - 1e-6
            and total_duration_s - timing.tl_start >= MIN_TRACK_DURATION_S - 1e-6
        )
        if explicit_track is not None:
            should_switch = True
        if not should_switch:
            continue
        track = _select_track(
            mood=cue_mood,
            selection_index=selection_counts[cue_mood],
            seed=seed,
            music=music,
            override_id=track_overrides.get(cue_beat_id),
            assets_by_id=assets_by_id,
            asset_warnings=asset_warnings,
        )
        selection_counts[cue_mood] += 1
        cues.append(
            MusicCue(
                asset_id=track.asset_id,
                tl_start=round(cue_start, 3),
                tl_end=round(timing.tl_start, 3),
                mood=cue_mood,
                crossfade_s=0.0 if not cues else MUSIC_CROSSFADE_S,
            )
        )
        cue_start = timing.tl_start
        cue_mood = mood
        cue_beat_id = beat.beat_id
    track = _select_track(
        mood=cue_mood,
        selection_index=selection_counts[cue_mood],
        seed=seed,
        music=music,
        override_id=track_overrides.get(cue_beat_id),
        assets_by_id=assets_by_id,
        asset_warnings=asset_warnings,
    )
    selection_counts[cue_mood] += 1
    cues.append(
        MusicCue(
            asset_id=track.asset_id,
            tl_start=round(cue_start, 3),
            tl_end=round(total_duration_s, 3),
            mood=cue_mood,
            crossfade_s=0.0 if not cues else MUSIC_CROSSFADE_S,
        )
    )
    return cues


def _sfx_asset(manifest: AudioAssetManifest, kind: Literal["whoosh", "impact"], seed: int, index: int) -> AudioAsset:
    candidates = sorted(
        (asset for asset in manifest.assets if asset.kind == "sfx" and asset.sfx_kind == kind),
        key=lambda asset: asset.asset_id,
    )
    offset = int(_stable_unit(seed, "sfx", kind, index) * len(candidates)) % len(candidates)
    return candidates[offset]


def _within_density(existing: list[float], at: float, *, spacing_s: float, max_per_minute: int | None = None) -> bool:
    if existing and at - existing[-1] < spacing_s - 1e-6:
        return False
    if max_per_minute is not None and sum(1 for cue_at in existing if at - cue_at < 60.0) >= max_per_minute:
        return False
    return True


def _build_sfx_cues(
    placements: list[EdlPlacement], edits: list[PlacementEdit], manifest: AudioAssetManifest, seed: int
) -> list[SfxCue]:
    cues: list[SfxCue] = []
    whoosh_times: list[float] = []
    impact_times: list[float] = []
    for placement, edit in zip(placements, edits, strict=True):
        if edit.disabled:
            continue
        if edit.speed_ramp:
            at = placement.tl_start + (placement.tl_end - placement.tl_start) * edit.speed_ramp[-2].start_ratio
            if _within_density(whoosh_times, at, spacing_s=WHOOSH_SPACING_S, max_per_minute=MAX_WHOOSH_PER_MINUTE):
                asset = _sfx_asset(manifest, "whoosh", seed, len(whoosh_times))
                cues.append(SfxCue(asset_id=asset.asset_id, kind="whoosh", tl_start=round(at, 3), gain_db=-14.0))
                whoosh_times.append(at)
        if edit.freeze_duration_s > 0:
            at = placement.tl_end - edit.freeze_duration_s
            if _within_density(impact_times, at, spacing_s=IMPACT_SPACING_S):
                asset = _sfx_asset(manifest, "impact", seed, len(impact_times))
                cues.append(SfxCue(asset_id=asset.asset_id, kind="impact", tl_start=round(at, 3), gain_db=-10.0))
                impact_times.append(at)
    return sorted(cues, key=lambda cue: (cue.tl_start, cue.kind, cue.asset_id))


def _attribution_text(manifest: AudioAssetManifest, plan: EditPlan) -> str:
    selected_ids = {cue.asset_id for cue in plan.music_cues} | {cue.asset_id for cue in plan.sfx_cues}
    selected = [asset for asset in manifest.assets if asset.asset_id in selected_ids]
    lines = ["Audio assets used by Anime Season Enhanced Post-Production V1", ""]
    if not any(asset.attribution_required for asset in selected):
        lines.extend(["No attribution is required for the selected audio assets.", ""])
    for asset in selected:
        detail = asset.attribution or "No attribution text required"
        lines.append(f"- {asset.asset_id}: {detail}")
        lines.append(f"  License: {asset.license_source}")
        if asset.license_url:
            lines.append(f"  License URL: {asset.license_url}")
        lines.append(f"  File: {asset.path}")
    return "\n".join(lines).rstrip() + "\n"


def build_edit_plan(
    *,
    placements: list[EdlPlacement],
    beats: list[SeriesReviewBeat],
    timings: list[BeatTiming],
    event_bank: SeriesEventBank,
    shots_by_episode: dict[str, list[Shot]],
    audio_manifest: AudioAssetManifest,
    input_fingerprint: str,
    seed: int = 1234,
    profile: str = "dynamic_anime",
    overrides: EditOverrides | None = None,
) -> tuple[EditPlan, EditPlanMeta, EditPlanQa, str]:
    if profile != "dynamic_anime":
        raise ValueError(f"unsupported postprocess profile: {profile}")
    if not placements or not beats or not timings:
        raise ValueError("postprocess requires non-empty EDL, review script, and timings")
    beats_by_id = {beat.beat_id: beat for beat in beats}
    missing_beats = sorted({placement.beat_id for placement in placements} - set(beats_by_id))
    if missing_beats:
        raise ValueError(f"EDL references unknown beat ids: {missing_beats}")
    events_by_id = {event.event_id: event for event in event_bank.events}
    missing_event_ids = sorted(
        {ref.event_id for beat in beats for ref in beat.source_refs} - set(events_by_id)
    )
    if missing_event_ids:
        raise ValueError(f"series review references unknown event ids: {missing_event_ids}")
    if {timing.beat_id for timing in timings} != set(beats_by_id):
        raise ValueError("beats_timing ids must match the series review beat ids")
    overrides = overrides or EditOverrides()
    unknown_override_beats = sorted({item.beat_id for item in overrides.beats} - set(beats_by_id))
    if unknown_override_beats:
        raise ValueError(f"edit overrides reference unknown beat ids: {unknown_override_beats}")
    invalid_override_placements = sorted(
        item.placement_index for item in overrides.placements if item.placement_index >= len(placements)
    )
    if invalid_override_placements:
        raise ValueError(f"edit overrides reference unknown placement indexes: {invalid_override_placements}")
    skipped_effects: list[dict[str, Any]] = []
    source_bound_checks: list[dict[str, Any]] = []
    asset_warnings: list[str] = []
    edits, track_overrides = _build_base_edits(
        placements=placements,
        beats_by_id=beats_by_id,
        events_by_id=events_by_id,
        shots_by_episode=shots_by_episode,
        overrides=overrides,
        seed=seed,
        chapter_transition_beat_ids=_chapter_transition_beat_ids(beats, events_by_id),
        skipped_effects=skipped_effects,
        source_bound_checks=source_bound_checks,
    )
    for check, edit in zip(source_bound_checks, edits, strict=True):
        headroom = float(check["source_headroom_s"])
        check["required_extension_s"] = round(edit.source_extension_s, 3)
        check["effect_in_bounds"] = bool(check["in_bounds"] and headroom + 1e-3 >= edit.source_extension_s)
    moods_by_beat: dict[int, MusicMood] = {}
    placement_mood_overrides: dict[int, MusicMood] = {}
    for override in sorted(overrides.placements, key=lambda item: item.placement_index):
        if override.mood is not None and override.placement_index < len(placements):
            placement_mood_overrides[placements[override.placement_index].beat_id] = override.mood
    for beat in beats:
        beat_edits = [edit for edit in edits if edit.beat_id == beat.beat_id]
        moods_by_beat[beat.beat_id] = placement_mood_overrides.get(
            beat.beat_id,
            beat_edits[0].mood if beat_edits else _beat_mood(beat, _events_for_beat(beat, events_by_id)),
        )
    total_duration_s = round(placements[-1].tl_end, 3)
    music_cues = _build_music_cues(
        beats=beats,
        timings=timings,
        total_duration_s=total_duration_s,
        moods_by_beat=moods_by_beat,
        track_overrides=track_overrides,
        manifest=audio_manifest,
        seed=seed,
        asset_warnings=asset_warnings,
    )
    sfx_cues = _build_sfx_cues(placements, edits, audio_manifest, seed)
    plan = EditPlan(
        profile=profile,
        seed=seed,
        total_duration_s=total_duration_s,
        placements=edits,
        music_cues=music_cues,
        sfx_cues=sfx_cues,
        audio_mix=EditAudioMix(),
        original_audio_included=False,
    )
    warnings: list[str] = []
    failed_bounds = sum(1 for check in source_bound_checks if not check["in_bounds"])
    if failed_bounds:
        warnings.append(f"{failed_bounds} placements could not be verified inside their indexed shots")
    warnings.extend(asset_warnings)
    duration_minutes = total_duration_s / 60.0
    whoosh_count = sum(1 for cue in sfx_cues if cue.kind == "whoosh")
    impact_count = sum(1 for cue in sfx_cues if cue.kind == "impact")
    qa = EditPlanQa(
        warnings=warnings,
        skipped_effects=skipped_effects,
        source_bound_checks=source_bound_checks,
        cue_density={
            "music_coverage_s": total_duration_s,
            "music_coverage_ratio": 1.0,
            "whoosh_count": whoosh_count,
            "impact_count": impact_count,
            "whoosh_per_minute": round(whoosh_count / duration_minutes, 3) if duration_minutes else 0.0,
            "impact_per_minute": round(impact_count / duration_minutes, 3) if duration_minutes else 0.0,
        },
        asset_warnings=asset_warnings,
    )
    meta = EditPlanMeta(
        algorithm_version=ALGORITHM_VERSION,
        input_fingerprint=input_fingerprint,
        n_placements=len(edits),
        n_zoom=sum(1 for edit in edits if edit.zoom_start != 1.0 or edit.zoom_end != 1.0),
        n_aspect=sum(1 for edit in edits if edit.aspect_ratio is not None),
        n_speed_ramp=sum(1 for edit in edits if edit.speed_ramp),
        n_freeze=sum(1 for edit in edits if edit.freeze_duration_s > 0),
        n_music_cues=len(music_cues),
        n_sfx_cues=len(sfx_cues),
        created_at=datetime.now(timezone.utc),
    )
    return plan, meta, qa, _attribution_text(audio_manifest, plan)
