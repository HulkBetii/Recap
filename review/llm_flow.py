from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Sequence
from typing import Protocol

from common.schema import AnimeContext, EpisodeMemory, FilmMapSegment, ReviewBeat
from common.source_view import SOURCE_SEGMENT_CHARS, build_source_view, segment_text  # noqa: F401 - re-exported for review modules
from review.budget import estimate_total_chars
from review.json_utils import extract_json
from review.models import NarrationBeat, OutlineBeat, OutlineResult, QaResult

NARRATION_FIELDS = ("beat_id", "narration")
# 9router rejects very large responses (502 response_too_large); ~8k Vietnamese chars per request stays well below it.
NARRATION_BATCH_CHARS = 8000
# Models write ~700-1,350 characters per beat regardless of the target, so long films need more,
# shorter beats; ~700 chars is ~35 s of CapCut narration.
MAX_CHARS_PER_BEAT = 700


def min_outline_beats(char_budget: int) -> int:
    return max(1, math.ceil(char_budget / MAX_CHARS_PER_BEAT))

MOVIE_CONTENT_TYPES = {"movie", "anime_movie"}
# Segments shown on each side of a beat so a rewrite can see what belongs to the neighbouring beats.
SOURCE_CONTEXT_SEGMENTS = 3
OPENING_REWRITE_RULE = (
    "- This is the opening beat of a movie recap: clearly establish who the protagonist is, where the scene takes place, "
    "the problem they face and why it matters, using only facts from SOURCE; no later twist and no hype without context."
)


class ChatClient(Protocol):
    async def ask(self, prompt: str) -> str:
        ...

def build_anime_context_block(anime_context: AnimeContext | None, *, content_type: str) -> str:
    if anime_context is None:
        return ""
    payload = anime_context.model_dump(mode="json", exclude_none=True)
    continuity_rule = (
        "- Series continuity: stay inside this episode and the provided continuity notes; do not add future-episode spoilers."
        if content_type == "anime_series"
        else "- Standalone movie continuity: treat this as a self-contained anime movie unless the context explicitly says otherwise."
    )
    return f"""
ANIME CONTEXT:
{json.dumps(payload, ensure_ascii=False)}

ANIME RULES:
- Use characters.name_vi as the canonical Vietnamese names; aliases/name_original are for recognition only.
- Preserve special terms from terms.term/meaning_vi consistently.
- Treat non_story_ranges as forbidden source zones for story beats, especially opening_theme, ending_theme, next_episode_preview, recap_previous_episode, eyecatch, sponsor_card, title_card, and studio_logo.
- Do not quote original dialogue or theme-song lyrics verbatim; transform everything into commentary/narration.
- Do not build OP/ED/preview/recap-only beats.
- Flag unsupported spoilers, unclear episode continuity, or glossary/name drift during QA.
{continuity_rule}
""".strip()

