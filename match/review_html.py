from __future__ import annotations

from collections import defaultdict
from html import escape
from pathlib import Path
import shutil
from typing import Any

from common.schema import EdlPlacement, ReviewBeat, Shot


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if value is None:
        return "-"
    return str(value)


def _resolve_thumb(shots_path: Path, shot: Shot) -> Path:
    thumb = Path(shot.thumb)
    if thumb.is_absolute():
        return thumb
    return shots_path.parent / thumb


def _copy_thumb(source: Path, asset_dir: Path, beat_id: int, ordinal: int, shot_index: int) -> str | None:
    if not source.is_file():
        return None
    asset_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or ".jpg"
    target = asset_dir / f"beat-{beat_id:03d}-{ordinal:02d}-shot-{shot_index:04d}{suffix}"
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        shutil.copyfile(source, target)
    return target.name


def write_review_html(
    *,
    output_path: Path,
    asset_dir: Path,
    shots_path: Path,
    beats: list[ReviewBeat],
    placements: list[EdlPlacement],
    shots: list[Shot],
    qa: dict[str, Any],
    thumbs_per_beat: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    asset_dir.mkdir(parents=True, exist_ok=True)
    shots_by_index = {shot.index: shot for shot in shots}
    placements_by_beat: dict[int, list[EdlPlacement]] = defaultdict(list)
    for placement in placements:
        placements_by_beat[placement.beat_id].append(placement)
    qa_by_beat = {item.get("beat_id"): item for item in qa.get("beats", []) if isinstance(item, dict)}
    semantic_values = [
        float(selected.get("semantic_score", 0.0))
        for beat in qa.get("beats", []) if isinstance(beat, dict)
        for selected in beat.get("selected", []) if isinstance(selected, dict)
    ]
    visual_values = [
        float(selected.get("visual_score", 0.0))
        for beat in qa.get("beats", []) if isinstance(beat, dict)
        for selected in beat.get("selected", []) if isinstance(selected, dict)
    ]
    avg_semantic = sum(semantic_values) / len(semantic_values) if semantic_values else 0.0
    min_semantic = min(semantic_values) if semantic_values else 0.0
    avg_visual = sum(visual_values) / len(visual_values) if visual_values else 0.0
    warnings_count = sum(len(item.get("warnings", [])) for item in qa_by_beat.values())
    rel_asset_dir = asset_dir.relative_to(output_path.parent).as_posix() if asset_dir.is_relative_to(output_path.parent) else asset_dir.as_posix()

    parts: list[str] = [
        "<!doctype html>",
        "<html lang=\"vi\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>EDL Review</title>",
        "<style>body{font-family:Arial,sans-serif;margin:24px;background:#111;color:#eee}a{color:#8cc8ff}.summary,.beat{background:#1d1d1d;border:1px solid #333;border-radius:10px;padding:16px;margin:16px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.clip{background:#292929;border:1px solid #444;border-radius:8px;padding:8px}.clip img{width:100%;height:110px;object-fit:cover;background:#444;border-radius:6px}.warn{color:#ffcc66}.bad{color:#ff7777}.ok{color:#8cff9a}.meta{font-size:13px;color:#bbb;line-height:1.45}.narration{font-size:16px;line-height:1.5}.placeholder{height:110px;display:flex;align-items:center;justify-content:center;background:#444;border-radius:6px;color:#ccc}</style>",
        "</head><body>",
        "<h1>EDL Review</h1>",
        "<section class=\"summary\">",
        f"<div><b>Total beats:</b> {len(beats)} | <b>placements:</b> {len(placements)} | <b>intro excluded:</b> {escape(_fmt(qa.get('n_intro_excluded')))}</div>",
        f"<div><b>selected_from_non_story:</b> {escape(_fmt(qa.get('selected_from_non_story')))} | <b>avg semantic:</b> {avg_semantic:.3f} | <b>min semantic:</b> {min_semantic:.3f} | <b>avg visual:</b> {avg_visual:.3f} | <b>warnings:</b> {warnings_count}</div>",
        "</section>",
    ]
    for beat in sorted(beats, key=lambda item: item.beat_id):
        qa_beat = qa_by_beat.get(beat.beat_id, {})
        beat_warnings = qa_beat.get("warnings", []) if isinstance(qa_beat, dict) else []
        parts.append(f"<section class=\"beat\" id=\"beat-{beat.beat_id}\">")
        parts.append(f"<h2>Beat {beat.beat_id}</h2>")
        parts.append(f"<p class=\"narration\">{escape(beat.narration)}</p>")
        repeat_info = ""
        if isinstance(qa_beat, dict):
            repeat_info = f" | Repeat: {escape(_fmt(qa_beat.get('repeat_ratio')))} | Reused: {escape(_fmt(qa_beat.get('n_reused')))} | Unique shots: {escape(_fmt(qa_beat.get('unique_shots')))}"
        drift_info = ""
        if isinstance(qa_beat, dict):
            drift_info = f" | Avg drift: {escape(_fmt(qa_beat.get('avg_source_drift_s')))}s | Max drift: {escape(_fmt(qa_beat.get('max_source_drift_s')))}s"
        visual_query = ""
        if isinstance(qa_beat, dict) and qa_beat.get("visual_queries"):
            visual_query = " | Visual query: " + escape(" / ".join(str(item) for item in qa_beat.get("visual_queries", [])[:2]))
        parts.append(f"<div class=\"meta\">Source: {beat.src_tc_start:.3f}–{beat.src_tc_end:.3f}s | Hook: {beat.is_hook} | Avg semantic: {escape(_fmt(qa_beat.get('avg_semantic_score') if isinstance(qa_beat, dict) else None))}{repeat_info}{drift_info}{visual_query}</div>")
        if beat_warnings:
            parts.append("<ul class=\"warn\">" + "".join(f"<li>{escape(str(warning))}</li>" for warning in beat_warnings) + "</ul>")
        parts.append("<div class=\"grid\">")
        for ordinal, placement in enumerate(placements_by_beat.get(beat.beat_id, [])[: max(0, thumbs_per_beat)]):
            shot = shots_by_index.get(placement.shot_index)
            qa_selected = None
            if isinstance(qa_beat, dict):
                selected_items = qa_beat.get("selected", [])
                if ordinal < len(selected_items) and isinstance(selected_items[ordinal], dict):
                    qa_selected = selected_items[ordinal]
            image_html = "<div class=\"placeholder\">missing thumbnail</div>"
            missing = False
            keyframe = qa_selected.get("selected_keyframe") if qa_selected else None
            keyframe_path = Path(str(keyframe.get("frame_path"))) if isinstance(keyframe, dict) and keyframe.get("frame_path") else None
            if shot is not None:
                source_image = keyframe_path if keyframe_path is not None and keyframe_path.is_file() else _resolve_thumb(shots_path, shot)
                copied = _copy_thumb(source_image, asset_dir, beat.beat_id, ordinal, shot.index)
                if copied:
                    image_html = f"<img src=\"{escape(rel_asset_dir + '/' + copied)}\" alt=\"shot {shot.index}\">"
                else:
                    missing = True
            classes = "clip bad" if shot is not None and not shot.is_story else "clip"
            parts.append(f"<article class=\"{classes}\">{image_html}")
            if missing:
                parts.append("<div class=\"warn\">missing thumbnail</div>")
            parts.append(
                "<div class=\"meta\">"
                f"TL {placement.tl_start:.3f}–{placement.tl_end:.3f}<br>"
                f"SRC {placement.src_in:.3f}–{placement.src_out:.3f}<br>"
                f"shot {placement.shot_index} | reused={placement.reused}<br>"
                f"semantic={escape(_fmt(qa_selected.get('semantic_score') if qa_selected else None))} rank={escape(_fmt(qa_selected.get('semantic_rank') if qa_selected else None))}<br>"
                f"visual={escape(_fmt(qa_selected.get('visual_score') if qa_selected else None))} raw={escape(_fmt(qa_selected.get('visual_raw_cosine') if qa_selected else None))} rank={escape(_fmt(qa_selected.get('visual_rank') if qa_selected else None))}<br>"
                f"expected={escape(_fmt(qa_selected.get('expected_src_position') if qa_selected else None))} drift={escape(_fmt(qa_selected.get('source_drift_s') if qa_selected else None))} chrono={escape(_fmt(qa_selected.get('chronology_score') if qa_selected else None))}<br>"
                f"drift tier={escape(_fmt(qa_selected.get('drift_tier') if qa_selected else None))}<br>"
                f"motion={escape(_fmt(shot.motion_score if shot else None))} bright={escape(_fmt(shot.brightness if shot else None))} face={escape(_fmt(shot.face_count if shot else None))}<br>"
                f"is_story={escape(_fmt(shot.is_story if shot else None))} reason={escape(_fmt(shot.exclude_reason if shot else None))}"
                "</div></article>"
            )
        parts.append("</div>")
        alternatives = qa_beat.get("visual_alternatives", []) if isinstance(qa_beat, dict) else []
        if alternatives:
            parts.append("<h3>Visual alternatives</h3><div class=\"grid\">")
            for alt_ordinal, alternative in enumerate(alternatives):
                shot_index = int(alternative.get("shot_index", -1))
                keyframe = alternative.get("selected_keyframe")
                frame_path = Path(str(keyframe.get("frame_path"))) if isinstance(keyframe, dict) and keyframe.get("frame_path") else None
                image_html = "<div class=\"placeholder\">missing keyframe</div>"
                if frame_path is not None:
                    copied = _copy_thumb(frame_path, asset_dir, beat.beat_id, 100 + alt_ordinal, shot_index)
                    if copied:
                        image_html = f"<img src=\"{escape(rel_asset_dir + '/' + copied)}\" alt=\"alternative shot {shot_index}\">"
                parts.append(
                    "<article class=\"clip\">"
                    + image_html
                    + "<div class=\"meta\">"
                    + f"shot {shot_index} | tier={escape(_fmt(alternative.get('drift_tier')))}<br>visual={escape(_fmt(alternative.get('visual_score')))} raw={escape(_fmt(alternative.get('visual_raw_cosine')))}<br>combined={escape(_fmt(alternative.get('total_score_no_reuse')))}"
                    + "</div></article>"
                )
            parts.append("</div>")
        parts.append("</section>")
    parts.append("</body></html>")
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
