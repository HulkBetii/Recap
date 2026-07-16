from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path
from typing import Any, Callable

MatchRunner = Callable[[argparse.Namespace], int]


def run_sentence_refinement_ab(args: argparse.Namespace, runner: MatchRunner) -> int:
    output_path = Path(args.output).expanduser().resolve()
    work_dir = Path(getattr(args, "work_dir", output_path.parent / "work" / "match")).expanduser().resolve()
    ab_root = (
        Path(args.sentence_refinement_ab_output_dir).expanduser().resolve()
        if getattr(args, "sentence_refinement_ab_output_dir", None)
        else work_dir / "sentence_refinement_ab"
    )
    baseline_dir = ab_root / "baseline"
    guarded_dir = ab_root / "guarded"
    for label, mode, run_dir in (("baseline", "off", baseline_dir), ("guarded", "guarded", guarded_dir)):
        run_args = _clone_args_for_ab(args, mode=mode, output_dir=run_dir)
        result = runner(run_args)
        if result != 0:
            return result

    report = build_sentence_refinement_ab_report(
        baseline_dir=baseline_dir,
        guarded_dir=guarded_dir,
    )
    report["ab_root"] = str(ab_root)
    report["main_output_preserved"] = str(output_path)
    json_path = output_path.with_name("match_refinement_ab.qa.json")
    html_path = output_path.with_name("match_refinement_ab.html")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_sentence_refinement_ab_html(report), encoding="utf-8")
    return 0


def _clone_args_for_ab(args: argparse.Namespace, *, mode: str, output_dir: Path) -> argparse.Namespace:
    output_dir.mkdir(parents=True, exist_ok=True)
    clone = argparse.Namespace(**vars(args))
    clone.sentence_refinement_ab = False
    clone.sentence_refinement_ab_output_dir = None
    clone.sentence_refinement_mode = mode
    clone.output = output_dir / "edl.json"
    clone.output_qa = output_dir / "edl.qa.json"
    clone.output_sync_qa = output_dir / "edl.sync.qa.json"
    clone.output_visual_qa = output_dir / "edl.visual.qa.json"
    clone.output_review_html = output_dir / "edl.review.html"
    clone.review_asset_dir = output_dir / "edl.review"
    clone.review_html = False
    clone.work_dir = output_dir / "work"
    return clone


def build_sentence_refinement_ab_report(*, baseline_dir: Path, guarded_dir: Path) -> dict[str, Any]:
    baseline = _load_run("baseline", baseline_dir)
    guarded = _load_run("guarded", guarded_dir)
    beat_rows = _compare_beats(baseline["beats"], guarded["beats"])
    improved = [row["beat_id"] for row in beat_rows if row["status"] == "improved"]
    worsened = [row["beat_id"] for row in beat_rows if row["status"] == "worsened"]
    needs_eye_review = [
        {
            "beat_id": row["beat_id"],
            "reasons": row["review_reasons"],
        }
        for row in beat_rows
        if row["review_reasons"]
    ]
    return {
        "version": 1,
        "baseline": baseline["metrics"],
        "guarded": guarded["metrics"],
        "summary": {
            "improved_beats": improved,
            "worsened_beats": worsened,
            "n_improved_beats": len(improved),
            "n_worsened_beats": len(worsened),
            "needs_eye_review": needs_eye_review,
            "max_drift_before_s": baseline["metrics"]["max_source_drift_s"],
            "max_drift_after_s": guarded["metrics"]["max_source_drift_s"],
            "warning_count_before": baseline["metrics"]["warning_count"],
            "warning_count_after": guarded["metrics"]["warning_count"],
            "short_clip_count_before": baseline["metrics"]["short_clip_count"],
            "short_clip_count_after": guarded["metrics"]["short_clip_count"],
            "max_repeat_ratio_before": baseline["metrics"]["max_repeat_ratio"],
            "max_repeat_ratio_after": guarded["metrics"]["max_repeat_ratio"],
            "accepted_beats": guarded["metrics"]["sentence_refinement_summary"].get("accepted_beats", 0),
            "rejected_beats": guarded["metrics"]["sentence_refinement_summary"].get("rejected_beats", 0),
        },
        "beats": beat_rows,
    }


