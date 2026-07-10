from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.schema import (
    BeatTiming,
    FilmMapSegment,
    MicroPolicyMeta,
    ReviewBeat,
    ReviewMicroBeat,
    ReviewMicroMeta,
    TtsSentenceAlignment,
    validate_review_script,
    write_json,
)

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_ABBREVIATIONS = ("TP.", "P.", "Q.", "Mr.", "Mrs.", "Dr.")

class TtsAlignError(RuntimeError):
    pass

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 3.5: TTS sentence alignment + review_script.micro.json")
    parser.add_argument("--review-script", required=True, type=Path)
    parser.add_argument("--beats-timing", required=True, type=Path)
    parser.add_argument("--film-map", required=True, type=Path)
    parser.add_argument("--audio-dir", required=True, type=Path)
    parser.add_argument("--output-micro", required=True, type=Path)
    parser.add_argument("--output-policy", required=True, type=Path)
    parser.add_argument("--output-align", required=True, type=Path)
    parser.add_argument("--output-meta", required=True, type=Path)
    parser.add_argument("--mode", default="auto", choices=["off", "auto", "on"])
    parser.add_argument("--max-source-span-s", type=float, default=120.0)
    parser.add_argument("--max-narration-chars", type=int, default=520)
    parser.add_argument("--min-sentences", type=int, default=2)
    parser.add_argument("--target-sub-beat-audio-s", type=float, default=8.0)
    parser.add_argument("--max-sub-beat-audio-s", type=float, default=12.0)
    parser.add_argument("--split-hooks", action="store_true", default=True)
    parser.add_argument("--no-split-hooks", dest="split_hooks", action="store_false")
    parser.add_argument("--aligner", default="whisperx", choices=["none", "whisperx"])
    parser.add_argument("--alignment-device", default="auto")
    parser.add_argument("--source-language", default="vi")
    parser.add_argument("--work-dir", default=None, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser

def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def split_sentences(text: str) -> list[str]:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return []
    protected = normalized
    replacements: dict[str, str] = {}
    for index, token in enumerate(_ABBREVIATIONS):
        marker = f"__ABBR_{index}__"
        replacements[marker] = token
        protected = protected.replace(token, marker)
    parts = [part.strip() for part in _SENTENCE_RE.split(protected) if part.strip()]
    output: list[str] = []
    for part in parts or [protected]:
        for marker, token in replacements.items():
            part = part.replace(marker, token)
        if part:
            output.append(part)
    return output

def resolve_audio_path(audio_dir: Path, timing: BeatTiming) -> Path:
    from_timing = Path(timing.audio_path)
    candidates = [audio_dir / f"{timing.beat_id}.mp3"]
    if not from_timing.is_absolute():
        candidates.append(audio_dir.parent / from_timing)
    else:
        candidates.append(from_timing)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]

def should_split(beat: ReviewBeat, sentences: list[str], args: argparse.Namespace, timing: BeatTiming | None = None) -> bool:
    if len(sentences) < args.min_sentences:
        return False
    if beat.is_hook and not args.split_hooks:
        return False
    span = beat.src_tc_end - beat.src_tc_start
    duration = timing.duration if timing is not None else 0.0
    return span > args.max_source_span_s or len(beat.narration) > args.max_narration_chars or duration > args.max_sub_beat_audio_s

def build_policy(beats: list[ReviewBeat], timings: dict[int, BeatTiming], args: argparse.Namespace) -> MicroPolicyMeta:
    spans = [beat.src_tc_end - beat.src_tc_start for beat in beats]
    max_chars = max((len(beat.narration) for beat in beats), default=0)
    candidate_count = sum(1 for beat in beats if should_split(beat, split_sentences(beat.narration), args, timings.get(beat.beat_id)))
    if args.mode == "off":
        enabled = False
        reason = "mode=off"
    elif args.mode == "on":
        enabled = True
        reason = "mode=on"
    else:
        enabled = candidate_count > 0
        reason = f"auto: {candidate_count} candidate beat(s) exceed thresholds" if enabled else "auto: no beat exceeds thresholds"
    return MicroPolicyMeta(
        mode=args.mode,
        enabled=enabled,
        reason=reason,
        n_parent_beats=len(beats),
        n_candidate_beats=candidate_count,
        avg_source_span_s=round(sum(spans) / len(spans), 3) if spans else 0.0,
        max_source_span_s=round(max(spans), 3) if spans else 0.0,
        max_narration_chars=max_chars,
        thresholds={
            "max_source_span_s": args.max_source_span_s,
            "max_narration_chars": float(args.max_narration_chars),
            "min_sentences": float(args.min_sentences),
            "target_sub_beat_audio_s": args.target_sub_beat_audio_s,
            "max_sub_beat_audio_s": args.max_sub_beat_audio_s,
            "split_by_audio_duration": 1.0,
        },
    )

