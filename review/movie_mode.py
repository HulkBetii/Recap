from __future__ import annotations

import re
from dataclasses import dataclass

from common.schema import FilmMapSegment, ReviewBeat, VideoProfile
from review.models import NarrationBeat, OutlineBeat, OutlineResult

MOVIE_AUTO_MIN_RATIO = 0.18
MOVIE_AUTO_MAX_RATIO = 0.26
EPISODE_AUTO_RATIO = 0.33
OPENING_WINDOW_S = 120.0
STORY_START_TOLERANCE_S = 10.0
EPISODE_CONTENT_TYPES = {"episode", "anime_series"}
MOVIE_CONTENT_TYPES = {"movie", "anime_movie"}
SUPPORTED_CONTENT_TYPES = EPISODE_CONTENT_TYPES | MOVIE_CONTENT_TYPES

def content_family(content_type: str) -> str:
    if content_type in MOVIE_CONTENT_TYPES:
        return "movie"
    if content_type in EPISODE_CONTENT_TYPES:
        return "episode"
    raise ValueError("content_type must be episode, movie, anime_series, or anime_movie")

def is_movie_content(content_type: str) -> bool:
    return content_family(content_type) == "movie"


def story_start_from_profile(profile: VideoProfile | None) -> float:
    if profile is None:
        return 0.0
    starts = [
        item.end_s
        for item in profile.non_story_ranges
        if item.start_s <= 5.0
        and item.label in {
            "intro_opening",
            "opening",
            "intro",
            "opening_theme",
            "recap_previous_episode",
            "title_card",
            "studio_logo",
            "eyecatch",
            "sponsor_card",
        }
    ]
    return round(max(starts), 3) if starts else 0.0

@dataclass(frozen=True)
class AutoDurationResult:
    target_ratio: float
    complexity_score: float
    target_ratio_mode: str

@dataclass(frozen=True)
class OpeningCoherenceResult:
    passed: bool
    issues: list[str]
    warnings: list[str]


def resolve_content_defaults(content_type: str, hook_mode: str | None, opening_coherence_qa: bool | None) -> tuple[str, bool]:
    family = content_family(content_type)
    resolved_hook = hook_mode or ("setup" if family == "movie" else "cold_open")
    if resolved_hook not in {"cold_open", "setup", "off"}:
        raise ValueError("hook_mode must be cold_open, setup, or off")
    resolved_opening_qa = (family == "movie") if opening_coherence_qa is None else opening_coherence_qa
    return resolved_hook, resolved_opening_qa


def resolve_target_ratio(target_ratio: str | float, *, content_type: str, film_map: list[FilmMapSegment], duration_s: float) -> AutoDurationResult:
    if isinstance(target_ratio, (int, float)):
        return AutoDurationResult(float(target_ratio), 0.0, "fixed")
    raw = str(target_ratio).strip().lower()
    if raw != "auto":
        return AutoDurationResult(float(raw), 0.0, "fixed")
    score = compute_complexity_score(film_map, duration_s)
    if is_movie_content(content_type):
        ratio = MOVIE_AUTO_MIN_RATIO + (MOVIE_AUTO_MAX_RATIO - MOVIE_AUTO_MIN_RATIO) * score
    else:
        ratio = EPISODE_AUTO_RATIO
    return AutoDurationResult(round(ratio, 4), score, "auto")


def compute_complexity_score(film_map: list[FilmMapSegment], duration_s: float) -> float:
    if not film_map or duration_s <= 0:
        return 0.0
    speech = [seg for seg in film_map if seg.type == "speech"]
    visuals = [seg for seg in film_map if seg.type == "visual"]
    text = " ".join((seg.en or seg.ko or seg.scene_desc or "") for seg in film_map).lower()
    name_like = len(set(re.findall(r"\b[A-Z][a-z]{2,}\b", " ".join(seg.en or "" for seg in speech))))
    speech_density = min(1.0, len(speech) / max(1.0, duration_s / 25.0))
    visual_density = min(1.0, len(visuals) / max(1.0, duration_s / 90.0))
    entity_density = min(1.0, name_like / 12.0)
    twist_terms = sum(text.count(term) for term in ("secret", "ghost", "demon", "death", "curse", "murder", "possess", "ritual", "truth"))
    twist_density = min(1.0, twist_terms / 20.0)
    score = 0.40 * speech_density + 0.20 * visual_density + 0.25 * entity_density + 0.15 * twist_density
    return round(max(0.0, min(1.0, score)), 4)


