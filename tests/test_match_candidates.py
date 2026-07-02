from __future__ import annotations

from common.schema import Shot
from match.candidates import candidates_for_window, widen_until_enough


def shot(index, start, end, usable=True):  # type: ignore[no-untyped-def]
    return Shot(src="film.mp4", index=index, tc_start=start, tc_end=end, duration=end-start, thumb="x.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=usable)


def test_candidates_intersect_window_and_filter_usable() -> None:
    shots = [shot(0, 0, 2), shot(1, 3, 4, usable=False), shot(2, 5, 6)]
    assert [s.index for s in candidates_for_window(shots, 1, 5)] == [0]


def test_widen_until_enough_expands_window() -> None:
    shots = [shot(0, 0, 2), shot(1, 10, 14)]
    start, end, candidates, count = widen_until_enough(shots=shots, start=10, end=11, needed_duration=3, margin=2, max_widen=2)
    assert count > 0
    assert end > 11
    assert candidates