def build_episode_memory_block(episode_memory: EpisodeMemory | None, *, content_type: str) -> str:
    if episode_memory is None:
        return ""
    payload = episode_memory.model_dump(mode="json", exclude_none=True)
    guidance = "\n".join(f"- {line}" for line in episode_memory.review_guidance) if episode_memory.review_guidance else ""
    current_mode = episode_memory.current.recap_mode
    mode_rule = (
        "- Quick recap mode: focus on continuity-critical changes and the next-episode setup; do not pad to full coverage."
        if current_mode == "quick"
        else "- Full recap mode: cover the episode broadly while still avoiding spoilers beyond the allowed memory window."
    )
    return f"""
EPISODE MEMORY:
{json.dumps(payload, ensure_ascii=False)}

EPISODE MEMORY RULES:
- Trust episode_memory.current as the current episode identity; do not infer episode number from filename or OCR.
- Respect spoiler_limit_episode when reusing prior memories; do not mention future episodes beyond that ceiling.
- Use previous episode memory only for continuity, recurring character names, and unresolved state.
- Keep memory references transformed into commentary; do not quote old dialogue or theme-song lyrics verbatim.
- OCR/title-card/filename hints are sanity checks only.
- content_type={content_type}
{mode_rule}
{guidance}
""".strip()

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
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
) -> str:
    style_block = f"\nSTYLE GUIDE:\n{style_sample}\n" if style_sample else ""
    anime_block = f"\n{build_anime_context_block(anime_context, content_type=content_type)}\n" if anime_context else ""
    memory_block = f"\n{build_episode_memory_block(episode_memory, content_type=content_type)}\n" if episode_memory else ""
    content_label = "anime recap script" if content_type in {"anime_series", "anime_movie"} else "movie recap script"
    if content_type in MOVIE_CONTENT_TYPES and hook_mode == "setup":
        hook_rule = f"- hook: JSON array of integer segment ids from the first story/setup beat at or after story_start_s={story_start_s:.1f}s; do NOT use excluded intro/opening footage or jump to a later twist/ending. Never put narration text in hook."
        pacing_rule = "- Movie mode: prioritize clear premise, characters, cause/effect, and story comprehension over viral shock."
    elif hook_mode == "off":
        hook_rule = "- hook: JSON array of integer segment ids from the first chronological story beat, as a minimal setup hook to satisfy schema; do NOT create cold-open hype. Never put narration text in hook."
        pacing_rule = "- Keep the opening chronological and clear."
    else:
        hook_rule = "- hook: JSON array of integer segment ids for an exciting cold-open, usually from later in the story, without spoiling the ending. Never put narration text in hook."
        pacing_rule = "- Episode mode: fast recap pacing is allowed, but keep plot logic clear."
    return f"""
You are planning a Vietnamese {content_label}.
Return ONLY valid JSON with keys: glossary, outline, hook.

Rules:
- Do NOT output timecodes.
- Use only segment ids from the FILM_MAP.
- If story_start_s is given, do not choose setup/opening source spans before story_start_s.
- glossary: list of characters with Vietnamese canonical name and short role.
- outline: ordered beats covering the full plot with recap pacing. Each beat has from_seg_id, to_seg_id, summary.
- A beat's summary must describe ONLY events whose segments lie inside its own from_seg_id..to_seg_id; every event belongs to exactly one beat. If a summary needs a later event, end that beat later instead of borrowing the event from the next beat.
- Use at least {min_outline_beats(char_budget)} beats so that no beat needs more than about {MAX_CHARS_PER_BEAT} Vietnamese characters; split long stretches of plot into several beats.
- Do NOT create standalone beats for credits, production info, black screen, title cards, or non-story outro; keep the real plot ending, but ignore post-plot credits.
{hook_rule}
{pacing_rule}
- Non-hook outline beats must be chronological and cover at least {min_coverage:.0%} of segment ids.
- Target recap length: about {target_video_s:.1f}s, char budget about {char_budget} Vietnamese characters.
{style_block}
{anime_block}
{memory_block}
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
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
    beat_ids: list[int] | None = None,
    previous_narration: str | None = None,
    film_map: Sequence[FilmMapSegment] | None = None,
) -> str:
    style_block = f"\nSTYLE GUIDE:\n{style_sample}\n" if style_sample else ""
    anime_block = f"\n{build_anime_context_block(anime_context, content_type=content_type)}\n" if anime_context else ""
    memory_block = f"\n{build_episode_memory_block(episode_memory, content_type=content_type)}\n" if episode_memory else ""
    movie_rule = (
        "- Movie mode: write cleaner, more explanatory Vietnamese; reduce jokes/clickbait; make cause/effect and character relations easy to follow."
        if content_type in MOVIE_CONTENT_TYPES
        else "- Dramatic fast-paced Vietnamese recap style."
    )
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
        if beat_ids is None or index in beat_ids
    ]
    source_rule = ""
    if film_map is not None:
        for item in payload:
            item["source"] = build_source_view(film_map, [(item["from_seg_id"], item["to_seg_id"])])
        source_rule = (
            "\n- Each beat carries SOURCE: the FILM_MAP lines of its own segment span. Narrate only events present in that beat's "
            "source (the summary is guidance); never move events from another beat's span into this beat."
        )
    # A batched request (long films) only adds context blocks; the single-request prompt stays byte-identical.
    batch_block = ""
    if beat_ids is not None:
        outline_context = [{"beat_id": index, "summary": beat.summary} for index, beat in enumerate(outline)]
        batch_block = (
            "\nThis is one part of a longer script. Write narration ONLY for the beats listed in BEATS; "
            "use FULL_OUTLINE for continuity and do not narrate other beats.\n"
            f"FULL_OUTLINE:\n{json.dumps(outline_context, ensure_ascii=False)}\n"
        )
        if previous_narration:
            batch_block += f"PREVIOUS_BEAT_NARRATION (continue naturally from it; do not rewrite it):\n{previous_narration}\n"
    return f"""