def _load_run(label: str, output_dir: Path) -> dict[str, Any]:
    qa_path = output_dir / "edl.qa.json"
    meta_path = output_dir / "edl.meta.json"
    qa = _read_json(qa_path)
    meta = _read_json(meta_path)
    beats = {
        int(beat["beat_id"]): beat
        for beat in qa.get("beats", [])
        if isinstance(beat, dict) and "beat_id" in beat
    }
    max_drifts = [_float(beat.get("max_source_drift_s")) for beat in beats.values()]
    repeat_ratios = [_float(beat.get("repeat_ratio")) for beat in beats.values()]
    warning_count = sum(len(beat.get("warnings", [])) for beat in beats.values())
    short_clip_count = sum(int(beat.get("short_clip_count", 0) or 0) for beat in beats.values())
    refinement_summary = qa.get("sentence_refinement_summary", {})
    if not isinstance(refinement_summary, dict):
        refinement_summary = {}
    accepted_beats = sum(1 for beat in beats.values() if beat.get("sentence_refinement_accepted"))
    rejected_beats = sum(1 for beat in beats.values() if beat.get("sentence_refinement_rejected_reason"))
    metrics = {
        "label": label,
        "output": str(output_dir / "edl.json"),
        "qa": str(qa_path),
        "meta": str(meta_path),
        "algorithm_version": meta.get("algorithm_version"),
        "n_placements": int(meta.get("n_placements", 0) or 0),
        "coverage_ok": bool(meta.get("coverage_ok", False)),
        "warning_count": warning_count,
        "short_clip_count": short_clip_count,
        "max_source_drift_s": round(max(max_drifts, default=0.0), 3),
        "avg_source_drift_s": round(sum(max_drifts) / len(max_drifts), 3) if max_drifts else 0.0,
        "max_repeat_ratio": round(max(repeat_ratios, default=0.0), 6),
        "avg_repeat_ratio": round(sum(repeat_ratios) / len(repeat_ratios), 6) if repeat_ratios else 0.0,
        "sentence_refinement_summary": refinement_summary,
        "sentence_refinement_accepted_beats": accepted_beats,
        "sentence_refinement_rejected_beats": rejected_beats,
    }
    return {"metrics": metrics, "beats": beats}