def apply_hook_mode(outline_result: OutlineResult, *, content_type: str, hook_mode: str, film_map: list[FilmMapSegment], story_start_s: float = 0.0) -> OutlineResult:
    if not outline_result.outline:
        return outline_result
    if not is_movie_content(content_type) or hook_mode != "setup":
        return outline_result
    opening_end_s = max(OPENING_WINDOW_S, story_start_s + OPENING_WINDOW_S)
    opening_ids = {seg.id for seg in film_map if seg.tc_end > story_start_s and seg.tc_start <= opening_end_s}
    outline = [beat.model_copy(update={"is_hook": False}) for beat in outline_result.outline]
    setup_index = 0
    for index, beat in enumerate(outline):
        if beat.from_seg_id in opening_ids or beat.to_seg_id in opening_ids:
            setup_index = index
            break
    setup = outline.pop(setup_index).model_copy(update={"is_hook": True})
    return OutlineResult(glossary=outline_result.glossary, outline=[setup, *outline], hook=[setup.from_seg_id])


def check_opening_coherence(beats: list[ReviewBeat], *, content_type: str, hook_mode: str, opening_window_s: float = OPENING_WINDOW_S, story_start_s: float = 0.0) -> OpeningCoherenceResult:
    if not is_movie_content(content_type):
        return OpeningCoherenceResult(True, [], [])
    if not beats:
        return OpeningCoherenceResult(False, ["missing opening beat"], ["opening coherence failed: missing opening beat"])
    beat = beats[0]
    text = beat.narration.strip().lower()
    issues: list[str] = []
    if hook_mode == "setup" and beat.src_tc_end <= story_start_s + 1e-6:
        issues.append(f"setup hook ends before story_start_s {story_start_s:.1f}s")
    elif hook_mode == "setup" and beat.src_tc_start + STORY_START_TOLERANCE_S < story_start_s:
        issues.append(f"setup hook starts too far before story_start_s {story_start_s:.1f}s at {beat.src_tc_start:.1f}s")
    if hook_mode == "setup" and beat.src_tc_start > story_start_s + opening_window_s:
        issues.append(f"setup hook starts too late at {beat.src_tc_start:.1f}s")
    if len(text) < 80:
        issues.append("opening narration is too short to establish premise")
    has_subject_marker = any(token in text for token in ("l?", "?ang", "b?", "c?", "anh", "ng??i", "nh?n v?t", "c?u chuy?n", "b? phim"))
    has_named_entity = len(re.findall(r"\b[A-Z?-?][\w?-?-]{2,}\b", beat.narration)) >= 2
    if not has_subject_marker and not has_named_entity:
        issues.append("opening narration lacks clear subject/premise markers")
    hype_terms = ("s?c", "kinh ho?ng", "kh?ng khi?p", "?i?n r?", "kh?ng ai ng?", "c? twist")
    if any(term in text for term in hype_terms) and not any(term in text for term in ("v?", "sau khi", "tr??c ??", "b?t ??u", "c?u chuy?n")):
        issues.append("opening sounds like hype without context")
    warnings = [f"opening coherence: {issue}" for issue in issues]
    return OpeningCoherenceResult(not issues, issues, warnings)


def opening_rewrite_issue(report: OpeningCoherenceResult) -> str:
    return "Opening coherence: rewrite this as a clear movie setup beat. Establish who is involved, where the story starts, what unusual problem appears, and why it matters. Avoid later-story twist/cold-open hype. Issues: " + "; ".join(report.issues)


def replace_narration(narration: list[NarrationBeat], revised: NarrationBeat) -> list[NarrationBeat]:
    by_id = {item.beat_id: item for item in narration}
    by_id[revised.beat_id] = revised
    return [by_id[index] for index in sorted(by_id)]
