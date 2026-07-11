from __future__ import annotations

from dataclasses import dataclass

from common.schema import FilmMapSegment, ReviewBeat, Shot
from match.candidates import intersect_duration, is_dark_fallback_candidate
from match.semantic import segment_text


@dataclass(frozen=True)
class ContentAnchorPlan:
    intervals: list[tuple[float, float]]
    candidate_ids: set[int]
    dark_candidate_ids: set[int]
    segment_ids: list[int]
    capacity_s: float
    threshold: float


@dataclass(frozen=True)
class _AnchorSegment:
    segment: FilmMapSegment
    score: float
    anchor_end: float


def plan_content_anchors(
    *,
    beat: ReviewBeat,
    required_duration_s: float,
    shots: list[Shot],
    film_map: list[FilmMapSegment],
    segment_scores: dict[tuple[int, int], float],
    max_clip: float,
    min_visual_clip: float,
    allow_dark_fallback: bool,
    min_span_ratio: float = 4.0,
    min_score: float = 0.35,
    score_margin: float = 0.18,
    cluster_gap_s: float = 10.0,
    padding_s: float = 2.0,
    max_segment_span_s: float = 8.0,
) -> ContentAnchorPlan | None:
    source_span = beat.src_tc_end - beat.src_tc_start
    if required_duration_s <= 0 or source_span < required_duration_s * min_span_ratio:
        return None
    scored: list[_AnchorSegment] = []
    seen_texts: set[str] = set()
    for segment in film_map:
        if not (beat.from_seg_id <= segment.id <= beat.to_seg_id):
            continue
        text = " ".join(segment_text(segment).split())
        key = text.casefold()
        if not text or key in seen_texts:
            continue
        seen_texts.add(key)
        score = segment_scores.get((beat.beat_id, segment.id), 0.0)
        scored.append(
            _AnchorSegment(
                segment=segment,
                score=score,
                anchor_end=min(segment.tc_end, segment.tc_start + max_segment_span_s),
            )
        )
    if not scored:
        return None
    best_score = max(item.score for item in scored)
    threshold = max(min_score, best_score - score_margin)
    relevant = [item for item in scored if item.score + 1e-6 >= threshold]
    if len(relevant) < 2:
        return None
    clusters = _cluster_segments(relevant, cluster_gap_s=cluster_gap_s)
    ranked_clusters = sorted(
        clusters,
        key=lambda cluster: (
            max(item.score for item in cluster),
            sum(item.score for item in cluster) / len(cluster),
            -cluster[0].segment.tc_start,
        ),
        reverse=True,
    )
    selected: list[list[_AnchorSegment]] = []
    for cluster in ranked_clusters:
        selected.append(cluster)
        intervals = _merge_intervals(
            [
                (
                    max(beat.src_tc_start, group[0].segment.tc_start - padding_s),
                    min(beat.src_tc_end, max(item.anchor_end for item in group) + padding_s),
                )
                for group in selected
            ]
        )
        candidates, dark_ids = _anchor_candidates(
            shots,
            intervals,
            min_visual_clip=min_visual_clip,
            allow_dark_fallback=allow_dark_fallback,
        )
        capacity = _interval_capacity(
            candidates,
            intervals,
            max_clip=max_clip,
            min_visual_clip=min_visual_clip,
        )
        if capacity + 1e-6 >= required_duration_s:
            segment_ids = sorted(item.segment.id for group in selected for item in group)
            return ContentAnchorPlan(
                intervals=intervals,
                candidate_ids={shot.index for shot in candidates},
                dark_candidate_ids=dark_ids,
                segment_ids=segment_ids,
                capacity_s=capacity,
                threshold=threshold,
            )
    return None


def _cluster_segments(
    segments: list[_AnchorSegment],
    *,
    cluster_gap_s: float,
) -> list[list[_AnchorSegment]]:
    clusters: list[list[_AnchorSegment]] = []
    for item in sorted(segments, key=lambda value: value.segment.tc_start):
        if not clusters or item.segment.tc_start - max(value.anchor_end for value in clusters[-1]) > cluster_gap_s:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _anchor_candidates(
    shots: list[Shot],
    intervals: list[tuple[float, float]],
    *,
    min_visual_clip: float,
    allow_dark_fallback: bool,
) -> tuple[list[Shot], set[int]]:
    candidates: list[Shot] = []
    dark_ids: set[int] = set()
    for shot in shots:
        intersection = sum(intersect_duration(shot, start, end) for start, end in intervals)
        if intersection <= 1e-6 or intersection + 1e-6 < min_visual_clip:
            continue
        if shot.is_usable:
            candidates.append(shot)
        elif allow_dark_fallback and is_dark_fallback_candidate(shot, min_visual_clip=min_visual_clip):
            candidates.append(shot)
            dark_ids.add(shot.index)
    return candidates, dark_ids


def _interval_capacity(
    shots: list[Shot],
    intervals: list[tuple[float, float]],
    *,
    max_clip: float,
    min_visual_clip: float,
) -> float:
    capacity = 0.0
    for shot in shots:
        duration = sum(intersect_duration(shot, start, end) for start, end in intervals)
        if duration <= 1e-6 or duration + 1e-6 < min_visual_clip:
            continue
        capacity += min(max_clip, duration)
    return capacity


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if output and start <= output[-1][1] + 1e-6:
            output[-1] = (output[-1][0], max(output[-1][1], end))
        else:
            output.append((start, end))
    return output