def _compare_beats(baseline: dict[int, dict[str, Any]], guarded: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for beat_id in sorted(set(baseline) | set(guarded)):
        before = baseline.get(beat_id, {})
        after = guarded.get(beat_id, {})
        before_drift = _float(before.get("max_source_drift_s"))
        after_drift = _float(after.get("max_source_drift_s"))
        before_repeat = _float(before.get("repeat_ratio"))
        after_repeat = _float(after.get("repeat_ratio"))
        before_short = int(before.get("short_clip_count", 0) or 0)
        after_short = int(after.get("short_clip_count", 0) or 0)
        before_warnings = len(before.get("warnings", []))
        after_warnings = len(after.get("warnings", []))
        drift_delta = round(after_drift - before_drift, 3)
        repeat_delta = round(after_repeat - before_repeat, 6)
        short_delta = after_short - before_short
        warning_delta = after_warnings - before_warnings
        source_jump_max = _float(after.get("sentence_refinement_max_source_jump_s"))
        low_confidence_count = int(after.get("sentence_refinement_low_confidence_count", 0) or 0)
        rejected_reason = after.get("sentence_refinement_rejected_reason")
        status = "unchanged"
        if drift_delta < -0.5 and short_delta <= 0 and repeat_delta <= 1e-6:
            status = "improved"
        if drift_delta > 0.5 or short_delta > 0 or repeat_delta > 0.05 or warning_delta > 0:
            status = "worsened"
        review_reasons: list[str] = []
        if status == "worsened":
            review_reasons.append("guarded metrics worsened")
        if source_jump_max > 45.0:
            review_reasons.append(f"large source jump {source_jump_max:.3f}s")
        if low_confidence_count:
            review_reasons.append(f"low-confidence skipped chunks {low_confidence_count}")
        if rejected_reason:
            review_reasons.append(f"guarded rejected: {rejected_reason}")
        rows.append(
            {
                "beat_id": beat_id,
                "status": status,
                "narration_preview": after.get("narration_preview") or before.get("narration_preview"),
                "max_drift_before_s": round(before_drift, 3),
                "max_drift_after_s": round(after_drift, 3),
                "delta_max_drift_s": drift_delta,
                "repeat_ratio_before": round(before_repeat, 6),
                "repeat_ratio_after": round(after_repeat, 6),
                "delta_repeat_ratio": repeat_delta,
                "short_clip_count_before": before_short,
                "short_clip_count_after": after_short,
                "delta_short_clip_count": short_delta,
                "warning_count_before": before_warnings,
                "warning_count_after": after_warnings,
                "delta_warning_count": warning_delta,
                "sentence_refinement_used": bool(after.get("sentence_refinement_used", False)),
                "sentence_refinement_accepted": bool(after.get("sentence_refinement_accepted", False)),
                "sentence_refinement_rejected_reason": rejected_reason,
                "sentence_refinement_reason": after.get("sentence_refinement_reason"),
                "sentence_refinement_replaced_duration_s": _float(after.get("sentence_refinement_replaced_duration_s")),
                "sentence_refinement_max_source_jump_s": source_jump_max,
                "sentence_refinement_avg_source_jump_s": _float(after.get("sentence_refinement_avg_source_jump_s")),
                "sentence_refinement_low_confidence_count": low_confidence_count,
                "review_reasons": review_reasons,
            }
        )
    return rows


def render_sentence_refinement_ab_html(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    rows = report.get("beats", [])
    parts = [
        "<!doctype html>",
        "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>Match Refinement A/B</title>",
        "<style>body{font-family:Arial,sans-serif;margin:24px;background:#111;color:#eee}table{border-collapse:collapse;width:100%;margin-top:16px}th,td{border:1px solid #333;padding:8px;vertical-align:top}th{background:#222}.ok{color:#8cff9a}.bad{color:#ff7777}.muted{color:#bbb}.summary{background:#1d1d1d;border:1px solid #333;border-radius:8px;padding:16px}</style>",
        "</head><body>",
        "<h1>Match Refinement A/B</h1>",
        "<section class=\"summary\">",
        f"<div>Improved beats: {escape(str(summary.get('n_improved_beats', 0)))} | Worsened beats: {escape(str(summary.get('n_worsened_beats', 0)))}</div>",
        f"<div>Max drift: {escape(str(summary.get('max_drift_before_s')))}s -> {escape(str(summary.get('max_drift_after_s')))}s | Short clips: {escape(str(summary.get('short_clip_count_before')))} -> {escape(str(summary.get('short_clip_count_after')))}</div>",
        f"<div>Warnings: {escape(str(summary.get('warning_count_before')))} -> {escape(str(summary.get('warning_count_after')))} | Repeat max: {escape(str(summary.get('max_repeat_ratio_before')))} -> {escape(str(summary.get('max_repeat_ratio_after')))} | Accepted: {escape(str(summary.get('accepted_beats')))} | Rejected: {escape(str(summary.get('rejected_beats')))}</div>",
        "</section>",
        "<table><thead><tr><th>Beat</th><th>Status</th><th>Drift</th><th>Repeat/Short/Warn</th><th>Refinement</th><th>Review</th></tr></thead><tbody>",
    ]
    for row in rows:
        status = str(row.get("status", "unchanged"))
        status_class = "ok" if status == "improved" else "bad" if status == "worsened" else "muted"
        review_reasons = row.get("review_reasons", [])
        parts.append(
            "<tr>"
            + f"<td><b>{escape(str(row.get('beat_id')))}</b><br><span class=\"muted\">{escape(str(row.get('narration_preview') or ''))}</span></td>"
            + f"<td class=\"{status_class}\">{escape(status)}</td>"
            + f"<td>{escape(str(row.get('max_drift_before_s')))} -> {escape(str(row.get('max_drift_after_s')))}<br>delta {escape(str(row.get('delta_max_drift_s')))}s</td>"
            + f"<td>repeat {escape(str(row.get('repeat_ratio_before')))} -> {escape(str(row.get('repeat_ratio_after')))}<br>short {escape(str(row.get('short_clip_count_before')))} -> {escape(str(row.get('short_clip_count_after')))}<br>warn {escape(str(row.get('warning_count_before')))} -> {escape(str(row.get('warning_count_after')))}</td>"
            + f"<td>used={escape(str(row.get('sentence_refinement_used')))}<br>accepted={escape(str(row.get('sentence_refinement_accepted')))}<br>reason={escape(str(row.get('sentence_refinement_reason')))}<br>rejected={escape(str(row.get('sentence_refinement_rejected_reason')))}<br>replaced={escape(str(row.get('sentence_refinement_replaced_duration_s')))}s<br>jump max/avg={escape(str(row.get('sentence_refinement_max_source_jump_s')))} / {escape(str(row.get('sentence_refinement_avg_source_jump_s')))}</td>"
            + f"<td>{escape('; '.join(str(item) for item in review_reasons))}</td>"
            + "</tr>"
        )
    parts.extend(["</tbody></table>", "</body></html>"])
    return "\n".join(parts) + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing A/B artifact: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"A/B artifact must be a JSON object: {path}")
    return raw


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
