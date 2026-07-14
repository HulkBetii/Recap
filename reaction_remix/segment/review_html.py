from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from common.schema import ReactionBlocks, ReactionTranscript


def write_blocks_review_html(
    output_path: Path,
    blocks: ReactionBlocks,
    transcript: ReactionTranscript,
    *,
    shots: list[dict[str, Any]] | None = None,
    shots_base: Path | None = None,
) -> None:
    turns = {turn.turn_id: turn for turn in transcript.turns}
    cuts = {cut.cut_point_id: cut for cut in blocks.cut_points}
    shots = shots or []
    rows: list[str] = []
    for block in blocks.blocks:
        midpoint = block.tc_start + (block.tc_end - block.tc_start) / 2
        nearest = min(
            shots,
            key=lambda item: abs(float(item.get("tc_start", 0.0)) + (float(item.get("tc_end", 0.0)) - float(item.get("tc_start", 0.0))) / 2 - midpoint),
            default=None,
        )
        thumb = ""
        if nearest and nearest.get("thumb"):
            thumb_path = Path(str(nearest["thumb"]))
            if not thumb_path.is_absolute() and shots_base is not None:
                thumb_path = shots_base / thumb_path
            thumb = f'<img src="{html.escape(thumb_path.as_posix())}" loading="lazy">'
        text = "<br>".join(html.escape(turns[turn_id].text) for turn_id in block.turn_ids if turn_id in turns)
        warnings = "<br>".join(html.escape(value) for value in block.warnings)
        cut_details: list[str] = []
        for label, cut_id in (("start", block.start_cut_point_id), ("end", block.end_cut_point_id)):
            cut = cuts[cut_id]
            if cut.safety_mode is not None:
                safety_mode = cut.safety_mode
            elif cut.left_handle_s is not None and cut.right_handle_s is not None:
                safety_mode = "insufficient_handle / protected edge"
            else:
                safety_mode = "legacy"
            handles = (
                "legacy handles"
                if cut.left_handle_s is None or cut.right_handle_s is None
                else f"L {cut.left_handle_s:.3f}s / R {cut.right_handle_s:.3f}s"
            )
            cut_details.append(
                f"{label} {html.escape(cut_id)}: {html.escape(safety_mode)}; {html.escape(handles)}"
            )
        rows.append(
            "<tr>"
            f"<td>{thumb}</td><td><b>{html.escape(block.block_id)}</b><br>{block.tc_start:.3f}-{block.tc_end:.3f}" 
            f"<br>content {block.content_tc_start:.3f}-{block.content_tc_end:.3f}</td>"
            f"<td class=kind>{html.escape(block.kind)}</td><td>{text}</td>"
            f"<td>{html.escape(', '.join(block.speaker_ids))}<br>{html.escape(', '.join(block.language_codes))}</td>"
            f"<td>class {block.classification_confidence:.3f}<br>lang {block.language_confidence:.3f}"
            f"<br>speaker {block.speaker_confidence:.3f}<br>boundary {block.boundary_confidence:.3f}</td>"
            f"<td>{'<br>'.join(cut_details)}<br>{warnings}</td></tr>"
        )
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Reaction blocks review</title>
<style>body{{font:14px sans-serif;background:#f4f1e8;color:#171717}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #aaa;padding:7px;vertical-align:top}}th{{background:#19323c;color:white;position:sticky;top:0}}img{{width:180px;max-height:110px;object-fit:cover}}.kind{{font-weight:bold}}</style>
</head><body><h1>Reaction Blocks Audit</h1><p>{len(blocks.blocks)} blocks, source {blocks.source_duration_s:.3f}s</p>
<table><thead><tr><th>Frame</th><th>Span</th><th>Kind</th><th>Transcript</th><th>Speaker / language</th><th>Confidence</th><th>Cuts / warnings</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
