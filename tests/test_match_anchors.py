from __future__ import annotations

from common.schema import FilmMapSegment, ReviewBeat, Shot
from match.anchors import plan_content_anchors


def segment(index: int, start: float, end: float, text: str) -> FilmMapSegment:
    return FilmMapSegment(id=index, type="speech", tc_start=start, tc_end=end, ko=text, en=text)


def shot(index: int, start: float, end: float, *, usable: bool = True) -> Shot:
    return Shot(
        src="film.mp4",
        index=index,
        tc_start=start,
        tc_end=end,
        duration=end - start,
        thumb="x.jpg",
        motion_score=0.5,
        face_count=0,
        face_area=0,
        brightness=0.5 if usable else 0.05,
        is_usable=usable,
        unusable_reasons=[] if usable else ["too_dark"],
    )


def beat(*, end: float = 100.0) -> ReviewBeat:
    return ReviewBeat(
        beat_id=7,
        narration="nhom lap ke hoach va tin tuong nhau",
        from_seg_id=0,
        to_seg_id=3,
        src_tc_start=0,
        src_tc_end=end,
        is_hook=False,
    )


def test_content_anchor_keeps_relevant_cluster_and_drops_low_score_garbage() -> None:
    film_map = [
        segment(0, 5, 10, "lap ke hoach"),
        segment(1, 12, 18, "tin tuong dong doi"),
        segment(2, 45, 50, "hay dang ky kenh"),
        segment(3, 70, 75, "hay dang ky kenh"),
    ]
    shots = [shot(0, 4, 9), shot(1, 9, 14), shot(2, 14, 20), shot(3, 44, 52)]
    scores = {(7, 0): 0.70, (7, 1): 0.62, (7, 2): 0.20, (7, 3): 0.20}

    plan = plan_content_anchors(
        beat=beat(),
        required_duration_s=10,
        shots=shots,
        film_map=film_map,
        segment_scores=scores,
        max_clip=5,
        min_visual_clip=0.6,
        allow_dark_fallback=True,
    )

    assert plan is not None
    assert plan.segment_ids == [0, 1]
    assert plan.candidate_ids == {0, 1, 2}
    assert 3 not in plan.candidate_ids


def test_content_anchor_includes_dark_shot_when_it_is_inside_relevant_interval() -> None:
    film_map = [
        segment(0, 5, 10, "nhan phan thuong"),
        segment(1, 12, 18, "song lai sau tran chien"),
        segment(2, 45, 50, "khong lien quan"),
        segment(3, 70, 75, "khong lien quan khac"),
    ]
    shots = [shot(0, 4, 9), shot(1, 9, 15, usable=False), shot(2, 15, 20)]
    scores = {(7, 0): 0.68, (7, 1): 0.64, (7, 2): 0.10, (7, 3): 0.12}

    plan = plan_content_anchors(
        beat=beat(),
        required_duration_s=12,
        shots=shots,
        film_map=film_map,
        segment_scores=scores,
        max_clip=5,
        min_visual_clip=0.6,
        allow_dark_fallback=True,
    )

    assert plan is not None
    assert 1 in plan.candidate_ids
    assert plan.dark_candidate_ids == {1}


def test_content_anchor_is_disabled_for_compact_source_span() -> None:
    film_map = [segment(0, 0, 4, "lap ke hoach"), segment(1, 5, 9, "tin tuong"), segment(2, 10, 14, "x"), segment(3, 15, 19, "y")]
    scores = {(7, index): 0.7 for index in range(4)}

    plan = plan_content_anchors(
        beat=beat(end=20),
        required_duration_s=8,
        shots=[shot(0, 0, 10), shot(1, 10, 20)],
        film_map=film_map,
        segment_scores=scores,
        max_clip=5,
        min_visual_clip=0.6,
        allow_dark_fallback=True,
    )

    assert plan is None
