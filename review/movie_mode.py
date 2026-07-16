from __future__ import annotations

import re
from dataclasses import dataclass

from common.schema import FilmMapSegment, ReviewBeat, StorySection, VideoProfile
from review.models import NarrationBeat, OutlineBeat, OutlineResult

MOVIE_AUTO_POLICY = "balanced-v1"
MOVIE_AUTO_HARD_MAX_RATIO = 0.40
MOVIE_AUTO_SOFT_CAP_S = 35.0 * 60.0
MOVIE_AUTO_HARD_CAP_S = 45.0 * 60.0
MOVIE_AUTO_LONG_SCORE_THRESHOLD = 0.80
LONG_MOVIE_QA_TARGET_S = 35.0 * 60.0
LONG_MOVIE_QA_CHAR_BUDGET = 30_000
LONG_MOVIE_QA_MAX_ITERATIONS = 1
EPISODE_AUTO_RATIO = 0.33
OPENING_WINDOW_S = 120.0
STORY_START_TOLERANCE_S = 10.0


def story_start_from_profile(profile: VideoProfile | None) -> float:
    if profile is None:
        return 0.0
    starts = [item.end_s for item in profile.non_story_ranges if item.start_s <= 5.0 and item.label in {"intro_opening", "opening", "intro", "title_card", "studio_logo"}]
    return round(max(starts), 3) if starts else 0.0

@dataclass(frozen=True)
class AutoDurationResult:
    target_ratio: float
    complexity_score: float
    target_ratio_mode: str
    story_duration_s: float
    target_duration_base_s: float
    auto_duration_policy: str | None = None
    auto_duration_raw_ratio: float | None = None
    auto_duration_raw_target_s: float | None = None
    auto_duration_cap_applied: str | None = None
    warnings: list[str] | None = None

@dataclass(frozen=True)
class AutoDurationPolicy:
    max_ratio: float = MOVIE_AUTO_HARD_MAX_RATIO
    soft_cap_s: float = MOVIE_AUTO_SOFT_CAP_S
    hard_cap_s: float = MOVIE_AUTO_HARD_CAP_S
    long_score_threshold: float = MOVIE_AUTO_LONG_SCORE_THRESHOLD

@dataclass(frozen=True)
class QaIterationPolicyResult:
    requested_max_qa_iterations: int
    effective_max_qa_iterations: int
    policy: str
    warning: str | None = None

@dataclass(frozen=True)
class OpeningCoherenceResult:
    passed: bool
    issues: list[str]
    warnings: list[str]


def resolve_content_defaults(content_type: str, hook_mode: str | None, opening_coherence_qa: bool | None) -> tuple[str, bool]:
    if content_type not in {"episode", "movie"}:
        raise ValueError("content_type must be episode or movie")
    resolved_hook = hook_mode or ("setup" if content_type == "movie" else "cold_open")
    if resolved_hook not in {"cold_open", "setup", "off"}:
        raise ValueError("hook_mode must be cold_open, setup, or off")
    resolved_opening_qa = (content_type == "movie") if opening_coherence_qa is None else opening_coherence_qa
    return resolved_hook, resolved_opening_qa


def resolve_target_ratio(
    target_ratio: str | float,
    *,
    content_type: str,
    film_map: list[FilmMapSegment],
    duration_s: float,
    video_profile: VideoProfile | None = None,
    story_sections: list[StorySection] | None = None,
    auto_policy: AutoDurationPolicy | None = None,
) -> AutoDurationResult:
    story_duration_s, story_warnings = compute_story_duration(duration_s, video_profile)
    if isinstance(target_ratio, (int, float)):
        return AutoDurationResult(float(target_ratio), 0.0, "fixed", story_duration_s, duration_s, warnings=story_warnings)
    raw = str(target_ratio).strip().lower()
    if raw != "auto":
        return AutoDurationResult(float(raw), 0.0, "fixed", story_duration_s, duration_s, warnings=story_warnings)
    if content_type != "movie":
        return AutoDurationResult(
            EPISODE_AUTO_RATIO,
            0.0,
            "auto",
            story_duration_s,
            duration_s,
            auto_duration_policy="episode-legacy",
            auto_duration_raw_ratio=EPISODE_AUTO_RATIO,
            auto_duration_raw_target_s=round(duration_s * EPISODE_AUTO_RATIO, 3),
            auto_duration_cap_applied="none",
            warnings=story_warnings,
        )
    policy = auto_policy or AutoDurationPolicy()
    score = compute_complexity_score(film_map, story_duration_s, story_sections=story_sections)
    raw_ratio = movie_ratio_from_complexity(score)
    effective_max_ratio = min(policy.max_ratio, MOVIE_AUTO_HARD_MAX_RATIO)
    capped_ratio = min(raw_ratio, effective_max_ratio)
    raw_target_s = story_duration_s * raw_ratio
    target_s = story_duration_s * capped_ratio
    caps: list[str] = []
    if capped_ratio < raw_ratio:
        caps.append("max_ratio")
    if target_s > policy.soft_cap_s and score < policy.long_score_threshold:
        target_s = policy.soft_cap_s
        caps.append("soft_cap_s")
    hard_cap_s = min(policy.hard_cap_s, story_duration_s * MOVIE_AUTO_HARD_MAX_RATIO)
    if target_s > hard_cap_s:
        target_s = hard_cap_s
        caps.append("hard_cap_s")
    final_ratio = target_s / story_duration_s if story_duration_s > 0 else 0.0
    return AutoDurationResult(
        round(final_ratio, 4),
        score,
        "auto",
        story_duration_s,
        story_duration_s,
        auto_duration_policy=MOVIE_AUTO_POLICY,
        auto_duration_raw_ratio=round(raw_ratio, 4),
        auto_duration_raw_target_s=round(raw_target_s, 3),
        auto_duration_cap_applied="+".join(caps) if caps else "none",
        warnings=story_warnings,
    )


