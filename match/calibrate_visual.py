from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from common.schema import VisualGoldenBeat, VisualGoldenSet, write_json

DEFAULT_WEIGHTS = (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40)


def evaluate_weight(beats: list[VisualGoldenBeat], weight: float, *, min_visual_clip: float) -> dict[str, float]:
    ndcg_values: list[float] = []
    acceptable_top1 = 0
    drift_values: list[float] = []
    reused_count = 0
    short_count = 0
    high_drift_count = 0
    for beat in beats:
        minimum_tier = min(candidate.drift_tier for candidate in beat.candidates)
        eligible = [candidate for candidate in beat.candidates if candidate.drift_tier == minimum_tier]
        ranked = sorted(
            eligible,
            key=lambda candidate: (candidate.base_score + weight * candidate.visual_score, candidate.visual_score, -candidate.shot_index),
            reverse=True,
        )
        gains = [1.0 if candidate.acceptable else 0.0 for candidate in ranked[:5]]
        dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
        ideal_count = min(5, sum(1 for candidate in eligible if candidate.acceptable))
        ideal_dcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
        ndcg_values.append(dcg / ideal_dcg if ideal_dcg else 0.0)
        selected = ranked[0]
        acceptable_top1 += int(selected.acceptable)
        drift_values.append(selected.source_drift_s)
        high_drift_count += int(selected.drift_tier >= 2)
        reused_count += int(selected.reused)
        short_count += int(selected.duration_s < min_visual_clip)
    count = max(1, len(beats))
    return {
        "weight": weight,
        "ndcg_at_5": sum(ndcg_values) / count,
        "acceptable_top1_rate": acceptable_top1 / count,
        "avg_source_drift_s": sum(drift_values) / count,
        "high_drift_rate": high_drift_count / count,
        "reuse_rate": reused_count / count,
        "short_clip_rate": short_count / count,
    }


def calibrate_visual_weight(
    golden: VisualGoldenSet,
    *,
    weights: tuple[float, ...] = DEFAULT_WEIGHTS,
    min_visual_clip: float = 0.6,
) -> dict[str, Any]:
    if not weights or any(weight < 0 for weight in weights):
        raise ValueError("calibration weights must be non-empty and >= 0")
    metrics = [evaluate_weight(golden.beats, weight, min_visual_clip=min_visual_clip) for weight in sorted(set(weights))]
    baseline = next((item for item in metrics if item["weight"] == 0.0), metrics[0])
    feasible = [
        item
        for item in metrics
        if item["avg_source_drift_s"] <= baseline["avg_source_drift_s"] + 1e-6
        and item["high_drift_rate"] <= baseline["high_drift_rate"] + 1e-6
        and item["reuse_rate"] <= baseline["reuse_rate"] + 1e-6
        and item["short_clip_rate"] <= baseline["short_clip_rate"] + 1e-6
    ]
    selected = max(
        feasible or metrics,
        key=lambda item: (item["ndcg_at_5"], item["acceptable_top1_rate"], -item["weight"]),
    )
    return {
        "version": 1,
        "videos": golden.videos,
        "n_beats": len(golden.beats),
        "selected_weight": selected["weight"],
        "baseline": baseline,
        "metrics": metrics,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calibrate G5 visual weight from labeled candidate rankings.")
    parser.add_argument("--golden", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--weights", default=",".join(str(value) for value in DEFAULT_WEIGHTS))
    parser.add_argument("--min-visual-clip", default=0.6, type=float)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    golden = VisualGoldenSet.model_validate_json(args.golden.read_text(encoding="utf-8"))
    weights = tuple(float(value.strip()) for value in args.weights.split(",") if value.strip())
    report = calibrate_visual_weight(golden, weights=weights, min_visual_clip=args.min_visual_clip)
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