def proportional_align(beat: ReviewBeat, timing: BeatTiming, sentences: list[str]) -> list[TtsSentenceAlignment]:
    weights = [max(1, len(sentence)) for sentence in sentences]
    total_weight = sum(weights)
    cursor = 0.0
    output: list[TtsSentenceAlignment] = []
    for index, (sentence, weight) in enumerate(zip(sentences, weights)):
        if index == len(sentences) - 1:
            audio_end = timing.duration
        else:
            audio_end = round(cursor + timing.duration * weight / total_weight, 3)
        audio_start = round(cursor, 3)
        audio_end = max(audio_start + 0.001, audio_end)
        output.append(TtsSentenceAlignment(
            parent_beat_id=beat.beat_id,
            sentence_index=index,
            text=sentence,
            audio_start=audio_start,
            audio_end=round(audio_end, 3),
            tl_start=round(timing.tl_start + audio_start, 3),
            tl_end=round(timing.tl_start + audio_end, 3),
            alignment_method="proportional",
            confidence=None,
        ))
        cursor = audio_end
    return output

def whisperx_align(beat: ReviewBeat, timing: BeatTiming, sentences: list[str], audio_path: Path, args: argparse.Namespace) -> list[TtsSentenceAlignment]:
    # Phase 1 keeps WhisperX optional. If runtime/model output shape differs,
    # raise and let caller use proportional fallback.
    try:
        state = getattr(args, "_whisperx_state")
    except AttributeError:
        try:
            import torch  # type: ignore
            import whisperx  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise TtsAlignError(f"whisperx unavailable: {exc}") from exc
        device = args.alignment_device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        language_code = "vi" if args.source_language.lower().startswith("vi") else args.source_language.lower()
        try:
            model, metadata = whisperx.load_align_model(language_code=language_code, device=device)
        except Exception as exc:  # noqa: BLE001
            raise TtsAlignError(f"whisperx alignment model failed: {exc}") from exc
        state = {"whisperx": whisperx, "device": device, "model": model, "metadata": metadata}
        setattr(args, "_whisperx_state", state)
    whisperx = state["whisperx"]
    device = state["device"]
    model = state["model"]
    metadata = state["metadata"]
    try:
        audio = whisperx.load_audio(str(audio_path))
        segments = []
        cursor = 0.0
        for sentence in sentences:
            approx = max(0.25, timing.duration * max(1, len(sentence)) / max(1, len(beat.narration)))
            segments.append({"start": cursor, "end": min(timing.duration, cursor + approx), "text": sentence})
            cursor = min(timing.duration, cursor + approx)
        result = whisperx.align(segments, model, metadata, audio, device, return_char_alignments=False)
    except Exception as exc:  # noqa: BLE001
        raise TtsAlignError(f"whisperx alignment failed: {exc}") from exc
    aligned_segments = result.get("segments") or []
    if len(aligned_segments) != len(sentences):
        raise TtsAlignError("whisperx returned unexpected segment count")
    output: list[TtsSentenceAlignment] = []
    previous_end = 0.0
    for index, (sentence, segment) in enumerate(zip(sentences, aligned_segments)):
        start = max(previous_end, float(segment.get("start", previous_end)))
        end = float(segment.get("end", start))
        if index == len(sentences) - 1:
            end = timing.duration
        start = min(max(0.0, start), timing.duration)
        end = min(max(start + 0.001, end), timing.duration)
        output.append(TtsSentenceAlignment(
            parent_beat_id=beat.beat_id,
            sentence_index=index,
            text=sentence,
            audio_start=round(start, 3),
            audio_end=round(end, 3),
            tl_start=round(timing.tl_start + start, 3),
            tl_end=round(timing.tl_start + end, 3),
            alignment_method="whisperx",
            confidence=None,
        ))
        previous_end = end
    return output

def align_sentences(beat: ReviewBeat, timing: BeatTiming, sentences: list[str], audio_path: Path, args: argparse.Namespace, warnings: list[str]) -> list[TtsSentenceAlignment]:
    if args.aligner == "whisperx" and audio_path.is_file():
        try:
            return whisperx_align(beat, timing, sentences, audio_path, args)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"beat {beat.beat_id}: {exc}; using proportional fallback")
    elif args.aligner == "whisperx":
        warnings.append(f"beat {beat.beat_id}: audio file missing for whisperx ({audio_path}); using proportional fallback")
    return proportional_align(beat, timing, sentences)

