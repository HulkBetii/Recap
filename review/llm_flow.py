from __future__ import annotations

import json
from typing import Protocol

from pydantic import ValidationError

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
    content_type: str = "episode",
    hook_mode: str = "cold_open",
    story_start_s: float = 0.0,
) -> str:
    style_block = f"\nSTYLE GUIDE:\n{style_sample}\n" if style_sample else ""
    if content_type == "movie" and hook_mode == "setup":
        hook_rule = f"- hook: use the first story/setup beat at or after story_start_s={story_start_s:.1f}s; do NOT use excluded intro/opening footage or jump to a later twist/ending."
        pacing_rule = "- Movie mode: prioritize clear premise, characters, cause/effect, and story comprehension over viral shock."
    elif hook_mode == "off":
        hook_rule = "- hook: use the first chronological story beat as a minimal setup hook to satisfy schema; do NOT create cold-open hype."
        pacing_rule = "- Keep the opening chronological and clear."
    else:
        hook_rule = "- hook: list of exciting segment ids for a cold-open, usually from later in the story, without spoiling the ending."
        pacing_rule = "- Episode mode: fast recap pacing is allowed, but keep plot logic clear."
    return f"""
You are planning a Vietnamese movie recap script.
Return ONLY valid JSON with keys: glossary, outline, hook.

Rules:
- Do NOT output timecodes.
- Use only segment ids from the FILM_MAP.
- If story_start_s is given, do not choose setup/opening source spans before story_start_s.
- glossary: list of characters with Vietnamese canonical name and short role.
- outline: ordered beats covering the full plot with recap pacing. Each beat has from_seg_id, to_seg_id, summary.
- Do NOT create standalone beats for credits, production info, black screen, title cards, or non-story outro; keep the real plot ending, but ignore post-plot credits.
{hook_rule}
{pacing_rule}
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
    content_type: str = "episode",
    hook_mode: str = "cold_open",
    story_start_s: float = 0.0,
) -> str:
    style_block = f"\nSTYLE GUIDE:\n{style_sample}\n" if style_sample else ""
    movie_rule = "- Movie mode: write cleaner, more explanatory Vietnamese; reduce jokes/clickbait; make cause/effect and character relations easy to follow." if content_type == "movie" else "- Dramatic fast-paced Vietnamese recap style."
    hook_rule = "- Setup hook must explain the initial premise from the start of the film, not a later twist." if hook_mode == "setup" else "- Hook beat must be gripping and placed first."
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
{movie_rule}
- Use the glossary names consistently.
- Do NOT quote original dialogue verbatim; transform into narration.
- Stay within ±20% of each char_target where possible.
{hook_rule}
{style_block}
GLOSSARY:
{json.dumps(glossary, ensure_ascii=False)}

BEATS:
{json.dumps(payload, ensure_ascii=False)}
""".strip()

def validate_narration_payload(data: object, *, expected_count: int) -> list[NarrationBeat]:
    if not isinstance(data, list):
        raise ValueError("narration response must be a JSON array")
    beats = [NarrationBeat.model_validate(item) for item in data]
    expected_ids = set(range(expected_count))
    actual_ids = [beat.beat_id for beat in beats]
    if len(beats) != expected_count:
        raise ValueError(f"narration response has {len(beats)} beat(s), expected {expected_count}")
    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError("narration response has duplicate beat_id values")
    if set(actual_ids) != expected_ids:
        missing = sorted(expected_ids.difference(actual_ids))
        extra = sorted(set(actual_ids).difference(expected_ids))
        raise ValueError(f"narration beat_id mismatch; missing={missing[:10]} extra={extra[:10]}")
    return sorted(beats, key=lambda beat: beat.beat_id)


def build_qa_prompt(
    *,
    film_map_view: str,
    beats: list[ReviewBeat],
    glossary: list[dict],
    char_budget: int,
    coverage_pct: float,
    content_type: str = "episode",
    hook_mode: str = "cold_open",
    story_start_s: float = 0.0,
) -> str:
    payload = [beat.model_dump() for beat in beats]
    opening_rule = f"- Movie opening: beat 0 must clearly establish who/where/problem/why it matters and its source span must start at or after story_start_s={story_start_s:.1f}s; flag confusing cold-open twist, excluded intro footage, or hype without context." if content_type == "movie" else "- Opening should be engaging and accurate."
    return f"""
Review this Vietnamese recap script against the film map.
Return ONLY valid JSON: {{"pass": boolean, "issues": [{{"beat_id": number, "type": string, "suggestion": string}}], "notes": string}}.

Check:
- Accuracy: no invented plot not supported by FILM_MAP.
- Coverage: important plot branches are not skipped. Current coverage={coverage_pct:.3f}.
- Names: use glossary consistently.
- Length: total characters should be near {char_budget}; current={estimate_total_chars(beats)}.
- Non-story credits/outro/production-info beats should be removed, not rewritten longer.
{opening_rule}

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
    style_sample: str = "",
) -> str:
    style_block = f"\nSTYLE GUIDE:\n{style_sample}\n" if style_sample else ""
    return f"""
