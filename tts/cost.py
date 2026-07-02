from __future__ import annotations

from common.schema import ReviewBeat

DEFAULT_COST_PER_1K_CHARS = 0.0


def total_chars(beats: list[ReviewBeat]) -> int:
    return sum(len(beat.narration) for beat in beats)


def estimate_cost(chars: int, cost_per_1k_chars: float = DEFAULT_COST_PER_1K_CHARS) -> float:
    return round((chars / 1000.0) * cost_per_1k_chars, 6)


def real_ratio(total_duration_s: float, film_duration_s: float | None) -> float | None:
    if not film_duration_s or film_duration_s <= 0:
        return None
    return total_duration_s / film_duration_s