def group_sentence_alignments(alignments: list[TtsSentenceAlignment], args: argparse.Namespace) -> list[TtsSentenceAlignment]:
    if len(alignments) <= 1:
        return alignments
    groups: list[list[TtsSentenceAlignment]] = []
    current: list[TtsSentenceAlignment] = []
    for alignment in alignments:
        if not current:
            current = [alignment]
            continue
        current_duration = current[-1].audio_end - current[0].audio_start
        next_duration = alignment.audio_end - current[0].audio_start
        if current_duration >= args.target_sub_beat_audio_s or next_duration > args.max_sub_beat_audio_s:
            groups.append(current)
            current = [alignment]
        else:
            current.append(alignment)
    if current:
        groups.append(current)
    output: list[TtsSentenceAlignment] = []
    for index, group in enumerate(groups):
        first = group[0]
        last = group[-1]
        methods = {item.alignment_method for item in group}
        method = methods.pop() if len(methods) == 1 else "mixed"
        confidences = [item.confidence for item in group if item.confidence is not None]
        output.append(TtsSentenceAlignment(
            parent_beat_id=first.parent_beat_id,
            sentence_index=index,
            text=" ".join(item.text for item in group),
            audio_start=first.audio_start,
            audio_end=last.audio_end,
            tl_start=first.tl_start,
            tl_end=last.tl_end,
            alignment_method=method,
            confidence=round(sum(confidences) / len(confidences), 3) if confidences else None,
        ))
    return output


def partition_source_by_alignment(beat: ReviewBeat, alignments: list[TtsSentenceAlignment], film_map: list[FilmMapSegment]) -> list[tuple[int, int, float, float]]:
    by_id = {segment.id: segment for segment in film_map}
    segment_ids = [sid for sid in range(beat.from_seg_id, beat.to_seg_id + 1) if sid in by_id]
    if not segment_ids:
        return [(beat.from_seg_id, beat.to_seg_id, beat.src_tc_start, beat.src_tc_end) for _ in alignments]
    total_audio = max(0.001, alignments[-1].audio_end - alignments[0].audio_start)
    total_source = beat.src_tc_end - beat.src_tc_start
    output: list[tuple[int, int, float, float]] = []
    prev_tc = beat.src_tc_start
    for index, alignment in enumerate(alignments):
        if index == len(alignments) - 1:
            start_tc = prev_tc
            end_tc = beat.src_tc_end
        else:
            start_tc = prev_tc
            ratio = (alignment.audio_end - alignments[0].audio_start) / total_audio
            end_tc = beat.src_tc_start + total_source * ratio
        start_tc = max(beat.src_tc_start, min(start_tc, beat.src_tc_end - 0.001))
        end_tc = max(start_tc + 0.001, min(end_tc, beat.src_tc_end))
        overlapping = [segment.id for segment in film_map if segment.id in segment_ids and segment.tc_end > start_tc and segment.tc_start < end_tc]
        if not overlapping:
            overlapping = [min(segment_ids, key=lambda sid: abs((by_id[sid].tc_start + by_id[sid].tc_end) / 2 - (start_tc + end_tc) / 2))]
        output.append((overlapping[0], overlapping[-1], round(start_tc, 3), round(end_tc, 3)))
        prev_tc = end_tc
    return output

def build_micro_beats(beats: list[ReviewBeat], timings: dict[int, BeatTiming], film_map: list[FilmMapSegment], policy: MicroPolicyMeta, args: argparse.Namespace) -> tuple[list[ReviewMicroBeat], list[dict[str, Any]], list[str]]:
    warnings: list[str] = list(policy.warnings)
    micro: list[ReviewMicroBeat] = []
    align_report: list[dict[str, Any]] = []
    next_id = 0
    for beat in beats:
        timing = timings[beat.beat_id]
        sentences = split_sentences(beat.narration)
        split = policy.enabled and (args.mode == "on" or should_split(beat, sentences, args, timing))
        if not split:
            sentences = [beat.narration]
        audio_path = resolve_audio_path(args.audio_dir.expanduser().resolve(), timing)
        alignments = align_sentences(beat, timing, sentences, audio_path, args, warnings) if split else [
            TtsSentenceAlignment(
                parent_beat_id=beat.beat_id,
                sentence_index=0,
                text=beat.narration,
                audio_start=0.0,
                audio_end=round(timing.duration, 3),
                tl_start=timing.tl_start,
                tl_end=timing.tl_end,
                alignment_method="parent",
            )
        ]
        sentence_alignments = alignments
        if split:
            alignments = group_sentence_alignments(alignments, args)
        spans = partition_source_by_alignment(beat, alignments, film_map)
        for sub_id, (alignment, (from_seg_id, to_seg_id, src_start, src_end)) in enumerate(zip(alignments, spans)):
            micro.append(ReviewMicroBeat(
                beat_id=next_id,
                parent_beat_id=beat.beat_id,
                sub_beat_id=sub_id,
                narration=alignment.text,
                from_seg_id=from_seg_id,
                to_seg_id=to_seg_id,
                src_tc_start=src_start,
                src_tc_end=src_end,
                tl_start=alignment.tl_start,
                tl_end=alignment.tl_end,
                duration=round(alignment.tl_end - alignment.tl_start, 3),
                alignment_method=alignment.alignment_method,
                is_hook=beat.is_hook and sub_id == 0,
            ))
            next_id += 1
        align_report.append({
            "parent_beat_id": beat.beat_id,
            "audio_path": str(audio_path),
            "split": split,
            "sentences": [item.model_dump() for item in sentence_alignments],
            "micro_units": [item.model_dump() for item in alignments],
        })
    return micro, align_report, warnings

