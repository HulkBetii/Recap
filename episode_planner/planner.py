from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from common.integrity import file_hash, media_identity_hash
from common.inputs import load_anime_context, load_series_manifest
from common.schema import (
    AnimeContext,
    EpisodeMemory,
    EpisodeMemoryEntry,
    EpisodeMeta,
    EpisodeScoreSignals,
    EpisodeTimecodeHook,
    FilmMapMeta,
    FilmMapSegment,
    ResolvedRecapMode,
    SeriesManifest,
    SeriesManifestEpisode,
    StorySection,
    VideoProfile,
    validate_film_map,
    validate_story_map,
)
from episode_planner.integrity import EPISODE_PLANNER_CACHE_VERSION, episode_planner_config_hash

REVEAL_TERMS = (
    "reveal",
    "reveals",
    "secret",
    "truth",
    "hidden",
    "identity",
    "discover",
    "discovers",
    "realize",
    "realizes",
    "mystery",
    "clue",
    "curse",
)
STATE_CHANGE_TERMS = (
    "decide",
    "decides",
    "betray",
    "betrays",
    "joins",
    "leaves",
    "dies",
    "death",
    "kill",
    "killed",
    "save",
    "saved",
    "confess",
    "awakens",
    "transform",
    "contract",
    "escape",
)
ACTION_TERMS = (
    "fight",
    "battle",
    "attack",
    "attacks",
    "chase",
    "run",
    "blood",
    "monster",
    "demon",
    "sword",
    "explosion",
    "shoot",
    "hit",
)
CONTINUITY_TERMS = (
    "promise",
    "mission",
    "clue",
    "arc",
    "next",
    "again",
    "past",
    "memory",
    "family",
    "rival",
    "villain",
    "goal",
)
NON_STORY_LABELS = {
    "opening_theme",
    "ending_theme",
    "next_episode_preview",
    "eyecatch",
    "recap_previous_episode",
    "sponsor_card",
    "title_card",
    "studio_logo",
}
ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9'-]{2,}\b")
EPISODE_RE = re.compile(
    r"(?:s(?P<season>\d{1,2})[\s._-]*e(?P<episode>\d{1,3})|(?P<season_alt>\d{1,2})x(?P<episode_alt>\d{1,3})|(?:ep(?:isode)?|e)[\s._-]*(?P<episode_only>\d{1,3}))",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class EpisodeIdentity:
    series_id: str
    episode_key: str
    episode_number: int | str | None
    title: str | None
    source_path: Path
    arc: str | None
    spoiler_limit_episode: int | str | None
    warnings: list[str]

@dataclass(frozen=True)
class EpisodePlanSettings:
    recap_mode: str = "auto"
    episode_key: str | None = None
    episode_number: int | str | None = None
    recap_full_threshold: float = 0.70
    recap_quick_threshold: float = 0.35
    recap_merge_threshold: float = 0.15
    quick_target_ratio: float = 0.12
    quick_min_coverage: float = 0.45

def load_film_map(path: Path) -> tuple[list[FilmMapSegment], float]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    segments = [FilmMapSegment.model_validate(item) for item in raw]
    duration = max((segment.tc_end for segment in segments), default=0.001)
    meta_path = path.with_name("film_map.meta.json")
    if meta_path.is_file():
        duration = FilmMapMeta.model_validate_json(meta_path.read_text(encoding="utf-8-sig")).duration
    return validate_film_map(segments, duration=duration), duration

def load_story_sections(path: Path | None, duration_s: float) -> list[StorySection]:
    if path is None or not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return validate_story_map([StorySection.model_validate(item) for item in raw], duration=duration_s)

def load_video_profile(path: Path | None) -> VideoProfile | None:
    if path is None or not path.is_file():
        return None
    return VideoProfile.model_validate_json(path.read_text(encoding="utf-8-sig"))

def normalize_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "episode"

def numeric_episode(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None

def parse_episode_from_filename(path: Path) -> tuple[str | None, int | None]:
    match = EPISODE_RE.search(path.stem)
    if not match:
        return None, None
    season = match.group("season") or match.group("season_alt")
    episode = match.group("episode") or match.group("episode_alt") or match.group("episode_only")
    if not episode:
        return None, None
    episode_number = int(episode)
    if season:
        return f"s{int(season):02d}e{episode_number:02d}", episode_number
    return f"e{episode_number:02d}", episode_number

def _episode_source_path(episode: SeriesManifestEpisode, manifest_path: Path | None) -> Path | None:
    if not episode.source_path:
        return None
    source = Path(episode.source_path).expanduser()
    if not source.is_absolute() and manifest_path is not None:
        source = manifest_path.parent / source
    return source.resolve()

def _top_level_episode(manifest: SeriesManifest) -> SeriesManifestEpisode | None:
    if any((manifest.episode_key, manifest.episode_number, manifest.source_path, manifest.title, manifest.arc)):
        return SeriesManifestEpisode(
            episode_key=manifest.episode_key,
            episode_number=manifest.episode_number,
            title=manifest.title,
            source_path=manifest.source_path,
            arc=manifest.arc,
            spoiler_limit_episode=manifest.spoiler_limit_episode,
        )
    return None

def select_manifest_episode(
    manifest: SeriesManifest,
    *,
    manifest_path: Path,
    film: Path,
    episode_key: str | None,
    episode_number: int | str | None,
) -> tuple[SeriesManifestEpisode | None, list[str]]:
    warnings: list[str] = []
    candidates = list(manifest.episodes)
    top_level = _top_level_episode(manifest)
    if top_level is not None:
        candidates.insert(0, top_level)
    if not candidates:
        return None, warnings
    if episode_key:
        for episode in candidates:
            if episode.episode_key == episode_key:
                return episode, warnings
        warnings.append(f"episode_key override '{episode_key}' was not found in series_manifest")
    target_number = numeric_episode(episode_number)
    if target_number is not None:
        for episode in candidates:
            if numeric_episode(episode.episode_number) == target_number:
                return episode, warnings
        warnings.append(f"episode_number override '{episode_number}' was not found in series_manifest")
    film_resolved = film.expanduser().resolve()
    for episode in candidates:
        source = _episode_source_path(episode, manifest_path)
        if source == film_resolved:
            return episode, warnings
    if len(candidates) == 1:
        return candidates[0], warnings
    warnings.append("series_manifest has multiple episodes; falling back to filename episode hint")
    return None, warnings

def resolve_episode_identity(
    *,
    film: Path,
    manifest_path: Path | None,
    settings: EpisodePlanSettings,
    anime_context: AnimeContext | None,
) -> EpisodeIdentity:
    warnings: list[str] = []
    filename_key, filename_number = parse_episode_from_filename(film)
    manifest: SeriesManifest | None = load_series_manifest(manifest_path) if manifest_path else None
    selected: SeriesManifestEpisode | None = None
    if manifest is not None and manifest_path is not None:
        selected, select_warnings = select_manifest_episode(
            manifest,
            manifest_path=manifest_path,
            film=film,
            episode_key=settings.episode_key,
            episode_number=settings.episode_number,
        )
        warnings.extend(select_warnings)
    series_id = manifest.series_id if manifest else normalize_key(film.parent.name or film.stem)
    episode_key = settings.episode_key or (selected.episode_key if selected else None) or filename_key or normalize_key(film.stem)
    episode_number = settings.episode_number
    if episode_number is None:
        episode_number = selected.episode_number if selected else filename_number
    title = (selected.title if selected else None) or (anime_context.episode_title if anime_context else None)
    title = title or film.stem
    arc = (selected.arc if selected else None) or (anime_context.arc if anime_context else None)
    spoiler_limit = (selected.spoiler_limit_episode if selected else None) or numeric_episode(episode_number)
    source_path = _episode_source_path(selected, manifest_path) if selected and selected.source_path else film.expanduser().resolve()
    if source_path != film.expanduser().resolve():
        warnings.append(f"manifest source_path differs from --source-path: {source_path} != {film.expanduser().resolve()}")
    if filename_number is not None and numeric_episode(episode_number) not in {None, filename_number}:
        warnings.append(
            f"filename episode hint {filename_number} differs from manifest/config episode_number {episode_number}"
        )
    return EpisodeIdentity(
        series_id=series_id,
        episode_key=str(episode_key),
        episode_number=episode_number,
        title=title,
        source_path=source_path,
        arc=arc,
        spoiler_limit_episode=spoiler_limit,
        warnings=warnings,
    )

def segment_text(segment: FilmMapSegment) -> str:
    return " ".join(part for part in (segment.en, segment.ko, segment.scene_desc) if part).strip()

def keyword_score(text: str, terms: Iterable[str], scale: float) -> float:
    lowered = text.lower()
    hits = sum(lowered.count(term) for term in terms)
    return round(min(1.0, hits / scale), 4)

def non_story_ratio(profile: VideoProfile | None, duration_s: float) -> float:
    if profile is None or duration_s <= 0:
        return 0.0
    total = 0.0
    for item in profile.non_story_ranges:
        if item.label not in NON_STORY_LABELS:
            continue
        total += max(0.0, min(duration_s, item.end_s) - max(0.0, item.start_s))
    return round(min(1.0, total / duration_s), 4)

def extract_entities(film_map: list[FilmMapSegment], anime_context: AnimeContext | None) -> list[str]:
    entities: list[str] = []
    if anime_context:
        entities.extend(character.name_vi for character in anime_context.characters)
        entities.extend(term.term for term in anime_context.terms)
    for segment in film_map:
        for match in ENTITY_RE.findall(segment.en or ""):
            if match in {"The", "This", "That", "When", "After", "Before", "They", "There", "And"}:
                continue
            entities.append(match)
    return list(dict.fromkeys(item for item in entities if item))

def previous_entity_set(previous: list[EpisodeMemoryEntry]) -> set[str]:
    values: set[str] = set()
    for entry in previous:
        values.update(item.casefold() for item in entry.entity_hooks)
    return values

def compute_score(
    *,
    film_map: list[FilmMapSegment],
    duration_s: float,
    profile: VideoProfile | None,
    previous: list[EpisodeMemoryEntry],
    anime_context: AnimeContext | None,
) -> tuple[float, EpisodeScoreSignals, list[str]]:
    text = " ".join(segment_text(segment) for segment in film_map)
    reveal = keyword_score(text, REVEAL_TERMS, 4.0)
    state_change = keyword_score(text, STATE_CHANGE_TERMS, 5.0)
    fight_action = keyword_score(text, ACTION_TERMS, 5.0)
    continuity = keyword_score(text, CONTINUITY_TERMS, 4.0)
    entities = extract_entities(film_map, anime_context)
    seen_before = previous_entity_set(previous)
    new_entities = [entity for entity in entities if entity.casefold() not in seen_before]
    new_entity = round(min(1.0, len(new_entities) / 4.0), 4)
    story_segments = [segment for segment in film_map if segment.type == "speech" or segment.scene_desc]
    story_density = round(min(1.0, len(story_segments) / max(1.0, duration_s / 25.0)), 4)
    non_story = non_story_ratio(profile, duration_s)
    penalty = round(min(0.35, non_story * 0.7), 4)
    raw = (
        0.25 * reveal
        + 0.20 * state_change
        + 0.20 * fight_action
        + 0.15 * new_entity
        + 0.15 * continuity
        + 0.05 * story_density
        - penalty
    )
    score = round(max(0.0, min(1.0, raw)), 4)
    reasons: list[str] = []
    if reveal:
        reasons.append(f"reveal={reveal:.2f}")
    if state_change:
        reasons.append(f"state_change={state_change:.2f}")
    if fight_action:
        reasons.append(f"fight_action={fight_action:.2f}")
    if new_entity:
        reasons.append(f"new_entity={new_entity:.2f}")
    if continuity:
        reasons.append(f"continuity_dependency={continuity:.2f}")
    if non_story:
        reasons.append(f"non_story_ratio={non_story:.2f}")
    signals = EpisodeScoreSignals(
        reveal=reveal,
        state_change=state_change,
        fight_action=fight_action,
        new_entity=new_entity,
        continuity_dependency=continuity,
        story_density=story_density,
        non_story_ratio=non_story,
        non_story_penalty=penalty,
    )
    return score, signals, reasons

def select_recap_mode(score: float, settings: EpisodePlanSettings) -> ResolvedRecapMode:
    if settings.recap_mode in {"full", "quick", "merge", "skip"}:
        return settings.recap_mode  # type: ignore[return-value]
    if score >= settings.recap_full_threshold:
        return "full"
    if score >= settings.recap_quick_threshold:
        return "quick"
    if score >= settings.recap_merge_threshold:
        return "merge"
    return "skip"

def read_memory_index(index_path: Path, identity: EpisodeIdentity) -> list[EpisodeMemoryEntry]:
    if not index_path.is_file():
        return []
    limit = numeric_episode(identity.spoiler_limit_episode)
    current_number = numeric_episode(identity.episode_number)
    by_key: dict[str, EpisodeMemoryEntry] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = EpisodeMemoryEntry.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.series_id != identity.series_id or entry.episode_key == identity.episode_key:
            continue
        entry_number = numeric_episode(entry.episode_number)
        if current_number is not None and entry_number is not None and entry_number >= current_number:
            continue
        if limit is not None and entry_number is not None and entry_number > limit:
            continue
        by_key[entry.episode_key] = entry
    return sorted(by_key.values(), key=lambda item: (numeric_episode(item.episode_number) or 0, item.created_at))

def compact_summary(film_map: list[FilmMapSegment], story_sections: list[StorySection]) -> str:
    if story_sections:
        summaries = [section.summary for section in story_sections if section.type != "non_story" and section.summary]
        if summaries:
            return " ".join(summaries)[:900].strip()
    texts = [segment_text(segment) for segment in film_map if segment_text(segment)]
    return " ".join(texts)[:900].strip() or "No story summary could be derived from film_map."

def important_timecodes(film_map: list[FilmMapSegment], story_sections: list[StorySection]) -> list[EpisodeTimecodeHook]:
    hooks: list[EpisodeTimecodeHook] = []
    for section in story_sections:
        if section.type in {"reveal", "climax", "inciting_incident", "conflict"} and section.type != "non_story":
            hooks.append(
                EpisodeTimecodeHook(
                    start_s=section.tc_start,
                    end_s=section.tc_end,
                    label=section.type,
                    summary=section.summary[:180],
                )
            )
    for segment in film_map:
        text = segment_text(segment).lower()
        if any(term in text for term in (*REVEAL_TERMS, *STATE_CHANGE_TERMS, *ACTION_TERMS)):
            hooks.append(
                EpisodeTimecodeHook(
                    start_s=segment.tc_start,
                    end_s=segment.tc_end,
                    label="keyword_event",
                    summary=segment_text(segment)[:180],
                )
            )
        if len(hooks) >= 8:
            break
    deduped: list[EpisodeTimecodeHook] = []
    seen: set[tuple[float, float, str]] = set()
    for hook in hooks:
        key = (round(hook.start_s, 2), round(hook.end_s, 2), hook.label)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hook)
    return deduped[:8]

def build_memory_entry(
    *,
    identity: EpisodeIdentity,
    mode: ResolvedRecapMode,
    score: float,
    film_map: list[FilmMapSegment],
    story_sections: list[StorySection],
    anime_context: AnimeContext | None,
) -> EpisodeMemoryEntry:
    entities = extract_entities(film_map, anime_context)[:12]
    arc_hooks = [identity.arc] if identity.arc else []
    arc_hooks.extend(section.type for section in story_sections if section.type in {"reveal", "climax", "ending"})
    return EpisodeMemoryEntry(
        series_id=identity.series_id,
        episode_key=identity.episode_key,
        episode_number=identity.episode_number,
        title=identity.title,
        source_path=str(identity.source_path),
        arc=identity.arc,
        recap_mode=mode,
        importance_score=score,
        summary=compact_summary(film_map, story_sections),
        entity_hooks=entities,
        arc_hooks=arc_hooks,
        important_timecodes=important_timecodes(film_map, story_sections),
        created_at=datetime.now(timezone.utc),
    )

def append_memory_index(index_path: Path, entry: EpisodeMemoryEntry) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")

def review_guidance_for_mode(mode: ResolvedRecapMode) -> list[str]:
    if mode == "quick":
        return [
            "Quick recap mode: keep only continuity-critical state changes, reveals, relationship shifts, and facts needed for the next episode.",
            "Do not pad the script to full-episode coverage; skip repetitive slice-of-life or non-plot scenes.",
        ]
    if mode in {"merge", "skip"}:
        return [
            "No standalone recap render for this episode in Episode V1.",
            "Memory is still recorded so later episodes can mention only continuity-relevant facts.",
        ]
    return ["Full recap mode: cover the episode plot normally while respecting spoiler and non-story guards."]

def build_episode_plan(
    *,
    film: Path,
    film_map_path: Path,
    output_meta_path: Path,
    output_memory_path: Path,
    settings: EpisodePlanSettings,
    series_manifest_path: Path | None,
    series_memory_dir: Path | None,
    video_profile_path: Path | None,
    story_map_path: Path | None,
    anime_context_path: Path | None,
) -> tuple[EpisodeMeta, EpisodeMemory]:
    film_map, duration_s = load_film_map(film_map_path)
    story_sections = load_story_sections(story_map_path, duration_s)
    profile = load_video_profile(video_profile_path)
    anime_context = load_anime_context(anime_context_path) if anime_context_path else None
    identity = resolve_episode_identity(
        film=film,
        manifest_path=series_manifest_path,
        settings=settings,
        anime_context=anime_context,
    )
    memory_dir = series_memory_dir or output_meta_path.parent / "series_memory"
    index_path = memory_dir / "series_memory_index.jsonl"
    previous = read_memory_index(index_path, identity)
    score, signals, reasons = compute_score(
        film_map=film_map,
        duration_s=duration_s,
        profile=profile,
        previous=previous,
        anime_context=anime_context,
    )
    mode = select_recap_mode(score, settings)
    target_ratio_override = settings.quick_target_ratio if mode == "quick" else None
    entry = build_memory_entry(
        identity=identity,
        mode=mode,
        score=score,
        film_map=film_map,
        story_sections=story_sections,
        anime_context=anime_context,
    )
    memory = EpisodeMemory(
        anime_context=anime_context,
        current=entry,
        previous=previous,
        spoiler_limit_episode=identity.spoiler_limit_episode,
        review_guidance=review_guidance_for_mode(mode),
        warnings=identity.warnings,
        created_at=datetime.now(timezone.utc),
    )
    meta = EpisodeMeta(
        series_id=identity.series_id,
        episode_key=identity.episode_key,
        episode_number=identity.episode_number,
        title=identity.title,
        source_path=str(identity.source_path),
        arc=identity.arc,
        spoiler_limit_episode=identity.spoiler_limit_episode,
        requested_recap_mode=settings.recap_mode,  # type: ignore[arg-type]
        recap_mode=mode,
        importance_score=score,
        score_signals=signals,
        score_reasons=reasons,
        short_circuit=mode in {"merge", "skip"},
        target_ratio_override=target_ratio_override,
        quick_target_ratio=settings.quick_target_ratio,
        thresholds={
            "full": settings.recap_full_threshold,
            "quick": settings.recap_quick_threshold,
            "merge": settings.recap_merge_threshold,
            "quick_min_coverage": settings.quick_min_coverage,
        },
        previous_memory_count=len(previous),
        memory_index_path=str(index_path),
        warnings=identity.warnings,
        created_at=datetime.now(timezone.utc),
        film_map_hash=file_hash(film_map_path),
        story_map_hash=file_hash(story_map_path),
        video_profile_hash=file_hash(video_profile_path),
        anime_context_hash=file_hash(anime_context_path),
        series_manifest_hash=file_hash(series_manifest_path),
        source_hash=media_identity_hash(film),
        config_hash=episode_planner_config_hash(settings),
        cache_version=EPISODE_PLANNER_CACHE_VERSION,
    )
    output_meta_path.parent.mkdir(parents=True, exist_ok=True)
    output_memory_path.parent.mkdir(parents=True, exist_ok=True)
    output_meta_path.write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    output_memory_path.write_text(memory.model_dump_json(indent=2) + "\n", encoding="utf-8")
    append_memory_index(index_path, entry)
    return meta, memory