Write Vietnamese narration for each recap beat.
Return ONLY a JSON array of objects: {{"beat_id": number, "narration": string}}.{batch_block}

Rules:
- Dramatic fast-paced Vietnamese recap style.
{movie_rule}
{hook_rule}
- Use the glossary names consistently.{source_rule}
- Do NOT quote original dialogue verbatim; transform into narration.
- Stay within ±20% of each char_target where possible.
{style_block}
{anime_block}
{memory_block}
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
    content_type: str = "episode",
    hook_mode: str = "cold_open",
    story_start_s: float = 0.0,
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
) -> str:
    payload = [beat.model_dump() for beat in beats]
    opening_rule = (
        f"- Movie opening: beat 0 must clearly establish who/where/problem/why it matters and its source span must start at or after story_start_s={story_start_s:.1f}s; flag confusing cold-open twist, excluded intro footage, or hype without context."
        if content_type in MOVIE_CONTENT_TYPES
        else "- Opening should be engaging and accurate."
    )
    anime_block = f"\n{build_anime_context_block(anime_context, content_type=content_type)}\n" if anime_context else ""
    memory_block = f"\n{build_episode_memory_block(episode_memory, content_type=content_type)}\n" if episode_memory else ""
    return f"""
Review this Vietnamese recap script against the film map.
Return ONLY valid JSON: {{"pass": boolean, "issues": [{{"beat_id": number, "type": string, "suggestion": string}}], "notes": string}}.

Check:
- Accuracy: no invented plot not supported by FILM_MAP.
- Coverage: important plot branches are not skipped. Current coverage={coverage_pct:.3f}.
- Names: use glossary consistently.
- Length: total characters should be near {char_budget}; current={estimate_total_chars(beats)}.
- Non-story credits/outro/production-info beats should be removed, not rewritten longer.
- Anime QA when context is present: reject OP/ED/theme-song/preview/recap-only beats, glossary drift, unsupported future spoilers, non-story source range usage, and unclear episode continuity.
{opening_rule}
{anime_block}
{memory_block}

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
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
    content_type: str = "episode",
    source: str = "",
    opening_rule: str = "",
) -> str:
    style_block = f"\nSTYLE GUIDE:\n{style_sample}\n" if style_sample else ""
    anime_block = f"\n{build_anime_context_block(anime_context, content_type=content_type)}\n" if anime_context else ""
    memory_block = f"\n{build_episode_memory_block(episode_memory, content_type=content_type)}\n" if episode_memory else ""
    extra_rules = ""
    source_block = ""
    if source:
        extra_rules += "\n- Use only facts from SOURCE; lines tagged [other beat] belong to the neighbouring beats and must not be narrated here."
        source_block = f"SOURCE:\n{source}\n\n"
    if opening_rule:
        extra_rules += f"\n{opening_rule}"
    return f"""
Regenerate only this one Vietnamese recap beat.
Return ONLY JSON: {{"beat_id": {beat.beat_id}, "narration": string}}.

