from __future__ import annotations

import pytest

from common.episodes import numeric_episode


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (12, 12),
        ("12", 12),
        ("s01e12", 12),
        ("S1E12", 12),
        ("1x12", 12),
        ("e12", 12),
        ("ep12", 12),
        ("Episode 12", 12),
        ("Season 1 Episode 12", 12),
        ("part-12", 12),
        ("2024 special 12", None),
        ("special", None),
    ],
)
def test_numeric_episode(value: int | str | None, expected: int | None) -> None:
    assert numeric_episode(value) == expected