def compute_story_duration(duration_s: float, profile: VideoProfile | None) -> tuple[float, list[str]]:
    warnings: list[str] = []
    if duration_s <= 0:
        return duration_s, warnings
    if profile is None:
        return duration_s, ["video profile missing; using full duration as story duration"]
    ranges: list[tuple[float, float]] = []
    for item in profile.non_story_ranges:
        start = max(0.0, min(duration_s, item.start_s))
        end = max(0.0, min(duration_s, item.end_s))
        if end > start:
            ranges.append((start, end))
    if not ranges:
        return duration_s, warnings
    ranges.sort()
    merged: list[list[float]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    non_story_s = sum(end - start for start, end in merged)
    return round(max(1.0, duration_s - non_story_s), 3), warnings


def movie_ratio_from_complexity(score: float) -> float:
    score = max(0.0, min(1.0, score))
    if score < 0.25:
        return 0.18 + 0.04 * (score / 0.25)
    if score < 0.60:
        return 0.22 + 0.08 * ((score - 0.25) / 0.35)
    if score < MOVIE_AUTO_LONG_SCORE_THRESHOLD:
        return 0.30 + 0.05 * ((score - 0.60) / (MOVIE_AUTO_LONG_SCORE_THRESHOLD - 0.60))
    return 0.35 + 0.03 * ((score - MOVIE_AUTO_LONG_SCORE_THRESHOLD) / (1.0 - MOVIE_AUTO_LONG_SCORE_THRESHOLD))


def resolve_qa_iterations(
    requested_max_qa_iterations: int,
    *,
    content_type: str,
    target_video_s: float,
    char_budget: int,
) -> QaIterationPolicyResult:
    if requested_max_qa_iterations < 0:
        raise ValueError("requested_max_qa_iterations must be >= 0")
    if content_type == "movie" and requested_max_qa_iterations > LONG_MOVIE_QA_MAX_ITERATIONS:
        if target_video_s >= LONG_MOVIE_QA_TARGET_S or char_budget > LONG_MOVIE_QA_CHAR_BUDGET:
            warning = (
                "long movie QA clamp: max_qa_iterations "
                f"{requested_max_qa_iterations} -> {LONG_MOVIE_QA_MAX_ITERATIONS} "
                f"for target_video_s={target_video_s:.1f}, char_budget={char_budget}"
            )
            return QaIterationPolicyResult(
                requested_max_qa_iterations,
                LONG_MOVIE_QA_MAX_ITERATIONS,
                "long-movie-v1",
                warning,
            )
    return QaIterationPolicyResult(requested_max_qa_iterations, requested_max_qa_iterations, "configured")


def compute_complexity_score(film_map: list[FilmMapSegment], duration_s: float, *, story_sections: list[StorySection] | None = None) -> float:
    if not film_map or duration_s <= 0:
        return 0.0
    speech = [seg for seg in film_map if seg.type == "speech"]
    original_text = " ".join(seg.en or seg.ko or seg.scene_desc or "" for seg in film_map)
    text = original_text.lower()
    speech_s = sum(max(0.0, seg.tc_end - seg.tc_start) for seg in speech)
    speech_coverage = min(1.0, speech_s / max(1.0, duration_s))
    chars_density = min(1.0, (len(original_text) / max(1.0, duration_s / 60.0)) / 850.0)
    segment_density = min(1.0, len(speech) / max(1.0, duration_s / 30.0))
    name_like = len(set(re.findall(r"\b[A-ZÀ-ỸĐ][\wÀ-ỹĐđ'’-]{2,}\b", original_text)))
    entity_density = min(1.0, name_like / 18.0)
    keyword_terms = (
        "survival", "death", "kill", "game", "secret", "truth", "twist", "monster", "demon", "curse",
        "murder", "ritual", "system", "rule", "quest", "mission", "apocalypse", "regression", "loop",
        "sinh tồn", "tận thế", "trò chơi", "tử thần", "kịch bản", "bí mật", "sự thật", "quái", "giết",
        "chết", "sống sót", "hồi quy", "vòng lặp", "hệ thống", "luật", "nhiệm vụ", "chòm sao", "năng lực",
        "phản bội", "điều tra", "nghi lễ", "lời nguyền", "thời gian", "xuyên không",
    )
    keyword_hits = sum(text.count(term) for term in keyword_terms)
    keyword_density = min(1.0, keyword_hits / max(8.0, duration_s / 180.0))
    story_richness = 0.0
    if story_sections:
        section_score = min(1.0, len(story_sections) / 7.0)
        event_count = sum(len(section.events) for section in story_sections)
        character_count = len({name for section in story_sections for name in section.characters})
        event_score = min(1.0, event_count / 18.0)
        character_score = min(1.0, character_count / 10.0)
        story_richness = 0.45 * section_score + 0.35 * event_score + 0.20 * character_score
    score = (
        0.20 * speech_coverage
        + 0.20 * chars_density
        + 0.15 * segment_density
        + 0.15 * entity_density
        + 0.20 * keyword_density
        + 0.10 * story_richness
    )
    return round(max(0.0, min(1.0, score)), 4)


def apply_hook_mode(outline_result: OutlineResult, *, content_type: str, hook_mode: str, film_map: list[FilmMapSegment], story_start_s: float = 0.0) -> OutlineResult:
    if not outline_result.outline:
        return outline_result
    if content_type != "movie" or hook_mode != "setup":
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
    if content_type != "movie":
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
