from __future__ import annotations

import html
from pathlib import Path
from typing import Any


def write_review_html(path: Path, qa: Any) -> None:
    warnings = "".join(f"<li>{html.escape(item)}</li>" for item in qa.warnings)
    protected_ids = qa.commentary.protected_narrator_overlap_block_ids
    protected_items = "".join(f"<li><code>{html.escape(item)}</code></li>" for item in protected_ids)
    failed_reaction_ids = qa.reaction_preservation.failed_placement_ids
    failed_reaction_items = "".join(
        f"<li><code>{html.escape(item)}</code></li>" for item in failed_reaction_ids
    )
    body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Reaction Remix QA</title>
<style>body{{font-family:Segoe UI,sans-serif;max-width:960px;margin:40px auto;padding:0 20px}}code{{background:#eee;padding:2px 5px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:8px;text-align:left}}.pass{{color:#176b2c}}.fail{{color:#a01818}}</style></head>
<body><h1>Reaction Remix QA</h1><p class="{html.escape(qa.status)}">Status: <strong>{html.escape(qa.status.upper())}</strong></p>
<table><tr><th>Gate</th><th>Status</th></tr>
<tr><td>Duration</td><td>{html.escape(qa.duration.status)}</td></tr>
<tr><td>Reaction preservation</td><td>{html.escape(qa.reaction_preservation.status)}</td></tr>
<tr><td>Commentary</td><td>{html.escape(qa.commentary.status)}</td></tr>
<tr><td>Visual policy</td><td>{html.escape(qa.visual_policy.status)}</td></tr>
<tr><td>Audio</td><td>{html.escape(qa.audio.status)}</td></tr>
<tr><td>Timeline/decode</td><td>{html.escape(qa.timeline.status)}</td></tr></table>
<h2>Failed protected placements</h2><p>Count: {len(failed_reaction_ids)}</p>
<ul>{failed_reaction_items or '<li>None</li>'}</ul>
<h2>Protected narrator overlap</h2><p>Count: {len(protected_ids)}</p>
<ul>{protected_items or '<li>None</li>'}</ul>
<h2>Warnings</h2><ul>{warnings or '<li>None</li>'}</ul>
<p>Output: <code>{html.escape(qa.output_path)}</code></p></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
