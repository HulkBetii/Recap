from __future__ import annotations

from review.budget import allocate_char_targets, compute_budget
from review.models import OutlineBeat


def test_compute_budget_uses_ratio_and_tts_cps() -> None:
    target_s, chars = compute_budget(300.0, 0.33, 15.0)

    assert target_s == 99.0
    assert chars == 1485


def test_allocate_char_targets_gives_hook_budget_and_minimums() -> None:
    outline = [
        OutlineBeat(from_seg_id=5, to_seg_id=5, summary="hook", is_hook=True),
        OutlineBeat(from_seg_id=0, to_seg_id=4, summary="a"),
        OutlineBeat(from_seg_id=6, to_seg_id=10, summary="b"),
    ]

    targets = allocate_char_targets(outline, 1000)

    assert len(targets) == 3
    assert targets[0] >= 120
    assert targets[1] == targets[2]
    assert sum(targets) >= 1000 - 20
