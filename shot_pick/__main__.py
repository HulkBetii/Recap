"""Stage shot_pick: let a multimodal LLM pick the shot that shows what each narration sentence says.

Sentence-anchored matching (G5) already plays each sentence over footage from its own source segments, but
inside that window it cannot tell which shot shows the person the sentence is about: in a dialogue it often
lands on the listener instead of the speaker. This stage shows the gateway model the thumbnails of the
candidate shots in every sentence's source window (the same window G5 uses, `common.sentence_windows`) and
writes their 0..1 scores to `sentence_shot_scores.json`, which G5 uses to rank shots inside the window.
G5 itself stays offline; a missing or failed batch only means those sentences keep their current matching.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.inputs import load_shots
from common.llm_gateway import DEFAULT_GATEWAY_MODEL, GatewayChatClient, GatewayChatError
from common.schema import (
    ReviewBeat,
    SentenceRef,
    SentenceShotScores,
    SentenceTimestamp,
    Shot,
    ShotPickMeta,
    write_json,
)
from common.sentence_windows import sentence_source_windows

PROMPT_VERSION = "shot-pick-v1"
DEFAULT_MODEL = "ag/gemini-3.8-flash-low"
DEFAULT_MAX_CANDIDATES = 6
DEFAULT_SENTENCES_PER_REQUEST = 6
REQUEST_ATTEMPTS = 2
LABELS = "ABCDEFGHIJ"

PROMPT = """You pick footage for a Vietnamese movie recap. For each ITEM you get one narration SENTENCE and
several candidate shots from the right moment of the film, labelled A, B, C... The images are attached in
order: all candidates of the first item, then all of the next item, and so on (the order is listed per item).

Score every candidate 0-3 for how well the frame shows what the sentence describes:
3 = shows the described action or the person the sentence is about doing it;
2 = shows the person the sentence is about, or the described place/object, but not the action;
1 = same scene, but the frame is on someone else (for example the listener instead of the speaker) or on a detail;
0 = unrelated, black, blurred or a title/credit card.
When a sentence names who acts or speaks, prefer the frame that shows that person over the people they talk to.

Return ONLY JSON: [{{"id": number, "scores": {{"A": 0-3, "B": 0-3, ...}}}}].

