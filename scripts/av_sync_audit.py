"""Independent audio-visual sync audit of a rendered recap run.

For every narration sentence, a multimodal judge (9router) sees the sentence, a frame from the middle
of the footage shown while the sentence is spoken, and the film-map lines (dialogue / scene description)
at that footage's source time. It labels the pair match / partial / mismatch / commentary. The judge never
sees the matcher's own sentence refs or source spans, so the score does not just echo the matcher's plan.

    python -m scripts.av_sync_audit --run-dir runs/bench-qods-final5 --film "C:/path/film.mp4"

Writes `av_sync_audit.json` and `av_sync_audit.html` (frames + verdicts for eyeballing) into the run dir.
Sentence windows come from `sentence_timing.json` when present, otherwise they are computed from the run's
beat audio with the same pause-snapping G3 uses.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from common.llm_gateway import GatewayChatClient
from common.schema import BeatTiming, EdlPlacement, FilmMapSegment, ReviewBeat, SentenceTimestamp
from common.text import split_sentences
from tts.sentence_timing import build_sentence_timing

DEFAULT_MODEL = "ag/gemini-3.8-flash-high"
VERDICT_SCORE = {"match": 1.0, "partial": 0.5, "mismatch": 0.0}
FRAME_WIDTH = 512
MAX_SOURCE_LINES = 6

JUDGE_PROMPT = """You check whether the footage of a Vietnamese movie recap shows what the narration says at that moment.
For each ITEM you get the narration SENTENCE spoken over the shot, one FRAME from the middle of that footage
(images are attached in item order), and SOURCE: the original film's dialogue (English) or scene description
at the footage's source time.

Label each item:
- "match": the footage shows the event, action, people or place the sentence describes.
- "partial": same scene or same characters, but not the specific action/moment described.
- "mismatch": a different scene, unrelated people or an unrelated moment.
- "commentary": the sentence describes nothing concretely visible (opinion, summary, rhetorical question).

Return ONLY a JSON array: [{"id": number, "verdict": "match|partial|mismatch|commentary", "reason": "short reason in Vietnamese"}].