Regenerate only this one Vietnamese recap beat.
Return ONLY JSON: {{"beat_id": {beat.beat_id}, "narration": string}}.

Rules:
- Fix this issue: {issue}
- Keep same segment span: {beat.from_seg_id}-{beat.to_seg_id}.
- Do not quote original dialogue verbatim.
- Use glossary names consistently.
- Aim for about {char_target} Vietnamese characters.
- Keep narration TTS-friendly with natural punctuation and no long run-on sentence.
{style_block}
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
    content_type: str = "episode",
    hook_mode: str = "cold_open",
    story_start_s: float = 0.0,
) -> OutlineResult:
    response = await client.ask(
        build_outline_prompt(
            film_map_view=film_map_view,
            target_video_s=target_video_s,
            char_budget=char_budget,
            min_coverage=min_coverage,
            style_sample=style_sample,
            content_type=content_type,
            hook_mode=hook_mode,
        )
    )
    return normalize_outline(OutlineResult.model_validate(normalize_outline_payload(extract_json(response))))


async def request_narration(
    client: ChatClient,
    *,
    outline: list[OutlineBeat],
    glossary: list[dict],
    char_targets: list[int],
    style_sample: str = "",
    content_type: str = "episode",
    hook_mode: str = "cold_open",
) -> list[NarrationBeat]:
    base_prompt = build_narration_prompt(
        outline=outline,
        glossary=glossary,
        char_targets=char_targets,
        style_sample=style_sample,
        content_type=content_type,
        hook_mode=hook_mode,
    )
    last_error: Exception | None = None
    for attempt in range(2):
        prompt = base_prompt
        if attempt:
            prompt += (
                "\n\nYour previous answer was invalid: "
                f"{last_error}. Return ONLY the complete JSON array, exactly one object "
                f"for each beat_id 0..{len(outline) - 1}. Do not use placeholders like \"...\"."
            )
        response = await client.ask(prompt)
        try:
            data = extract_json(response)
            return validate_narration_payload(data, expected_count=len(outline))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                continue
            raise
    raise ValueError("narration request failed")


async def request_qa(
    client: ChatClient,
    *,
    film_map_view: str,
    beats: list[ReviewBeat],
    glossary: list[dict],
    char_budget: int,
    coverage_pct: float,
    content_type: str = "episode",
    hook_mode: str = "cold_open",
    story_start_s: float = 0.0,
) -> QaResult:
    prompt = build_qa_prompt(
        film_map_view=film_map_view,
        beats=beats,
        glossary=glossary,
        char_budget=char_budget,
        coverage_pct=coverage_pct,
        content_type=content_type,
        hook_mode=hook_mode,
        story_start_s=story_start_s,
    )
    response = await client.ask(prompt)
    try:
        data = extract_json(response)
    except json.JSONDecodeError:
        response = await client.ask(
            prompt
            + "\n\nYour previous answer was not valid JSON. "
            + "Return ONLY the JSON object with keys pass, issues, notes. No prose, no markdown."
        )
        data = extract_json(response)
    if isinstance(data, dict) and isinstance(data.get("issues"), list):
        max_beat_id = len(beats) - 1
        data["issues"] = [
            issue
            for issue in data["issues"]
            if isinstance(issue, dict)
            and isinstance(issue.get("beat_id"), int)
            and 0 <= issue["beat_id"] <= max_beat_id
        ]
    return QaResult.model_validate(data)


async def regenerate_beat(
    client: ChatClient,
    *,
    beat: ReviewBeat,
    issue: str,
    glossary: list[dict],
    char_target: int,
    style_sample: str = "",
) -> NarrationBeat:
    response = await client.ask(
        build_regenerate_prompt(beat=beat, issue=issue, glossary=glossary, char_target=char_target, style_sample=style_sample)
    )
    return NarrationBeat.model_validate(extract_json(response))


def normalize_outline_payload(data: object) -> object:
    if not isinstance(data, dict):
        return data
    hook = data.get("hook")
    if isinstance(hook, dict):
        hook_ids = []
        for key in ("segment_ids", "ids"):
            value = hook.get(key)
            if isinstance(value, list):
                hook_ids.extend(item for item in value if isinstance(item, int))
        for key in ("from_seg_id", "to_seg_id", "seg_id", "id"):
            value = hook.get(key)
            if isinstance(value, int):
                hook_ids.append(value)
        data = {**data, "hook": sorted(set(hook_ids))}
    elif hook is None:
        data = {**data, "hook": []}
    return data


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