ITEMS:
{items}
"""


class ShotPickError(RuntimeError):
    pass


@dataclass(frozen=True)
class PickItem:
    beat_id: int
    idx: int
    text: str
    candidates: list[Shot]
    key: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage shot_pick: score each sentence's candidate shots with a multimodal LLM")
    parser.add_argument("--review-script", required=True, type=Path)
    parser.add_argument("--review-sentences", required=True, type=Path)
    parser.add_argument("--sentence-timing", required=True, type=Path)
    parser.add_argument("--shots", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fallback-model", default=DEFAULT_GATEWAY_MODEL)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--timeout-s", default=600, type=int)
    parser.add_argument("--concurrency", default=4, type=int)
    parser.add_argument("--max-candidates", default=DEFAULT_MAX_CANDIDATES, type=int)
    parser.add_argument("--sentences-per-request", default=DEFAULT_SENTENCES_PER_REQUEST, type=int)
    parser.add_argument("--work-dir", default=Path("work/shot_pick"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def load_models(path: Path, model):  # type: ignore[no-untyped-def]
    return [model.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def spread(items: list[Shot], limit: int) -> list[Shot]:
    """At most `limit` shots, evenly spaced in time, always keeping the first and the last."""
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1)
    return [items[round(position * step)] for position in range(limit)]


def thumb_path(shots_path: Path, shot: Shot) -> Path:
    path = Path(shot.thumb)
    return path if path.is_absolute() else shots_path.parent / path


def item_key(text: str, candidates: list[Shot], shots_path: Path, model: str) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([PROMPT_VERSION, model, text, [shot.index for shot in candidates]], ensure_ascii=False).encode("utf-8"))
    for shot in candidates:
        digest.update(thumb_path(shots_path, shot).read_bytes())
    return digest.hexdigest()


def plan_items(
    beats: list[ReviewBeat],
    refs: list[SentenceRef],
    stamps: list[SentenceTimestamp],
    shots: list[Shot],
    *,
    shots_path: Path,
    model: str,
    max_candidates: int,
) -> tuple[list[PickItem], int]:
    """Items to score and the number of grounded sentences seen (items need at least two candidates)."""
    refs_by_beat: dict[int, list[SentenceRef]] = {}
    stamps_by_beat: dict[int, list[SentenceTimestamp]] = {}
    for ref in refs:
        refs_by_beat.setdefault(ref.beat_id, []).append(ref)
    for stamp in stamps:
        stamps_by_beat.setdefault(stamp.beat_id, []).append(stamp)
    usable = sorted((shot for shot in shots if shot.is_story and shot.is_usable and not shot.is_end_credit), key=lambda shot: shot.tc_start)
    items: list[PickItem] = []
    grounded = 0
    for beat in beats:
        beat_refs = sorted(refs_by_beat.get(beat.beat_id, []), key=lambda ref: ref.idx)
        beat_stamps = sorted(stamps_by_beat.get(beat.beat_id, []), key=lambda stamp: stamp.idx)
        if not beat_refs or len(beat_refs) != len(beat_stamps):
            continue
        spans = [(float(ref.src_tc_start), float(ref.src_tc_end)) if ref.grounded else None for ref in beat_refs]  # type: ignore[arg-type]
        windows = sentence_source_windows(spans, [stamp.tl_end - stamp.tl_start for stamp in beat_stamps], beat.src_tc_start, beat.src_tc_end)
        for ref, (start, end) in zip(beat_refs, windows):
            if not ref.grounded:
                continue
            grounded += 1
            candidates = spread([shot for shot in usable if shot.tc_start < end and start < shot.tc_end], max_candidates)
            if len(candidates) < 2:
                continue
            items.append(PickItem(beat.beat_id, ref.idx, ref.text, candidates, item_key(ref.text, candidates, shots_path, model)))
    return items, grounded


def build_prompt(batch: list[PickItem]) -> str:
    lines = [
        json.dumps(
            {"id": number, "sentence": item.text, "candidates": list(LABELS[: len(item.candidates)])},
            ensure_ascii=False,
        )
        for number, item in enumerate(batch)
    ]
    return PROMPT.format(items="\n".join(lines))


def data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def parse_scores(response: str, batch: list[PickItem]) -> dict[int, dict[int, float]]:
    """{item number: {shot index: score 0..1}}; candidates the model skipped are left out."""
    match = re.search(r"\[.*\]", response, re.S)
    if not match:
        raise ValueError("no JSON array in the shot_pick response")
    output: dict[int, dict[int, float]] = {}
    for entry in json.loads(match.group(0)):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int) or not 0 <= entry["id"] < len(batch):
            continue
        raw = entry.get("scores") if isinstance(entry.get("scores"), dict) else {}
        candidates = batch[entry["id"]].candidates
        scores: dict[int, float] = {}
        for position, shot in enumerate(candidates):
            value = raw.get(LABELS[position])
            if isinstance(value, (int, float)) and 0 <= value <= 3:
                scores[shot.index] = round(float(value) / 3.0, 4)
        if scores:
            output[entry["id"]] = scores
    return output


async def score_items(
    client: Any,
    items: list[PickItem],
    *,
    shots_path: Path,
    per_request: int,
    logger: logging.Logger,
) -> tuple[dict[str, dict[int, float]], int, list[str]]:
    batches = [items[start : start + per_request] for start in range(0, len(items), per_request)]
    results: dict[str, dict[int, float]] = {}
    warnings: list[str] = []
    requests = 0

    async def run(number: int, batch: list[PickItem]) -> None:
        nonlocal requests
        images = [data_url(thumb_path(shots_path, shot)) for item in batch for shot in item.candidates]
        failure: Exception | None = None
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            requests += 1
            try:
                parsed = parse_scores(await client.ask(build_prompt(batch), image_data_urls=images), batch)
            except (GatewayChatError, ValueError) as exc:
                failure = exc
                logger.warning("shot_pick batch %d/%d attempt %d failed: %s", number, len(batches), attempt, exc)
                continue
            for position, scores in parsed.items():
                results[batch[position].key] = scores
            return
        warnings.append(f"shot_pick batch {number}/{len(batches)} failed: {failure}")

    await asyncio.gather(*(run(number, batch) for number, batch in enumerate(batches, start=1)))
    return results, requests, warnings


def input_hash(paths: list[Path], settings: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps([PROMPT_VERSION, settings], sort_keys=True).encode("utf-8"))
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def run_shot_pick(args: argparse.Namespace, client: Any | None = None) -> tuple[list[SentenceShotScores], ShotPickMeta]:
    logger = logging.getLogger("shot_pick")
    inputs = [args.review_script, args.review_sentences, args.sentence_timing, args.shots]
    for path in inputs:
        if not path.expanduser().resolve().is_file():
            raise ShotPickError(f"input file does not exist: {path}")
    if args.max_candidates < 2 or args.max_candidates > len(LABELS):
        raise ShotPickError(f"--max-candidates must be within 2..{len(LABELS)}")
    shots_path = args.shots.expanduser().resolve()
    beats = load_models(args.review_script.expanduser().resolve(), ReviewBeat)
    refs = load_models(args.review_sentences.expanduser().resolve(), SentenceRef)
    stamps = load_models(args.sentence_timing.expanduser().resolve(), SentenceTimestamp)
    shots = load_shots(shots_path)
    items, grounded = plan_items(beats, refs, stamps, shots, shots_path=shots_path, model=args.model, max_candidates=args.max_candidates)

    work_dir = args.work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_path = work_dir / "cache.json"
    cache: dict[str, dict[str, float]] = {}
    if cache_path.is_file() and not args.force:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    pending = [item for item in items if item.key not in cache]
    logger.info("shot_pick: %d grounded sentence(s), %d with >=2 candidates, %d cached", grounded, len(items), len(items) - len(pending))
    requests = 0
    warnings: list[str] = []
    if pending:
        if client is None:
            async with GatewayChatClient(
                model=args.model,
                base_url=args.base_url,
                timeout_s=args.timeout_s,
                max_concurrency=max(1, args.concurrency),
                fallback_models=[args.fallback_model] if args.fallback_model and args.fallback_model != args.model else None,
            ) as gateway:
                try:
                    fresh, requests, warnings = await score_items(gateway, pending, shots_path=shots_path, per_request=args.sentences_per_request, logger=logger)
                finally:
                    write_json(work_dir / "gateway_usage.json", gateway.usage_summary())
        else:
            fresh, requests, warnings = await score_items(client, pending, shots_path=shots_path, per_request=args.sentences_per_request, logger=logger)
        cache.update({key: {str(shot): score for shot, score in scores.items()} for key, scores in fresh.items()})
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    output = [
        SentenceShotScores(beat_id=item.beat_id, idx=item.idx, scores={int(shot): score for shot, score in cache[item.key].items()})
        for item in items
        if item.key in cache
    ]
    meta = ShotPickMeta(
        model=args.model,
        prompt_version=PROMPT_VERSION,
        n_sentences=len(refs),
        n_candidate_sentences=len(items),
        n_scored_sentences=len(output),
        n_requests=requests,
        n_cached_sentences=len(items) - len(pending),
        max_candidates=args.max_candidates,
        input_hash=input_hash([path.expanduser().resolve() for path in inputs], {"model": args.model, "max_candidates": args.max_candidates}),
        created_at=datetime.now(timezone.utc),
        warnings=warnings,
    )
    output_path = args.output.expanduser().resolve()
    write_json(output_path, output)
    write_json(output_path.with_name(f"{output_path.stem}.meta.json"), meta)
    logger.info("shot_pick: scored %d/%d sentence(s) with %d request(s)", len(output), len(items), requests)
    return output, meta


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        asyncio.run(run_shot_pick(args))
    except ShotPickError as exc:
        logging.getLogger("shot_pick").error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