def validate_micro_beats(micro: list[ReviewMicroBeat], parent_beats: list[ReviewBeat]) -> None:
    parent_by_id = {beat.beat_id: beat for beat in parent_beats}
    previous_by_parent: dict[int, ReviewMicroBeat] = {}
    for item in micro:
        parent = parent_by_id[item.parent_beat_id]
        if item.src_tc_start < parent.src_tc_start - 1e-3 or item.src_tc_end > parent.src_tc_end + 1e-3:
            raise TtsAlignError(f"micro beat {item.beat_id} source span outside parent")
        if item.tl_start < -1e-3:
            raise TtsAlignError(f"micro beat {item.beat_id} has invalid timeline")
        previous = previous_by_parent.get(item.parent_beat_id)
        if previous and item.src_tc_start < previous.src_tc_start - 1e-3:
            raise TtsAlignError(f"micro beat {item.beat_id} source is not monotonic within parent")
        previous_by_parent[item.parent_beat_id] = item

def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    review_path = args.review_script.expanduser().resolve()
    timing_path = args.beats_timing.expanduser().resolve()
    film_map_path = args.film_map.expanduser().resolve()
    output_paths = [args.output_micro, args.output_policy, args.output_align, args.output_meta]
    if not args.force and all(path.expanduser().resolve().is_file() for path in output_paths):
        logging.info("Using cached tts_align outputs")
        return 0
    beats = [ReviewBeat.model_validate(item) for item in read_json(review_path)]
    film_map = [FilmMapSegment.model_validate(item) for item in read_json(film_map_path)]
    validate_review_script(beats, film_map)
    timings = sorted([BeatTiming.model_validate(item) for item in read_json(timing_path)], key=lambda item: item.beat_id)
    for expected_id, timing in enumerate(timings):
        if timing.beat_id != expected_id:
            raise TtsAlignError(f"beat timing ids must be continuous: expected {expected_id}, got {timing.beat_id}")
    timings_by_id = {timing.beat_id: timing for timing in timings}
    missing = [beat.beat_id for beat in beats if beat.beat_id not in timings_by_id]
    if missing:
        raise TtsAlignError(f"missing beat timing for beat ids: {missing[:10]}")
    policy = build_policy(beats, timings_by_id, args)
    micro, align_report, warnings = build_micro_beats(beats, timings_by_id, film_map, policy, args)
    validate_micro_beats(micro, beats)
    spans = [item.src_tc_end - item.src_tc_start for item in micro]
    methods = Counter(item.alignment_method for item in micro)
    meta = ReviewMicroMeta(
        enabled=policy.enabled,
        mode=policy.mode,
        n_parent_beats=len(beats),
        n_micro_beats=len(micro),
        n_split_parent_beats=sum(1 for item in align_report if item["split"]),
        avg_source_span_s=round(sum(spans) / len(spans), 3) if spans else 0.0,
        max_source_span_s=round(max(spans), 3) if spans else 0.0,
        alignment_methods=dict(methods),
        policy_path=str(args.output_policy),
        alignment_path=str(args.output_align),
        created_at=datetime.now(timezone.utc),
        warnings=warnings,
    )
    cache_key = {
        "review_script": file_hash(review_path),
        "beats_timing": file_hash(timing_path),
        "film_map": file_hash(film_map_path),
        "mode": args.mode,
        "aligner": args.aligner,
        "thresholds": policy.thresholds,
    }
    policy_payload = policy.model_dump()
    policy_payload["cache_key"] = cache_key
    write_json(args.output_policy.expanduser().resolve(), policy_payload)
    write_json(args.output_align.expanduser().resolve(), {"version": 1, "aligner": args.aligner, "beats": align_report, "warnings": warnings})
    write_json(args.output_micro.expanduser().resolve(), micro)
    write_json(args.output_meta.expanduser().resolve(), meta)
    logging.info("Wrote %d micro beat(s) from %d parent beat(s)", len(micro), len(beats))
    return 0

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001
        parser.exit(1, f"tts_align: error: {exc}\n")

if __name__ == "__main__":
    raise SystemExit(main())
