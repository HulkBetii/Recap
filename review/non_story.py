from __future__ import annotations

from dataclasses import dataclass

from common.schema import FilmMapSegment, ReviewBeat

NON_STORY_NARRATION_TERMS = (
    "credit",
    "credits",
    "thông tin sản xuất",
    "phần còn lại",
    "màn hình chuyển sang nền đen",
    "nền đen",
    "chữ trắng",
    "hết phim",
    "cuối phim chỉ còn",
)
NON_STORY_CONTEXT_TERMS = (
    "credit",
    "credits",
    "title card",
    "studio logo",
    "opening credits",
    "end credits",
    "black screen",
    "white text",
    "nền đen",
    "chữ trắng",
)
PLOT_TERMS = (
    "giết", "chạy", "trốn", "đánh", "cứu", "khóc", "sợ", "máu", "quỷ", "ác linh",
    "nghi lễ", "tấn công", "chết", "sống", "cô", "anh", "chị", "em", "bao", "eunseo", "jungwon",
)

@dataclass(frozen=True)
class NonStoryBeatDecision:
    beat_id: int
    action: str
    reason: str

@dataclass(frozen=True)
class NonStoryBeatReport:
    dropped_beat_ids: list[int]
    warnings: list[str]
    decisions: list[NonStoryBeatDecision]


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _span_text(beat: ReviewBeat, film_map: list[FilmMapSegment]) -> str:
    by_id = {segment.id: segment for segment in film_map}
    texts: list[str] = []
    for segment_id in range(beat.from_seg_id, beat.to_seg_id + 1):
        segment = by_id.get(segment_id)
        if segment is None:
            continue
        texts.append(segment.en or segment.scene_desc or segment.ko or "")
    return _norm(" ".join(texts))


def is_non_story_beat(beat: ReviewBeat, film_map: list[FilmMapSegment], duration_s: float, tail_s: float) -> tuple[bool, str]:
    narration = _norm(beat.narration)
    context = _span_text(beat, film_map)
    near_tail = beat.src_tc_end >= max(0.0, duration_s - tail_s)
    narration_hit = next((term for term in NON_STORY_NARRATION_TERMS if term in narration), None)
    context_hits = [term for term in NON_STORY_CONTEXT_TERMS if term in context]
    plot_hits = [term for term in PLOT_TERMS if term in narration or term in context]
    if narration_hit and (near_tail or context_hits):
        return True, f"narration contains non-story term '{narration_hit}' near tail/credit context"
    if near_tail and len(context_hits) >= 2 and not plot_hits:
        return True, "tail source context looks like credits/outro without plot action"
    return False, ""


def drop_non_story_beats(
    beats: list[ReviewBeat],
    film_map: list[FilmMapSegment],
    *,
    duration_s: float,
    tail_s: float,
) -> tuple[list[ReviewBeat], NonStoryBeatReport]:
    kept: list[ReviewBeat] = []
    dropped: list[int] = []
    warnings: list[str] = []
    decisions: list[NonStoryBeatDecision] = []
    for beat in beats:
        should_drop, reason = is_non_story_beat(beat, film_map, duration_s, tail_s)
        if should_drop and beat.is_hook:
            warnings.append(f"hook beat {beat.beat_id} looks non-story but was kept: {reason}")
            decisions.append(NonStoryBeatDecision(beat.beat_id, "kept_hook", reason))
            kept.append(beat)
        elif should_drop:
            dropped.append(beat.beat_id)
            decisions.append(NonStoryBeatDecision(beat.beat_id, "dropped", reason))
        else:
            kept.append(beat)
    reindexed = [beat.model_copy(update={"beat_id": index}) for index, beat in enumerate(kept)]
    if dropped:
        warnings.append(f"dropped {len(dropped)} non-story credit/outro beat(s): {dropped}")
    return reindexed, NonStoryBeatReport(dropped_beat_ids=dropped, warnings=warnings, decisions=decisions)