ITEMS:
"""


def film_path_from_run(run_dir: Path) -> Path | None:
    log = run_dir / "run.log"
    if not log.exists():
        return None
    match = re.search(r"-m render .*?--film (.+?) --output ", log.read_text(encoding="utf-8", errors="replace"))
    return Path(match.group(1).strip()) if match else None


def load_models(path: Path, model):  # type: ignore[no-untyped-def]
    return [model.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def sentence_windows(run_dir: Path, beats: list[ReviewBeat], timings: list[BeatTiming], work: Path) -> list[SentenceTimestamp]:
    stored = run_dir / "sentence_timing.json"
    if stored.exists():
        return load_models(stored, SentenceTimestamp)
    stamps, _warnings, _snapped = build_sentence_timing(
        beats=beats,
        timings=timings,
        audio_dir=run_dir / "audio",
        text_mode="vi",
        lexicon={},
        mode="silence",
        cache_path=work / "sentence_timing_cache.json",
    )
    return stamps


def placement_source_time(placement: EdlPlacement, tl_time: float) -> float:
    span = placement.tl_end - placement.tl_start
    fraction = 0.0 if span <= 0 else (tl_time - placement.tl_start) / span
    return placement.src_in + fraction * (placement.src_out - placement.src_in)


def source_lines(film_map: list[FilmMapSegment], ranges: list[tuple[float, float]]) -> list[str]:
    lines: list[str] = []
    for segment in film_map:
        if any(segment.tc_start < end + 0.5 and segment.tc_end > start - 0.5 for start, end in ranges):
            text = segment.en if segment.type == "speech" else f"[scene] {segment.scene_desc}"
            if text:
                lines.append(" ".join(text.split())[:200])
    return lines[:MAX_SOURCE_LINES]


def extract_frame(film: Path, seconds: float, output: Path) -> None:
    if output.exists():
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(film), "-frames:v", "1",
         "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", "-y", str(output)],
        check=True,
    )


def build_items(run_dir: Path, film: Path, work: Path) -> list[dict]:
    beats = load_models(run_dir / "review_script.json", ReviewBeat)
    timings = load_models(run_dir / "beats_timing.json", BeatTiming)
    edl = load_models(run_dir / "edl.json", EdlPlacement)
    film_map = load_models(run_dir / "film_map.json", FilmMapSegment)
    stamps = sentence_windows(run_dir, beats, timings, work)
    texts = {beat.beat_id: split_sentences(beat.narration) for beat in beats}
    items: list[dict] = []
    for stamp in stamps:
        sentences = texts.get(stamp.beat_id, [])
        if stamp.idx >= len(sentences):
            continue
        shown = [p for p in edl if p.tl_start < stamp.tl_end and p.tl_end > stamp.tl_start]
        if not shown:
            continue
        middle = (stamp.tl_start + stamp.tl_end) / 2
        at_middle = min(shown, key=lambda p: 0 if p.tl_start <= middle < p.tl_end else min(abs(p.tl_start - middle), abs(p.tl_end - middle)))
        frame_time = placement_source_time(at_middle, min(max(middle, at_middle.tl_start), at_middle.tl_end - 1e-3))
        ranges = [
            (placement_source_time(p, max(p.tl_start, stamp.tl_start)), placement_source_time(p, min(p.tl_end, stamp.tl_end)))
            for p in shown
        ]
        frame = work / "frames" / f"{stamp.beat_id:03d}_{stamp.idx:02d}.jpg"
        extract_frame(film, frame_time, frame)
        items.append(
            {
                "id": len(items),
                "beat_id": stamp.beat_id,
                "idx": stamp.idx,
                "sentence": sentences[stamp.idx],
                "tl_start": stamp.tl_start,
                "tl_end": stamp.tl_end,
                "frame_src_s": round(frame_time, 2),
                "source": source_lines(film_map, ranges),
                "frame": frame,
            }
        )
    return items


def judge_prompt(batch: list[dict]) -> str:
    lines = [
        json.dumps({"id": item["id"], "sentence": item["sentence"], "source": item["source"]}, ensure_ascii=False)
        for item in batch
    ]
    return JUDGE_PROMPT + "\n".join(lines)


def data_url(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def parse_verdicts(response: str) -> list[dict]:
    match = re.search(r"\[.*\]", response, re.S)
    if not match:
        raise ValueError("judge returned no JSON array")
    return [item for item in json.loads(match.group(0)) if isinstance(item, dict)]


async def judge(items: list[dict], *, model: str, batch_size: int, concurrency: int) -> None:
    batches = [items[start : start + batch_size] for start in range(0, len(items), batch_size)]
    async with GatewayChatClient(model=model, max_concurrency=concurrency) as client:

        async def run(batch: list[dict]) -> None:
            for attempt in range(2):
                try:
                    response = await client.ask(judge_prompt(batch), image_data_urls=[data_url(item["frame"]) for item in batch])
                    by_id = {int(verdict["id"]): verdict for verdict in parse_verdicts(response) if "id" in verdict}
                except (ValueError, KeyError, TypeError) as exc:
                    if attempt:
                        print(f"batch starting at item {batch[0]['id']} failed: {exc}", file=sys.stderr)
                    continue
                for item in batch:
                    verdict = by_id.get(item["id"], {})
                    item["verdict"] = verdict.get("verdict") if verdict.get("verdict") in {*VERDICT_SCORE, "commentary"} else None
                    item["reason"] = str(verdict.get("reason") or "")
                return

        await asyncio.gather(*(run(batch) for batch in batches))


def summarize(items: list[dict]) -> dict:
    judged = [item for item in items if item.get("verdict") in VERDICT_SCORE]
    seconds = sum(item["tl_end"] - item["tl_start"] for item in judged)
    score = sum(VERDICT_SCORE[item["verdict"]] * (item["tl_end"] - item["tl_start"]) for item in judged)
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[str(item.get("verdict"))] += 1
    per_beat: dict[int, list[float]] = defaultdict(list)
    for item in judged:
        per_beat[item["beat_id"]].append(VERDICT_SCORE[item["verdict"]])
    worst = sorted(
        (item for item in judged if item["verdict"] == "mismatch"),
        key=lambda item: item["tl_end"] - item["tl_start"],
        reverse=True,
    )[:20]
    return {
        "n_sentences": len(items),
        "verdict_counts": dict(counts),
        "judged_seconds": round(seconds, 1),
        "match_rate_by_duration": round(score / seconds, 4) if seconds else None,
        "match_rate_by_count": round(sum(VERDICT_SCORE[item["verdict"]] for item in judged) / len(judged), 4) if judged else None,
        "per_beat": {beat: round(sum(scores) / len(scores), 3) for beat, scores in sorted(per_beat.items())},
        "worst_mismatches": [{key: item[key] for key in ("beat_id", "idx", "sentence", "reason", "frame_src_s")} for item in worst],
    }


def write_html(path: Path, run_name: str, summary: dict, items: list[dict], work: Path) -> None:
    colors = {"match": "#2e7d32", "partial": "#b7791f", "mismatch": "#c62828", "commentary": "#607d8b"}
    rows = []
    for item in items:
        verdict = item.get("verdict") or "n/a"
        rows.append(
            f"<tr><td>{item['beat_id']}.{item['idx']}<br><small>{item['tl_start']:.1f}s</small></td>"
            f"<td><img src=\"{html.escape(item['frame'].relative_to(path.parent).as_posix())}\" width=\"256\"></td>"
            f"<td>{html.escape(item['sentence'])}<div class=\"src\">{'<br>'.join(html.escape(line) for line in item['source'])}</div></td>"
            f"<td style=\"color:{colors.get(verdict, '#999')};font-weight:600\">{verdict}<div class=\"src\">{html.escape(item.get('reason', ''))}</div></td></tr>"
        )
    path.write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>AV sync audit</title>"
        "<style>body{font:14px system-ui;margin:16px;background:#fafafa}table{border-collapse:collapse;width:100%}"
        "td{border-bottom:1px solid #ddd;padding:6px;vertical-align:top}.src{color:#666;font-size:12px;margin-top:4px}</style>"
        f"<h2>{html.escape(run_name)}</h2><p>match rate (duration) <b>{summary['match_rate_by_duration']}</b>, "
        f"verdicts {html.escape(json.dumps(summary['verdict_counts']))}</p><table>{''.join(rows)}</table>",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--film", default=None, type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", default=12, type=int)
    parser.add_argument("--concurrency", default=4, type=int)
    parser.add_argument("--tag", default="", help="Suffix for the report files, e.g. a judge name, so judges do not overwrite each other.")
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    film = args.film or film_path_from_run(run_dir)
    if film is None or not film.exists():
        parser.error("--film is required (could not read the source film path from run.log)")
    work = run_dir / "work" / "av_audit"
    items = build_items(run_dir, film, work)
    asyncio.run(judge(items, model=args.model, batch_size=args.batch_size, concurrency=args.concurrency))
    summary = summarize(items)
    report = {"run": run_dir.name, "model": args.model, **summary, "items": [{**item, "frame": item["frame"].name} for item in items]}
    stem = f"av_sync_audit.{args.tag}" if args.tag else "av_sync_audit"
    (run_dir / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    write_html(run_dir / f"{stem}.html", run_dir.name, summary, items, work)
    print(json.dumps({key: summary[key] for key in ("n_sentences", "verdict_counts", "match_rate_by_duration", "match_rate_by_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
