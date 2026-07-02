from __future__ import annotations

from common.schema import ReviewBeat
from review.models import OutlineBeat

HOOK_TARGET_CHARS = 220
MIN_BEAT_CHARS = 120


def compute_budget(duration_s: float, target_ratio: float, tts_cps: float) -> tuple[float, int]:
    target_video_s = duration_s * target_ratio
    char_budget = max(1, int(round(target_video_s * tts_cps)))
    return target_video_s, char_budget


def allocate_char_targets(outline: list[OutlineBeat], char_budget: int) -> list[int]:
    if not outline:
        return []
    hook_count = sum(1 for beat in outline if beat.is_hook)
    hook_budget_total = min(char_budget // 4, hook_count * HOOK_TARGET_CHARS) if hook_count else 0
    normal_budget = max(1, char_budget - hook_budget_total)
    normal_beats = [beat for beat in outline if not beat.is_hook]
    total_weight = sum(max(1, beat.to_seg_id - beat.from_seg_id + 1) for beat in normal_beats)
    targets: list[int] = []
    for beat in outline:
        if beat.is_hook:
            targets.append(max(MIN_BEAT_CHARS, hook_budget_total // max(1, hook_count)))
            continue
        if not normal_beats or total_weight <= 0:
            targets.append(max(MIN_BEAT_CHARS, normal_budget // max(1, len(outline))))
            continue
        weight = max(1, beat.to_seg_id - beat.from_seg_id + 1)
        targets.append(max(MIN_BEAT_CHARS, int(round(normal_budget * weight / total_weight))))
    return targets


def estimate_total_chars(beats: list[ReviewBeat]) -> int:
    return sum(len(beat.narration) for beat in beats)
