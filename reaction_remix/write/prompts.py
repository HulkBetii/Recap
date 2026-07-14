from __future__ import annotations

import json
from typing import Any


def build_write_prompt(*, slots: list[dict[str, Any]]) -> str:
    return f"""
Write new Japanese editorial commentary for a reaction-compilation video.
Return ONLY JSON: {{"slots":[{{"slot_id":"...","text_ja":"..."}}]}}.

Rules:
- Return every requested slot exactly once and no extra slots.
- Write only Japanese editorial commentary. Never translate, dub, quote, or rewrite participant reactions.
- Every factual claim must be supported by that slot's evidence.
- Stay within the character budget and duration intent.
- Keep natural TTS punctuation and clean lead-in/lead-out to adjacent reactions.
- Use Japanese internet-commentary tone contextually when the character budget permits: お前ら, ネキ, ニキ, ワイ, 草, ～だぜ, humor and irony. Do not force slang into every short slot.
- Do not mention subtitles or instruct visual edits.
- No URLs, Markdown, emoji, stage directions, or speaker labels.

SLOTS AND EVIDENCE:
{json.dumps(slots, ensure_ascii=False)}
""".strip()


def build_script_qa_prompt(*, slots: list[dict[str, Any]]) -> str:
    return f"""
Audit this Japanese reaction-video commentary against its evidence.
Return ONLY JSON: {{"pass":boolean,"issues":[{{"slot_id":"...","issue_type":"unsupported_fact|duplicate|tone|transition|reaction_rewrite","suggestion":"..."}}],"notes":"..."}}.

Fail a slot if it invents facts, rewrites participant speech, duplicates adjacent explanation,
uses an unsuitable tone, or leads poorly into/out of its neighboring reactions.
Do not request subtitle or visual edits.

SLOTS:
{json.dumps(slots, ensure_ascii=False)}
""".strip()


def build_slot_repair_prompt(*, slots: list[dict[str, Any]], issues: list[dict[str, str]]) -> str:
    return f"""
Rewrite only the listed Japanese commentary slots. Return ONLY JSON:
{{"slots":[{{"slot_id":"...","text_ja":"..."}}]}}.
Keep the same evidence and editorial purpose. Do not change or dub reactions.

ISSUES:
{json.dumps(issues, ensure_ascii=False)}

SLOTS:
{json.dumps(slots, ensure_ascii=False)}
""".strip()


def build_fit_repair_prompt(*, slots: list[dict[str, Any]]) -> str:
    return f"""
Rewrite only these Japanese commentary slots to fit measured AI33 speech durations.
Return ONLY JSON: {{"slots":[{{"slot_id":"...","text_ja":"..."}}]}}.
Preserve the same evidence, claim, tone, and transition purpose.
For direction=shorten, be substantially more concise. For direction=lengthen, add only supported context.
For direction=clarify, replace hard-to-pronounce wording with ordinary TTS-friendly Japanese while preserving the same claim and budget.
Never change reaction speech, voice speed, or introduce stage directions.

FIT REQUESTS:
{json.dumps(slots, ensure_ascii=False)}
""".strip()