Rules:
- Fix this issue: {issue}
- Keep same segment span: {beat.from_seg_id}-{beat.to_seg_id}.{extra_rules}
- Do not quote original dialogue verbatim.
- Use glossary names consistently.
- Aim for about {char_target} Vietnamese characters.
- Keep narration TTS-friendly with natural punctuation and no long run-on sentence.
{style_block}
{anime_block}
{memory_block}
{source_block}CURRENT_BEAT:
{json.dumps({"beat_id": beat.beat_id, "narration": beat.narration}, ensure_ascii=False)}

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
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
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
            story_start_s=story_start_s,
            anime_context=anime_context,
            episode_memory=episode_memory,
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
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
    max_batch_chars: int = NARRATION_BATCH_CHARS,
    load_batch: Callable[[list[int]], list[NarrationBeat] | None] | None = None,
    save_batch: Callable[[list[int], list[NarrationBeat]], None] | None = None,
    film_map: Sequence[FilmMapSegment] | None = None,
) -> list[NarrationBeat]:
    batches = plan_narration_batches(char_targets, max_batch_chars)
    if len(batches) <= 1:
        response = await client.ask(
            build_narration_prompt(
                outline=outline,
                glossary=glossary,
                char_targets=char_targets,
                style_sample=style_sample,
                content_type=content_type,
                hook_mode=hook_mode,
                anime_context=anime_context,
                episode_memory=episode_memory,
                film_map=film_map,
            )
        )
        data = extract_json(response)
        return [NarrationBeat.model_validate(narration_fields(item)) for item in data]
    logger = logging.getLogger("review")
    narration: list[NarrationBeat] = []
    previous: str | None = None
    for number, batch in enumerate(batches, start=1):
        cached = load_batch(batch) if load_batch else None
        if cached is not None:
            logger.info("[2/4] Using cached narration batch %d/%d (beats %d-%d)", number, len(batches), batch[0], batch[-1])
            beats = cached
        else:
            logger.info("[2/4] Requesting narration batch %d/%d (beats %d-%d)", number, len(batches), batch[0], batch[-1])
            response = await client.ask(
                build_narration_prompt(
                    outline=outline,
                    glossary=glossary,
                    char_targets=char_targets,
                    style_sample=style_sample,
                    content_type=content_type,
                    hook_mode=hook_mode,
                    anime_context=anime_context,
                    episode_memory=episode_memory,
                    beat_ids=batch,
                    previous_narration=previous,
                    film_map=film_map,
                )
            )
            by_id = {item.beat_id: item for item in (NarrationBeat.model_validate(narration_fields(raw)) for raw in extract_json(response))}
            missing = [beat_id for beat_id in batch if beat_id not in by_id]
            if missing:
                raise ValueError(f"narration batch {number}/{len(batches)} is missing beat_ids {missing}")
            beats = [by_id[beat_id] for beat_id in batch]
            if save_batch:
                save_batch(batch, beats)
        narration.extend(beats)
        previous = beats[-1].narration
    return narration


