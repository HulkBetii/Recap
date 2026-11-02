"""Per-sentence source references (G2 sidecar `review_script.sentences.json`).

After the narration is final, each beat's narration is split into sentences and the LLM names, for every
sentence, the film-map segment ids it tells (or null for commentary). G5 then shows each sentence over
footage from its own segments instead of spreading the beat's whole span linearly over its audio.
As everywhere in G2, the LLM only returns segment ids; code clamps them to the beat span and derives
timecodes from film_map.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from typing import Any

from common.schema import FilmMapSegment, ReviewBeat, SentenceRef
from common.text import split_sentences
from review.json_utils import extract_json
from review.llm_flow import ChatClient, build_source_view, plan_narration_batches

SENTENCE_REFS_VERSION = "sentence-refs-v1"
# Narration characters per request; each beat also carries its source lines, so requests stay ~20-25k chars.
SENTENCE_REF_BATCH_CHARS = 8000


def build_sentence_ref_prompt(beats: Sequence[ReviewBeat], film_map: Sequence[FilmMapSegment]) -> str:
    payload = [
        {
            "beat_id": beat.beat_id,
            "sentences": [{"idx": index, "text": sentence} for index, sentence in enumerate(split_sentences(beat.narration))],
            "source": build_source_view(film_map, [(beat.from_seg_id, beat.to_seg_id)]),
        }
        for beat in beats
    ]
    return f"""
Link every narration sentence of a Vietnamese movie recap to the film moment it tells.
For each beat you get its numbered SENTENCES and SOURCE: the film-map lines ("#id text") of the beat's span.
For every sentence return seg_ids = [from_id, to_id]: the NARROWEST inclusive range of SOURCE ids that shows
the event, action or people the sentence describes. Use only ids listed in that beat's SOURCE.
Return null when the sentence is commentary with no concrete moment (opinion, summary, rhetorical question).
Ranges normally follow the sentence order, but a sentence that recalls an earlier moment points back to it.

Return ONLY JSON: [{{"beat_id": number, "sentences": [{{"idx": number, "seg_ids": [number, number] or null}}]}}].

BEATS:
{json.dumps(payload, ensure_ascii=False)}
""".strip()


def _seg_pair(value: Any) -> tuple[int, int] | None:
    if isinstance(value, int):
        return value, value
    if isinstance(value, list) and value and all(isinstance(item, int) for item in value):
        return min(value), max(value)
    return None


def resolve_sentence_refs(
    beats: Sequence[ReviewBeat],
    film_map: Sequence[FilmMapSegment],
    answers: dict[int, dict[int, Any]],
) -> tuple[list[SentenceRef], list[str]]:
    """Turn raw LLM answers ({beat_id: {idx: seg_ids}}) into validated refs clamped to each beat's span."""
    by_id = {segment.id: segment for segment in film_map}
    refs: list[SentenceRef] = []
    warnings: list[str] = []
    for beat in beats:
        beat_answers = answers.get(beat.beat_id, {})
        for index, sentence in enumerate(split_sentences(beat.narration)):
            pair = _seg_pair(beat_answers.get(index))
            if pair is not None:
                start, end = max(pair[0], beat.from_seg_id), min(pair[1], beat.to_seg_id)
                if start > end or start not in by_id or end not in by_id:
                    warnings.append(f"beat {beat.beat_id} sentence {index}: seg_ids {list(pair)} outside the beat span")
                    pair = None
                else:
                    pair = (start, end)
            if pair is None:
                refs.append(SentenceRef(beat_id=beat.beat_id, idx=index, text=sentence))
                continue
            refs.append(
                SentenceRef(
                    beat_id=beat.beat_id,
                    idx=index,
                    text=sentence,
                    from_seg_id=pair[0],
                    to_seg_id=pair[1],
                    src_tc_start=by_id[pair[0]].tc_start,
                    src_tc_end=by_id[pair[1]].tc_end,
                    grounded=True,
                )
            )
    return refs, warnings


def parse_sentence_ref_answers(data: Any) -> dict[int, dict[int, Any]]:
    answers: dict[int, dict[int, Any]] = {}
    if not isinstance(data, list):
        raise ValueError("sentence refs response must be a JSON array")
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("beat_id"), int) or not isinstance(item.get("sentences"), list):
            continue
        answers[item["beat_id"]] = {
            sentence["idx"]: sentence.get("seg_ids")
            for sentence in item["sentences"]
            if isinstance(sentence, dict) and isinstance(sentence.get("idx"), int)
        }
    return answers


def sentence_refs_key(beats: Sequence[ReviewBeat], film_map_hash: str, model: str) -> str:
    payload = [(beat.beat_id, beat.narration, beat.from_seg_id, beat.to_seg_id) for beat in beats]
    raw = json.dumps([SENTENCE_REFS_VERSION, model, film_map_hash, payload], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def request_sentence_refs(
    client: ChatClient,
    *,
    beats: Sequence[ReviewBeat],
    film_map: Sequence[FilmMapSegment],
    concurrency: int = 1,
    errors: tuple[type[BaseException], ...] = (ValueError,),
    logger: logging.Logger | None = None,
) -> tuple[list[SentenceRef], list[str], bool]:
    """Return refs, warnings and whether every batch succeeded (only complete results should be cached).

    A failed batch leaves its sentences ungrounded so G5 falls back to beat-level matching for them.
    """
    log = logger or logging.getLogger("review")
    batches = plan_narration_batches([len(beat.narration) for beat in beats], SENTENCE_REF_BATCH_CHARS)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    warnings: list[str] = []

    async def run(number: int, batch: list[int]) -> dict[int, dict[int, Any]]:
        async with semaphore:
            log.info("[4/4] Requesting sentence refs batch %d/%d (beats %d-%d)", number, len(batches), batch[0], batch[-1])
            try:
                response = await client.ask(build_sentence_ref_prompt([beats[index] for index in batch], film_map))
                return parse_sentence_ref_answers(extract_json(response))
            except errors as exc:
                warnings.append(f"sentence refs batch {number}/{len(batches)} failed: {exc}")
                log.warning(warnings[-1])
                return {}

    results = await asyncio.gather(*(run(number, batch) for number, batch in enumerate(batches, start=1)))
    complete = len(warnings) == 0
    answers: dict[int, dict[int, Any]] = {}
    for result in results:
        answers.update(result)
    refs, resolve_warnings = resolve_sentence_refs(beats, film_map, answers)
    return refs, [*warnings, *resolve_warnings], complete
