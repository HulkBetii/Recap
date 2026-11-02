"""Per-sentence sources of a season script (`python -m series_composer.sentence_refs`).

After the season narration is final, every beat's narration is split into sentences and the LLM names, for each
sentence, which of the beat's source refs (episode + event) it tells and the narrowest film_map segment range
inside that ref. Segment ids repeat across episodes, so the answer carries the ref index. series_match then
plays each sentence over footage from its own episode moment. Runs as its own cached step in series_recap so
changing it never re-runs the expensive composer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from common.llm_gateway import DEFAULT_GATEWAY_MODEL, GatewayChatClient, GatewayChatError
from common.schema import FilmMapSegment, SeriesReviewBeat, SeriesSentenceRef, write_json
from common.source_view import build_source_view
from common.text import split_sentences

SERIES_SENTENCE_REFS_VERSION = "series-sentence-refs-v1"
DEFAULT_MODEL = "ag/gemini-3.8-flash-low"
BATCH_CHARS = 8000
ATTEMPTS = 2

PROMPT = """Link every narration sentence of a Vietnamese anime season recap to the moment it tells.
Each beat lists its numbered SENTENCES and one or more SOURCES (R0, R1, ...). Each source is a moment of one
episode, shown as its film-map lines ("#id text"); ids repeat across episodes, so always say which source.
For every sentence return ref (the source index) and seg_ids = [from_id, to_id]: the NARROWEST inclusive range of
that source's ids that shows the event, action or people the sentence describes.
Return ref and seg_ids as null when the sentence is commentary with no concrete moment.

Return ONLY JSON: [{{"beat_id": number, "sentences": [{{"idx": number, "ref": number or null, "seg_ids": [number, number] or null}}]}}].

BEATS:
{beats}
"""


class SeriesSentenceRefError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Link season narration sentences to episode film-map segments")
    parser.add_argument("--series-review-script", required=True, type=Path)
    parser.add_argument("--episode-run-dir", action="append", default=[], help="episode_key=path (repeatable)")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--llm-model", default=DEFAULT_MODEL)
    parser.add_argument("--llm-fallback-model", default=DEFAULT_GATEWAY_MODEL)
    parser.add_argument("--llm-base-url", default=None)
    parser.add_argument("--reply-timeout-s", default=600, type=int)
    parser.add_argument("--concurrency", default=3, type=int)
    parser.add_argument("--work-dir", default=Path("work/series_sentence_refs"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def parse_run_dirs(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        key, separator, raw = value.partition("=")
        if not separator or not key.strip():
            raise SeriesSentenceRefError("--episode-run-dir must use episode_key=path")
        result[key.strip()] = Path(raw).expanduser().resolve()
    return result


def load_film_maps(run_dirs: dict[str, Path]) -> dict[str, list[FilmMapSegment]]:
    film_maps: dict[str, list[FilmMapSegment]] = {}
    for key, run_dir in run_dirs.items():
        path = run_dir / "film_map.json"
        if not path.is_file():
            raise SeriesSentenceRefError(f"missing film_map.json for episode {key}: {path}")
        film_maps[key] = [FilmMapSegment.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]
    return film_maps


def build_prompt(beats: Sequence[SeriesReviewBeat], film_maps: dict[str, list[FilmMapSegment]]) -> str:
    payload = [
        {
            "beat_id": beat.beat_id,
            "sentences": [{"idx": index, "text": sentence} for index, sentence in enumerate(split_sentences(beat.narration))],
            "sources": [
                {
                    "ref": position,
                    "episode": ref.episode_key,
                    "lines": build_source_view(film_maps.get(ref.episode_key, []), [(ref.from_seg_id, ref.to_seg_id)]),
                }
                for position, ref in enumerate(beat.source_refs)
            ],
        }
        for beat in beats
    ]
    return PROMPT.format(beats=json.dumps(payload, ensure_ascii=False))


def parse_answers(response: str) -> dict[int, dict[int, tuple[Any, Any]]]:
    match = re.search(r"\[.*\]", response, re.S)
    if not match:
        raise ValueError("no JSON array in the sentence refs response")
    answers: dict[int, dict[int, tuple[Any, Any]]] = {}
    for item in json.loads(match.group(0)):
        if isinstance(item, dict) and isinstance(item.get("beat_id"), int) and isinstance(item.get("sentences"), list):
            answers[item["beat_id"]] = {
                sentence["idx"]: (sentence.get("ref"), sentence.get("seg_ids"))
                for sentence in item["sentences"]
                if isinstance(sentence, dict) and isinstance(sentence.get("idx"), int)
            }
    return answers


def resolve_refs(
    beats: Sequence[SeriesReviewBeat],
    film_maps: dict[str, list[FilmMapSegment]],
    answers: dict[int, dict[int, tuple[Any, Any]]],
) -> tuple[list[SeriesSentenceRef], list[str]]:
    """Validate answers: the ref must exist and the segments are clamped to that ref's span."""
    refs: list[SeriesSentenceRef] = []
    warnings: list[str] = []
    by_episode = {key: {segment.id: segment for segment in film_map} for key, film_map in film_maps.items()}
    for beat in beats:
        for index, sentence in enumerate(split_sentences(beat.narration)):
            ref_index, seg_ids = answers.get(beat.beat_id, {}).get(index, (None, None))
            resolved: SeriesSentenceRef | None = None
            if isinstance(ref_index, int) and 0 <= ref_index < len(beat.source_refs) and isinstance(seg_ids, list) and seg_ids and all(isinstance(value, int) for value in seg_ids):
                source = beat.source_refs[ref_index]
                start, end = max(min(seg_ids), source.from_seg_id), min(max(seg_ids), source.to_seg_id)
                segments = by_episode.get(source.episode_key, {})
                if start <= end and start in segments and end in segments:
                    resolved = SeriesSentenceRef(
                        beat_id=beat.beat_id,
                        idx=index,
                        text=sentence,
                        ref_index=ref_index,
                        event_id=source.event_id,
                        episode_key=source.episode_key,
                        from_seg_id=start,
                        to_seg_id=end,
                        src_tc_start=segments[start].tc_start,
                        src_tc_end=segments[end].tc_end,
                        grounded=True,
                    )
                else:
                    warnings.append(f"beat {beat.beat_id} sentence {index}: seg_ids {seg_ids} outside ref {ref_index}")
            refs.append(resolved or SeriesSentenceRef(beat_id=beat.beat_id, idx=index, text=sentence))
    return refs, warnings