def plan_narration_batches(char_targets: list[int], max_batch_chars: int) -> list[list[int]]:
    """Group consecutive beats so each JSON response stays under gateway response-size limits."""
    batches: list[list[int]] = []
    current: list[int] = []
    total = 0
    for beat_id, target in enumerate(char_targets):
        if current and total + target > max_batch_chars:
            batches.append(current)
            current, total = [], 0
        current.append(beat_id)
        total += target
    if current:
        batches.append(current)
    return batches

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
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
    focus_beat_ids: Sequence[int] | None = None,
    film_map: Sequence[FilmMapSegment] | None = None,
) -> QaResult:
    if focus_beat_ids is not None and film_map is not None:
        return await request_focus_qa(
            client,
            film_map=film_map,
            beats=beats,
            focus_beat_ids=focus_beat_ids,
            glossary=glossary,
            content_type=content_type,
            story_start_s=story_start_s,
            anime_context=anime_context,
            episode_memory=episode_memory,
        )
    response = await client.ask(
        build_qa_prompt(
            film_map_view=film_map_view,
            beats=beats,
            glossary=glossary,
            char_budget=char_budget,
            coverage_pct=coverage_pct,
            content_type=content_type,
            hook_mode=hook_mode,
            story_start_s=story_start_s,
            anime_context=anime_context,
            episode_memory=episode_memory,
        )
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

def build_focus_qa_prompt(
    *,
    source_view: str,
    beats: list[ReviewBeat],
    focus_beat_ids: Sequence[int],
    glossary: list[dict],
    content_type: str = "episode",
    story_start_s: float = 0.0,
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
) -> str:
    focus = set(focus_beat_ids)
    payload = [beat.model_dump() | {"focus": beat.beat_id in focus} for beat in beats]
    opening_rule = (
        f"- Beat 0 is the movie opening: it must clearly establish who/where/problem/why it matters, with its source span at or after story_start_s={story_start_s:.1f}s."
        if 0 in focus and content_type in MOVIE_CONTENT_TYPES
        else ""
    )
    anime_block = f"\n{build_anime_context_block(anime_context, content_type=content_type)}\n" if anime_context else ""
    memory_block = f"\n{build_episode_memory_block(episode_memory, content_type=content_type)}\n" if episode_memory else ""
    return f"""
Review this Vietnamese recap script again: only the beats marked "focus": true were just rewritten.
Return ONLY valid JSON: {{"pass": boolean, "issues": [{{"beat_id": number, "type": string, "suggestion": string}}], "notes": string}}.
Report issues for focus beats only; the other beats are read-only context. Do not nitpick wording or style.

Check each focus beat against FILM_MAP:
- Accuracy: no plot, role, relationship or outcome that FILM_MAP does not support.
- Span: it narrates only events inside its own from_seg_id..to_seg_id, not events of the neighbouring beats.
- Names: use glossary consistently.
- Flow: it follows on from the previous beat and leads into the next one.
{opening_rule}
{anime_block}
{memory_block}

GLOSSARY:
{json.dumps(glossary, ensure_ascii=False)}

FILM_MAP (only the spans of these beats; [other beat] lines lie just outside them):
{source_view}

REVIEW_SCRIPT:
{json.dumps(payload, ensure_ascii=False)}
""".strip()


async def request_focus_qa(
    client: ChatClient,
    *,
    film_map: Sequence[FilmMapSegment],
    beats: list[ReviewBeat],
    focus_beat_ids: Sequence[int],
    glossary: list[dict],
    content_type: str = "episode",
    story_start_s: float = 0.0,
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
) -> QaResult:
    """Re-check only rewritten beats (plus one read-only neighbour each side) against their own source spans.

    A full-script QA pass resamples nitpicks across every beat, so repeating it after each rewrite never converges.
    """
    focus = sorted({beat_id for beat_id in focus_beat_ids if 0 <= beat_id < len(beats)})
    if not focus:
        return QaResult.model_validate({"pass": True, "issues": [], "notes": "no beats to re-check"})
    context_ids = sorted({neighbour for beat_id in focus for neighbour in (beat_id - 1, beat_id, beat_id + 1) if 0 <= neighbour < len(beats)})
    context_beats = [beats[beat_id] for beat_id in context_ids]
    source_view = build_source_view(
        film_map,
        [(beat.from_seg_id, beat.to_seg_id) for beat in context_beats],
        context=SOURCE_CONTEXT_SEGMENTS,
    )
    response = await client.ask(
        build_focus_qa_prompt(
            source_view=source_view,
            beats=context_beats,
            focus_beat_ids=focus,
            glossary=glossary,
            content_type=content_type,
            story_start_s=story_start_s,
            anime_context=anime_context,
            episode_memory=episode_memory,
        )
    )
    data = extract_json(response)
    if isinstance(data, dict) and isinstance(data.get("issues"), list):
        data["issues"] = [
            issue for issue in data["issues"] if isinstance(issue, dict) and isinstance(issue.get("beat_id"), int) and issue["beat_id"] in focus
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
    anime_context: AnimeContext | None = None,
    episode_memory: EpisodeMemory | None = None,
    content_type: str = "episode",
    film_map: Sequence[FilmMapSegment] | None = None,
) -> NarrationBeat:
    source = (
        build_source_view(film_map, [(beat.from_seg_id, beat.to_seg_id)], context=SOURCE_CONTEXT_SEGMENTS) if film_map is not None else ""
    )
    opening_rule = OPENING_REWRITE_RULE if beat.beat_id == 0 and content_type in MOVIE_CONTENT_TYPES else ""
    response = await client.ask(
        build_regenerate_prompt(
            beat=beat,
            issue=issue,
            glossary=glossary,
            char_target=char_target,
            style_sample=style_sample,
            anime_context=anime_context,
            episode_memory=episode_memory,
            content_type=content_type,
            source=source,
            opening_rule=opening_rule,
        )
    )
    revised = NarrationBeat.model_validate(narration_fields(extract_json(response)))
    if revised.beat_id != beat.beat_id:
        logging.getLogger("review").warning("regenerated beat returned beat_id=%d; keeping requested beat_id=%d", revised.beat_id, beat.beat_id)
        revised = revised.model_copy(update={"beat_id": beat.beat_id})
    return revised

def narration_fields(item: object) -> object:
    """Keep only beat_id/narration: models sometimes echo spans or timecodes, which code derives itself."""
    if isinstance(item, dict):
        return {key: item[key] for key in NARRATION_FIELDS if key in item}
    return item

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
    elif isinstance(hook, str):
        # Some models put hook narration text here; normalize_outline then promotes the first beat.
        logging.getLogger("review").warning("outline hook was text instead of segment ids; using the first outline beat as hook")
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
