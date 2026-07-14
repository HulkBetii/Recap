from __future__ import annotations

import json
from typing import Any


def build_plan_prompt(
    *,
    source_duration_s: float,
    block_catalog: list[dict[str, Any]],
    target_duration_s: float,
    hard_min_duration_s: float,
    hard_max_duration_s: float,
    min_reaction_retention: float,
) -> str:
    eligible_commentary_visual_ids = [
        str(item["block_id"])
        for item in block_catalog
        if item.get("kind") == "commentary" and item.get("eligible_commentary_visual") is True
    ]
    return f"""
You are editing a multilingual reaction-compilation video into a new coherent version.
Return ONLY valid JSON with keys: semantic_annotations, ordered_blocks, commentary_slots, exclusions.

JSON shape:
{{
  "semantic_annotations": [{{"block_id":"...","summary_ja":"...","country":null,"topic":"...","sentiment":"...","intensity":0.5,"novelty":0.5}}],
  "ordered_blocks": [{{"block_id": "...", "role": "hook|body|climax|close|branding", "reason": "..."}}],
  "commentary_slots": [{{
    "slot_id": "commentary-slot-0001",
    "after_block_id": null,
    "role": "setup|bridge|punchline|close",
    "evidence_block_ids": ["reaction-..."],
    "preferred_visual_block_ids": ["commentary-..."],
    "reason": "..."
  }}],
  "exclusions": [{{"block_id": "...", "reason_code": "commentary", "reason": "Replace original narrator commentary."}}]
}}

Rules:
- Output IDs only. Never output or invent timecodes.
- Add one evidence-grounded semantic annotation for every block. Do not invent a country or topic when the transcript is unclear.
- Preserve every non-commentary block, including reaction, mixed, unknown, branding, transition, and broll.
- Exclude only original commentary blocks; never paraphrase participant speech.
- Reorder only independent blocks. Preserve sequence_group internal order.
- Do not reuse a source block.
- Commentary evidence must reference retained reaction blocks.
- Commentary visuals may use only kind=commentary blocks marked eligible_commentary_visual=true.
- Eligible commentary visual IDs are exactly: {json.dumps(eligible_commentary_visual_ids, ensure_ascii=False)}.
- Create exactly {len(eligible_commentary_visual_ids)} commentary slots and use every eligible commentary visual ID exactly once. Every slot must contain exactly one eligible visual ID, and that ID must not appear in another slot.
- Never return an empty evidence_block_ids or preferred_visual_block_ids list. If there are more rhetorical ideas than eligible visuals, merge or omit slots instead of emitting an empty list.
- Do not mask, replace, generate, translate, or restyle subtitles.
- Prefer hook -> short setup -> topic groups -> escalation -> punchline -> concise close.
- Source duration is {source_duration_s:.3f}s. Target is {target_duration_s:.3f}s; hard range is {hard_min_duration_s:.3f}-{hard_max_duration_s:.3f}s.
- Retain all reaction speech; the minimum validation threshold remains {min_reaction_retention:.0%} as a safety gate.

BLOCKS:
{json.dumps(block_catalog, ensure_ascii=False)}
""".strip()


def build_plan_repair_prompt(
    *,
    previous: object,
    errors: list[str],
    eligible_commentary_visual_ids: list[str],
) -> str:
    return f"""
Repair the reaction-remix plan JSON. Return the complete corrected JSON only.
Do not add timecodes or new IDs. Keep all valid decisions unchanged.
Eligible commentary visual IDs are exactly: {json.dumps(eligible_commentary_visual_ids, ensure_ascii=False)}.
Create exactly {len(eligible_commentary_visual_ids)} commentary slots and use every eligible commentary visual ID exactly once. Each slot must have non-empty evidence_block_ids and exactly one unique eligible visual ID.
Merge rhetorical ideas as needed so every eligible visual has a grounded slot; never return an empty preferred_visual_block_ids list.

VALIDATION ERRORS:
{json.dumps(errors, ensure_ascii=False)}

PREVIOUS JSON:
{json.dumps(previous, ensure_ascii=False)}
""".strip()