def plan_batches(beats: Sequence[SeriesReviewBeat]) -> list[list[SeriesReviewBeat]]:
    batches: list[list[SeriesReviewBeat]] = []
    current: list[SeriesReviewBeat] = []
    size = 0
    for beat in beats:
        if current and size + len(beat.narration) > BATCH_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(beat)
        size += len(beat.narration)
    if current:
        batches.append(current)
    return batches


async def request_refs(
    client: Any,
    beats: Sequence[SeriesReviewBeat],
    film_maps: dict[str, list[FilmMapSegment]],
    *,
    concurrency: int,
    logger: logging.Logger,
) -> tuple[list[SeriesSentenceRef], list[str], bool]:
    batches = plan_batches(beats)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    warnings: list[str] = []

    async def run(number: int, batch: list[SeriesReviewBeat]) -> dict[int, dict[int, tuple[Any, Any]]]:
        async with semaphore:
            failure: Exception | None = None
            for attempt in range(1, ATTEMPTS + 1):
                logger.info("Requesting season sentence refs batch %d/%d (attempt %d)", number, len(batches), attempt)
                try:
                    return parse_answers(await client.ask(build_prompt(batch, film_maps)))
                except (GatewayChatError, ValueError) as exc:
                    failure = exc
            warnings.append(f"season sentence refs batch {number}/{len(batches)} failed: {failure}")
            return {}

    answers: dict[int, dict[int, tuple[Any, Any]]] = {}
    for result in await asyncio.gather(*(run(number, batch) for number, batch in enumerate(batches, start=1))):
        answers.update(result)
    complete = not warnings
    refs, resolve_warnings = resolve_refs(beats, film_maps, answers)
    return refs, [*warnings, *resolve_warnings], complete


def cache_key(script_path: Path, film_maps_paths: list[Path], model: str) -> str:
    digest = hashlib.sha256(json.dumps([SERIES_SENTENCE_REFS_VERSION, model]).encode("utf-8"))
    for path in [script_path, *film_maps_paths]:
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def run(args: argparse.Namespace, client: Any | None = None) -> list[SeriesSentenceRef]:
    logger = logging.getLogger("series_sentence_refs")
    script_path = args.series_review_script.expanduser().resolve()
    beats = [SeriesReviewBeat.model_validate(item) for item in json.loads(script_path.read_text(encoding="utf-8"))]
    run_dirs = parse_run_dirs(args.episode_run_dir)
    film_maps = load_film_maps(run_dirs)
    work_dir = args.work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(script_path, [run_dirs[name] / "film_map.json" for name in sorted(run_dirs)], args.llm_model)
    cache_path = work_dir / "sentence_refs.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() and not args.force else None
    if isinstance(cached, dict) and cached.get("key") == key:
        refs = [SeriesSentenceRef.model_validate(item) for item in cached["refs"]]
    else:
        if client is None:
            async with GatewayChatClient(
                model=args.llm_model,
                base_url=args.llm_base_url,
                timeout_s=args.reply_timeout_s,
                max_concurrency=max(1, args.concurrency),
                fallback_models=[args.llm_fallback_model] if args.llm_fallback_model and args.llm_fallback_model != args.llm_model else None,
            ) as gateway:
                try:
                    refs, warnings, complete = await request_refs(gateway, beats, film_maps, concurrency=args.concurrency, logger=logger)
                finally:
                    write_json(work_dir / "gateway_usage.json", gateway.usage_summary())
        else:
            refs, warnings, complete = await request_refs(client, beats, film_maps, concurrency=args.concurrency, logger=logger)
        for warning in warnings:
            logger.warning(warning)
        if complete:
            cache_path.write_text(json.dumps({"key": key, "refs": [ref.model_dump(mode="json") for ref in refs]}, ensure_ascii=False), encoding="utf-8")
    write_json(args.output.expanduser().resolve(), refs)
    logger.info("Season sentence refs: %d/%d sentence(s) grounded", sum(ref.grounded for ref in refs), len(refs))
    return refs


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        asyncio.run(run(args))
    except SeriesSentenceRefError as exc:
        logging.getLogger("series_sentence_refs").error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
