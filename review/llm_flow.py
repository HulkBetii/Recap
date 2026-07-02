from __future__ import annotations

import json
from typing import Protocol

from common.schema import ReviewBeat
from review.budget import estimate_total_chars
from review.json_utils import extract_json
from review.models import NarrationBeat, OutlineBeat, OutlineResult, QaResult


class ChatClient(Protocol):
    async def ask(self, prompt: str) -> str:
        ...


def build_outline_prompt(
    *,
    film_map_view: str,
    target_video_s: float,
    char_budget: int,
    min_coverage: float,
    style_sample: str = "",
) -> str:
    style_block = f"\nSTYLE SAMPLE:\n{style_sample}\n" if style_sample else ""
    return f"""
You are planning a Vietnamese movie recap script.
Return ONLY valid JSON with keys: glossary, outline, hook.

Rules:
- Do NOT output timecodes.
- Use only segment ids from the FILM_MAP.
- glossary: list of characters with Vietnamese canonical name and short role.
- outline: ordered beats covering the full plot. Each beat has from_seg_id, to_seg_id, summary.
- hook: list of exciting segment ids for a cold-open, usually from later in the story, without spoiling the ending.
- Non-hook outline beats must be chronological and cover at least {min_coverage:.0%} of segment ids.
- Target recap length: about {target_video_s:.1f}s, char budget about {char_budget} Vietnamese characters.
{style_block}
FILM_MAP:
{film_map_view}
""".strip()


def build_narration_prompt(
    *,
    outline: list[OutlineBeat],
    glossary: list[dict],
    char_targets: list[int],
    style_sample: str = "",
) -> str:
    style_block = f"\nSTYLE SAMPLE:\n{style_sample}\n" if style_sample else ""
    payload = [
        {
            "beat_id": index,
            "from_seg_id": beat.from_seg_id,
            "to_seg_id": beat.to_seg_id,
            "summary": beat.summary,
            "is_hook": beat.is_hook,
            "char_target": char_targets[index],
        }
        for index, beat in enumerate(outline)
    ]
    return f"""
Write Vietnamese narration for each recap beat.
Return ONLY a JSON array of objects: {{"beat_id": number, "narration": string}}.

Rules:
- Dramatic fast-paced Vietnamese recap style.
- Use the glossary names consistently.
- Do NOT quote original dialogue verbatim; transform into narration.
- Stay within ±20% of each char_target where possible.
- Hook beat must be gripping and placed first.
{style_block}
GLOSSARY:
{json.dumps(glossary, ensure_ascii=False)}

BEATS:
{json.dumps(payload, ensure_ascii=False)}
""".strip()


def build_qa_prompt(
    *,
    film_map_view: str,
    beats: list[ReviewBeat],
    glossary: list[dict],
    char_budget: int,
    coverage_pct: float,
) -> str:
    payload = [beat.model_dump() for beat in beats]
    return f"""
Review this Vietnamese recap script against the film map.
Return ONLY valid JSON: {{"pass": boolean, "issues": [{{"beat_id": number, "type": string, "suggestion": string}}], "notes": string}}.

Check:
- Accuracy: no invented plot not supported by FILM_MAP.
- Coverage: important plot branches are not skipped. Current coverage={coverage_pct:.3f}.
- Names: use glossary consistently.
- Length: total characters should be near {char_budget}; current={estimate_total_chars(beats)}.

GLOSSARY:
{json.dumps(glossary, ensure_ascii=False)}

FILM_MAP:
{film_map_view}

REVIEW_SCRIPT:
{json.dumps(payload, ensure_ascii=False)}
""".strip()


def build_regenerate_prompt(
    *,
    beat: ReviewBeat,
    issue: str,
    glossary: list[dict],
    char_target: int,
) -> str:
    return f"""
Regenerate only this one Vietnamese recap beat.
Return ONLY JSON: {{"beat_id": {beat.beat_id}, "narration": string}}.

Rules:
- Fix this issue: {issue}
- Keep same segment span: {beat.from_seg_id}-{beat.to_seg_id}.
- Do not quote original dialogue verbatim.
- Use glossary names consistently.
- Aim for about {char_target} Vietnamese characters.

CURRENT_BEAT:
{json.dumps(beat.model_dump(), ensure_ascii=False)}

GLOSSARY:
{json.dumps(glossary, ensure_ascii=False)}
""".strip()


async def request_outline(
    client: ChatClient,
    *,
    film_map_view: str,
    target_video_s: float,
    char_budget: int,
    min_coverage: float,
    style_sample: str = "",
) -> OutlineResult:
    response = await client.ask(
        build_outline_prompt(
            film_map_view=film_map_view,
            target_video_s=target_video_s,
            char_budget=char_budget,
            min_coverage=min_coverage,
            style_sample=style_sample,
        )
    )
    return normalize_outline(OutlineResult.model_validate(extract_json(response)))


async def request_narration(
    client: ChatClient,
    *,
    outline: list[OutlineBeat],
    glossary: list[dict],
    char_targets: list[int],
    style_sample: str = "",
) -> list[NarrationBeat]:
    response = await client.ask(
        build_narration_prompt(
            outline=outline,
            glossary=glossary,
            char_targets=char_targets,
            style_sample=style_sample,
        )
    )
    data = extract_json(response)
    return [NarrationBeat.model_validate(item) for item in data]


async def request_qa(
    client: ChatClient,
    *,
    film_map_view: str,
    beats: list[ReviewBeat],
    glossary: list[dict],
    char_budget: int,
    coverage_pct: float,
) -> QaResult:
    response = await client.ask(
        build_qa_prompt(
            film_map_view=film_map_view,
            beats=beats,
            glossary=glossary,
            char_budget=char_budget,
            coverage_pct=coverage_pct,
        )
    )
    return QaResult.model_validate(extract_json(response))


async def regenerate_beat(
    client: ChatClient,
    *,
    beat: ReviewBeat,
    issue: str,
    glossary: list[dict],
    char_target: int,
) -> NarrationBeat:
    response = await client.ask(
        build_regenerate_prompt(beat=beat, issue=issue, glossary=glossary, char_target=char_target)
    )
    return NarrationBeat.model_validate(extract_json(response))


def normalize_outline(outline_result: OutlineResult) -> OutlineResult:
    hook_ids = set(outline_result.hook)
    normalized: list[OutlineBeat] = []
    hook_added = False
    for beat in outline_result.outline:
        is_hook = beat.is_hook or (not hook_added and (beat.from_seg_id in hook_ids or beat.to_seg_id in hook_ids))
        if is_hook and not hook_added:
            normalized.insert(0, beat.model_copy(update={"is_hook": True}))
            hook_added = True
        elif not is_hook:
            normalized.append(beat.model_copy(update={"is_hook": False}))
    if not hook_added and outline_result.outline:
        first = outline_result.outline[0].model_copy(update={"is_hook": True})
        normalized.insert(0, first)
    return OutlineResult(glossary=outline_result.glossary, outline=normalized, hook=outline_result.hook)

