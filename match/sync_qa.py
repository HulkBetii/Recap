from __future__ import annotations

from collections import defaultdict
from typing import Any

from common.schema import BeatTiming, EdlPlacement, ReviewBeat


def duration(start: float, end: float) -> float:
    return max(0.0, end - start)


def overlap_duration(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def build_sync_qa(
    *,
    beats: list[ReviewBeat],
    timings: list[BeatTiming],
    placements: list[EdlPlacement],
    fps: float | None = None,
    tolerance_s: float = 0.08,
    short_clip_threshold_s: float = 0.0,
) -> dict[str, Any]:
    timings_by_id = {timing.beat_id: timing for timing in timings}
    next_timing_by_id = {timing.beat_id: timings[index + 1] for index, timing in enumerate(sorted(timings, key=lambda item: item.beat_id)[:-1])}
    beats_by_id = {beat.beat_id: beat for beat in beats}
    placements_by_beat: dict[int, list[EdlPlacement]] = defaultdict(list)
    for placement in placements:
        placements_by_beat[placement.beat_id].append(placement)

    beat_reports: list[dict[str, Any]] = []
    warning_counts: dict[str, int] = defaultdict(int)
    timeline_gaps: list[dict[str, float]] = []
    timeline_overlaps: list[dict[str, float]] = []
    ordered_placements = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.beat_id))
    previous_end = 0.0
    for index, placement in enumerate(ordered_placements):
        delta = placement.tl_start - previous_end
        if index > 0 and delta > tolerance_s:
            timeline_gaps.append({"index": index, "start": round(previous_end, 3), "end": round(placement.tl_start, 3), "duration": round(delta, 3)})
        if index > 0 and delta < -tolerance_s:
            timeline_overlaps.append({"index": index, "previous_end": round(previous_end, 3), "start": round(placement.tl_start, 3), "duration": round(-delta, 3)})
        previous_end = max(previous_end, placement.tl_end)

    for timing in sorted(timings, key=lambda item: item.beat_id):
        beat = beats_by_id.get(timing.beat_id)
        beat_placements = sorted(placements_by_beat.get(timing.beat_id, []), key=lambda item: (item.tl_start, item.tl_end, item.src_in))
        warnings: list[str] = []
        if not beat_placements:
            warnings.append("empty_beat")
            warning_counts["empty_beat"] += 1
            beat_reports.append({
                "beat_id": timing.beat_id,
                "timing": timing.model_dump(mode="json"),
                "n_placements": 0,
                "warnings": warnings,
            })
            continue

        edl_start = min(item.tl_start for item in beat_placements)
        edl_end = max(item.tl_end for item in beat_placements)
        clipped_starts = [max(item.tl_start, timing.tl_start) for item in beat_placements if overlap_duration(item.tl_start, item.tl_end, timing.tl_start, timing.tl_end) > 0]
        clipped_ends = [min(item.tl_end, timing.tl_end) for item in beat_placements if overlap_duration(item.tl_start, item.tl_end, timing.tl_start, timing.tl_end) > 0]
        active_start = min(clipped_starts) if clipped_starts else edl_start
        active_end = max(clipped_ends) if clipped_ends else edl_end
        edl_duration = sum(duration(item.tl_start, item.tl_end) for item in beat_placements)
        in_timing_duration = sum(overlap_duration(item.tl_start, item.tl_end, timing.tl_start, timing.tl_end) for item in beat_placements)
        outside_timing_duration = max(0.0, edl_duration - in_timing_duration)
        next_timing = next_timing_by_id.get(timing.beat_id)
        inter_beat_pause_filler_s = 0.0
        if next_timing is not None:
            inter_beat_pause_filler_s = sum(
                overlap_duration(item.tl_start, item.tl_end, timing.tl_end, next_timing.tl_start)
                for item in beat_placements
            )
        unexpected_outside_timing_s = max(0.0, outside_timing_duration - inter_beat_pause_filler_s)
        start_delta = active_start - timing.tl_start
        end_delta = active_end - timing.tl_end
        duration_delta = in_timing_duration - timing.duration
        src_order_mismatch = any(beat_placements[index].src_in > beat_placements[index + 1].src_in + tolerance_s for index in range(len(beat_placements) - 1))
        repeated_count = sum(1 for item in beat_placements if item.reused)
        unique_shots = len({item.shot_index for item in beat_placements})
        clip_durations = [duration(item.tl_start, item.tl_end) for item in beat_placements]
        max_clip_s = max(clip_durations)
        min_clip_s = min(clip_durations)
        short_clip_count = sum(1 for clip_s in clip_durations if short_clip_threshold_s > 0 and clip_s < short_clip_threshold_s)
        avg_clip_s = edl_duration / len(beat_placements)

        if abs(start_delta) > tolerance_s:
            warnings.append("beat_start_delta")
            warning_counts["beat_start_delta"] += 1
        if abs(end_delta) > max(tolerance_s, 0.12):
            warnings.append("beat_end_delta")
            warning_counts["beat_end_delta"] += 1
        if abs(duration_delta) > max(tolerance_s, 0.12):
            warnings.append("beat_duration_delta")
            warning_counts["beat_duration_delta"] += 1
        if unexpected_outside_timing_s > tolerance_s:
            warnings.append("placement_outside_beat_timing")
            warning_counts["placement_outside_beat_timing"] += 1
        if src_order_mismatch:
            warnings.append("source_order_mismatch")
            warning_counts["source_order_mismatch"] += 1
        if repeated_count and repeated_count / len(beat_placements) > 0.25:
            warnings.append("high_reuse_ratio")
            warning_counts["high_reuse_ratio"] += 1
        if max_clip_s > 5.25:
            warnings.append("long_clip")
            warning_counts["long_clip"] += 1
        if short_clip_count:
            warnings.append("short_clip")
            warning_counts["short_clip"] += 1

        beat_reports.append({
            "beat_id": timing.beat_id,
            "narration_preview": (beat.narration[:160] if beat else None),
            "timing": {
                "tl_start": timing.tl_start,
                "tl_end": timing.tl_end,
                "duration": timing.duration,
            },
            "edl": {
                "tl_start": round(edl_start, 3),
                "tl_end": round(edl_end, 3),
                "active_tl_start": round(active_start, 3),
                "active_tl_end": round(active_end, 3),
                "duration_sum": round(edl_duration, 3),
                "in_timing_duration": round(in_timing_duration, 3),
                "outside_timing_duration": round(outside_timing_duration, 3),
                "inter_beat_pause_filler_s": round(inter_beat_pause_filler_s, 3),
                "unexpected_outside_timing_s": round(unexpected_outside_timing_s, 3),
            },
            "deltas": {
                "start_s": round(start_delta, 3),
                "end_s": round(end_delta, 3),
                "duration_s": round(duration_delta, 3),
                "start_frames": round(start_delta * fps) if fps else None,
                "end_frames": round(end_delta * fps) if fps else None,
            },
            "n_placements": len(beat_placements),
            "unique_shots": unique_shots,
            "reused_count": repeated_count,
            "reuse_ratio": round(repeated_count / len(beat_placements), 6),
            "avg_clip_s": round(avg_clip_s, 3),
            "min_clip_s": round(min_clip_s, 3),
            "max_clip_s": round(max_clip_s, 3),
            "short_clip_count": short_clip_count,
            "source_order_mismatch": src_order_mismatch,
            "warnings": warnings,
        })

    return {
        "version": 2,
        "fps": fps,
        "tolerance_s": tolerance_s,
        "short_clip_threshold_s": short_clip_threshold_s,
        "summary": {
            "n_beats": len(timings),
            "n_placements": len(placements),
            "n_timeline_gaps": len(timeline_gaps),
            "n_timeline_overlaps": len(timeline_overlaps),
            "warning_counts": dict(sorted(warning_counts.items())),
            "max_abs_start_delta_s": round(max((abs(item.get("deltas", {}).get("start_s", 0.0)) for item in beat_reports), default=0.0), 3),
            "max_abs_end_delta_s": round(max((abs(item.get("deltas", {}).get("end_s", 0.0)) for item in beat_reports), default=0.0), 3),
        },
        "timeline_gaps": timeline_gaps[:100],
        "timeline_overlaps": timeline_overlaps[:100],
        "beats": beat_reports,
    }
