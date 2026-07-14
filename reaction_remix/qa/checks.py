from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from common.schema import (
    CommentaryAudio,
    CommentaryScript,
    ReactionBlocks,
    ReactionTranscript,
    RemixCommandManifest,
    RemixEdl,
    RemixPlan,
    RemixRenderTimeline,
    RemixRenderTimelinePlacement,
)


class RemixQaError(RuntimeError):
    pass


PRESERVATION_EDGE_WINDOW_S = 0.5
PRESERVATION_MIDDLE_WINDOW_S = 2.0
SILENCE_RMS_THRESHOLD = 0.1
ONE_SIDED_SILENCE_GAIN_DELTA_DB = 120.0


@dataclass(frozen=True)
class ReactionPlacementPreservation:
    placement_id: str
    min_audio_correlation: float
    max_av_drift_ms: float
    min_frame_similarity: float
    max_gain_delta_db: float


@dataclass(frozen=True)
class ReactionPreservationMeasurement:
    placements: tuple[ReactionPlacementPreservation, ...]

    @property
    def min_audio_correlation(self) -> float:
        return min((item.min_audio_correlation for item in self.placements), default=0.0)

    @property
    def max_av_drift_ms(self) -> float:
        return max((item.max_av_drift_ms for item in self.placements), default=0.0)

    @property
    def min_frame_similarity(self) -> float:
        return min((item.min_frame_similarity for item in self.placements), default=0.0)

    @property
    def max_gain_delta_db(self) -> float:
        return max((item.max_gain_delta_db for item in self.placements), default=0.0)

    def failed_placement_ids(
        self,
        *,
        min_correlation: float,
        max_lag_ms: float,
        min_frame_similarity: float,
        max_gain_delta_db: float,
    ) -> list[str]:
        return [
            item.placement_id
            for item in self.placements
            if item.min_audio_correlation < min_correlation
            or item.max_av_drift_ms > max_lag_ms
            or item.min_frame_similarity < min_frame_similarity
            or item.max_gain_delta_db > max_gain_delta_db
        ]


@dataclass(frozen=True)
class BoundaryFrameMeasurement:
    placement_similarities: tuple[tuple[str, float], ...]

    @property
    def min_frame_similarity(self) -> float:
        return min((similarity for _placement_id, similarity in self.placement_similarities), default=1.0)

    def failed_placement_ids(self, *, min_frame_similarity: float) -> list[str]:
        return [
            placement_id
            for placement_id, similarity in self.placement_similarities
            if similarity < min_frame_similarity
        ]


def probe_output(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RemixQaError((result.stderr or "").strip() or "ffprobe failed")
    try:
        payload = json.loads(result.stdout)
        video = next(stream for stream in payload["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in payload["streams"] if stream["codec_type"] == "audio")
        return {
            "duration_s": float(payload["format"]["duration"]),
            "video_codec": str(video["codec_name"]),
            "audio_codec": str(audio["codec_name"]),
            "width": int(video["width"]),
            "height": int(video["height"]),
            "fps": _rate(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"),
            "sample_rate": int(audio["sample_rate"]),
            "channels": int(audio["channels"]),
        }
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RemixQaError("rendered output is missing required H.264/AAC streams") from exc


def _rate(value: str) -> float:
    if "/" not in value:
        return float(value)
    numerator, denominator = value.split("/", 1)
    return float(numerator) / float(denominator)


def full_decode_ok(path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0


def decode_audio(path: Path, *, start_s: float = 0.0, duration_s: float | None = None, sample_rate: int = 16000) -> np.ndarray:
    args = ["ffmpeg", "-v", "error", "-ss", f"{start_s:.6f}", "-i", str(path)]
    if duration_s is not None:
        args.extend(["-t", f"{duration_s:.6f}"])
    args.extend(["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"])
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode != 0:
        return np.empty(0, dtype=np.float32)
    return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)


def best_correlation(reference: np.ndarray, candidate: np.ndarray, *, max_lag_samples: int) -> tuple[float, int]:
    length = min(len(reference), len(candidate))
    if length < 256:
        return 0.0, 0
    reference = reference[:length]
    candidate = candidate[:length]
    if np.array_equal(reference, candidate):
        return 1.0, 0
    reference_rms = _rms(reference)
    candidate_rms = _rms(candidate)
    reference_silent = reference_rms <= SILENCE_RMS_THRESHOLD
    candidate_silent = candidate_rms <= SILENCE_RMS_THRESHOLD
    if reference_silent and candidate_silent:
        return 1.0, 0
    if reference_silent != candidate_silent:
        return 0.0, 0
    best = (-1.0, 0)
    for lag in range(-max_lag_samples, max_lag_samples + 1):
        if lag < 0:
            left, right = reference[-lag:], candidate[: length + lag]
        elif lag > 0:
            left, right = reference[: length - lag], candidate[lag:]
        else:
            left, right = reference, candidate
        if len(left) < 256:
            continue
        left = left - float(np.mean(left))
        right = right - float(np.mean(right))
        denominator = math.sqrt(float(np.dot(left, left)) * float(np.dot(right, right)))
        score = float(np.dot(left, right)) / denominator if denominator > 0 else 0.0
        if score > best[0]:
            best = (score, lag)
    return best


def sample_reaction_preservation(
    *,
    film_path: Path,
    output_path: Path,
    edl: RemixEdl,
    max_samples: int | None = None,
    timeline: RemixRenderTimeline | None = None,
    source_frame_count: int | None = None,
    output_frame_count: int | None = None,
) -> tuple[float, float, float, float]:
    measurement = measure_reaction_preservation(
        film_path=film_path,
        output_path=output_path,
        edl=edl,
        max_samples=max_samples,
        timeline=timeline,
        source_frame_count=source_frame_count,
        output_frame_count=output_frame_count,
    )
    return (
        measurement.min_audio_correlation,
        measurement.max_av_drift_ms,
        measurement.min_frame_similarity,
        measurement.max_gain_delta_db,
    )


def measure_reaction_preservation(
    *,
    film_path: Path,
    output_path: Path,
    edl: RemixEdl,
    max_samples: int | None = None,
    timeline: RemixRenderTimeline | None = None,
    source_frame_count: int | None = None,
    output_frame_count: int | None = None,
) -> ReactionPreservationMeasurement:
    # Preservation is a hard gate: every protected source-audio placement is sampled.
    placements = [item for item in edl.placements if item.kind in {"reaction", "mixed", "unknown"}]
    measurements: list[ReactionPlacementPreservation] = []
    frame_s = edl.output.fps_den / edl.output.fps_num
    frame_ms = 1000.0 * edl.output.fps_den / edl.output.fps_num
    max_lag = round(16000 * frame_ms / 1000.0)
    timeline_by_placement = (
        {item.placement_id: item for item in timeline.placements}
        if timeline is not None
        else {}
    )
    for placement in placements:
        correlations: list[float] = []
        lags_ms: list[float] = []
        similarities: list[float] = []
        gain_deltas: list[float] = []
        quantized = timeline_by_placement.get(placement.placement_id)
        if timeline is not None and quantized is None:
            raise RemixQaError(f"render timeline is missing placement {placement.placement_id}")
        if quantized is not None:
            source_audio_start = quantized.src_start_sample / timeline.audio_sample_rate
            output_audio_start = quantized.tl_start_sample / timeline.audio_sample_rate
            audio_duration = min(
                quantized.src_end_sample - quantized.src_start_sample,
                quantized.tl_end_sample - quantized.tl_start_sample,
            ) / timeline.audio_sample_rate
            available_source_frames = (
                max(0, source_frame_count - quantized.src_start_frame)
                if source_frame_count is not None
                else None
            )
            available_output_frames = (
                max(0, output_frame_count - quantized.tl_start_frame)
                if output_frame_count is not None
                else None
            )
            frame_probes = _quantized_frame_probes(
                quantized,
                timeline.fps_num,
                timeline.fps_den,
                available_source_frames=available_source_frames,
                available_output_frames=available_output_frames,
            )
        else:
            audio_duration = placement.tl_end - placement.tl_start
            source_audio_start = placement.audio.source_in or 0.0
            output_audio_start = placement.tl_start
            probes = _preservation_probes(audio_duration, frame_s=frame_s)
            frame_probes = tuple(
                (placement.video.src_in + frame_offset, placement.tl_start + frame_offset)
                for _offset, _duration, frame_offset in probes
            )
        audio_windows = _audio_probe_windows(audio_duration)
        source_placement_audio = decode_audio(
            film_path,
            start_s=source_audio_start,
            duration_s=audio_duration,
        )
        output_placement_audio = decode_audio(
            output_path,
            start_s=output_audio_start,
            duration_s=audio_duration,
        )
        for (offset_s, duration_s), (source_frame_tc, output_frame_tc) in zip(audio_windows, frame_probes):
            source_audio = _slice_audio(source_placement_audio, offset_s=offset_s, duration_s=duration_s)
            output_audio = _slice_audio(output_placement_audio, offset_s=offset_s, duration_s=duration_s)
            correlation, lag = best_correlation(source_audio, output_audio, max_lag_samples=max_lag)
            correlations.append(correlation)
            lags_ms.append(abs(lag) * 1000.0 / 16000)
            left, right = _aligned_for_lag(source_audio, output_audio, lag)
            gain_deltas.append(_gain_delta_db(left, right))
            similarities.append(
                frame_similarity(
                    film_path,
                    source_frame_tc,
                    output_path,
                    output_frame_tc,
                )
            )
        measurements.append(
            ReactionPlacementPreservation(
                placement_id=placement.placement_id,
                min_audio_correlation=min(correlations),
                max_av_drift_ms=max(lags_ms),
                min_frame_similarity=min(similarities),
                max_gain_delta_db=max(gain_deltas),
            )
        )
    return ReactionPreservationMeasurement(placements=tuple(measurements))


def _preservation_probes(duration_s: float, *, frame_s: float) -> tuple[tuple[float, float, float], ...]:
    edge_duration = min(PRESERVATION_EDGE_WINDOW_S, duration_s)
    middle_duration = min(PRESERVATION_MIDDLE_WINDOW_S, duration_s)
    middle_offset = max(0.0, (duration_s - middle_duration) / 2)
    edge_frame_offset = min(frame_s, duration_s / 2)
    return (
        (0.0, edge_duration, edge_frame_offset),
        (middle_offset, middle_duration, duration_s / 2),
        (max(0.0, duration_s - edge_duration), edge_duration, max(0.0, duration_s - edge_frame_offset)),
    )


def _audio_probe_windows(duration_s: float) -> tuple[tuple[float, float], ...]:
    edge_duration = min(PRESERVATION_EDGE_WINDOW_S, duration_s)
    middle_duration = min(PRESERVATION_MIDDLE_WINDOW_S, duration_s)
    return (
        (0.0, edge_duration),
        (max(0.0, (duration_s - middle_duration) / 2), middle_duration),
        (max(0.0, duration_s - edge_duration), edge_duration),
    )


def _slice_audio(
    samples: np.ndarray,
    *,
    offset_s: float,
    duration_s: float,
    sample_rate: int = 16000,
) -> np.ndarray:
    start = max(0, round(offset_s * sample_rate))
    end = min(len(samples), start + max(0, round(duration_s * sample_rate)))
    return samples[start:end]


def _quantized_frame_probes(
    placement: RemixRenderTimelinePlacement,
    fps_num: int,
    fps_den: int,
    *,
    available_source_frames: int | None = None,
    available_output_frames: int | None = None,
) -> tuple[tuple[float, float], ...]:
    frame_count = min(
        placement.src_end_frame - placement.src_start_frame,
        placement.tl_end_frame - placement.tl_start_frame,
    )
    if available_source_frames is not None:
        frame_count = min(frame_count, available_source_frames)
    if available_output_frames is not None:
        frame_count = min(frame_count, available_output_frames)
    if frame_count <= 0:
        raise RemixQaError(f"placement {placement.placement_id} has no decodable preservation frame")
    offsets = (min(1, frame_count - 1), frame_count // 2, max(0, frame_count - 1))
    frame_s = fps_den / fps_num
    return tuple(
        (
            (placement.src_start_frame + offset) * frame_s,
            (placement.tl_start_frame + offset) * frame_s,
        )
        for offset in offsets
    )


def _aligned_for_lag(reference: np.ndarray, candidate: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray]:
    length = min(len(reference), len(candidate))
    if lag < 0:
        return reference[-lag:length], candidate[: length + lag]
    if lag > 0:
        return reference[: length - lag], candidate[lag:length]
    return reference[:length], candidate[:length]


def _rms(samples: np.ndarray) -> float:
    return math.sqrt(float(np.mean(samples * samples))) if len(samples) else 0.0


def _gain_delta_db(reference: np.ndarray, candidate: np.ndarray) -> float:
    reference_rms = _rms(reference)
    candidate_rms = _rms(candidate)
    reference_silent = reference_rms <= SILENCE_RMS_THRESHOLD
    candidate_silent = candidate_rms <= SILENCE_RMS_THRESHOLD
    if reference_silent and candidate_silent:
        return 0.0
    if reference_silent != candidate_silent:
        return ONE_SIDED_SILENCE_GAIN_DELTA_DB
    return abs(20.0 * math.log10(candidate_rms / reference_rms))


def _frame_png(path: Path, timestamp: float) -> np.ndarray | None:
    # Preservation QA compares CFR source-compatible renders at exact frame
    # positions. FFmpeg input seeking near H.264/concat clip boundaries can land
    # on an adjacent keyframe, producing false visual failures even when the
    # quantized output frame is correct. Prefer frame-index reads and keep
    # ffmpeg as a fallback for unusual containers/codecs.
    frame = _frame_by_index(path, timestamp)
    if frame is not None:
        return frame
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    return cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)


def _frame_by_index(path: Path, timestamp: float) -> np.ndarray | None:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return None
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0.0:
            return None
        frame_index = max(0, round(timestamp * fps))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count > 0:
            frame_index = min(frame_index, frame_count - 1)
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok or frame is None:
            return None
        return frame
    finally:
        capture.release()


def frame_similarity(source: Path, source_tc: float, output: Path, output_tc: float) -> float:
    left = _frame_png(source, source_tc)
    right = _frame_png(output, output_tc)
    if left is None or right is None:
        return 0.0
    if left.shape != right.shape:
        right = cv2.resize(right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA)
    difference = np.mean(np.abs(left.astype(np.float32) - right.astype(np.float32))) / 255.0
    return max(0.0, 1.0 - float(difference))


def write_boundary_frames(
    *,
    film_path: Path,
    output_path: Path,
    edl: RemixEdl,
    qa_dir: Path,
    timeline: RemixRenderTimeline | None = None,
    source_frame_count: int | None = None,
    output_frame_count: int | None = None,
) -> BoundaryFrameMeasurement:
    frame_dir = qa_dir / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_s = edl.output.fps_den / edl.output.fps_num
    similarities_by_placement: dict[str, list[float]] = {}
    protected_kinds = {"reaction", "mixed", "unknown"}
    timeline_by_placement = (
        {item.placement_id: item for item in timeline.placements}
        if timeline is not None
        else {}
    )
    for index in range(1, len(edl.placements)):
        left = edl.placements[index - 1]
        right = edl.placements[index]
        probes: list[tuple[str, str, float, float]] = []
        for label, placement, side in (("before", left, "tail"), ("after", right, "head")):
            if placement.kind not in protected_kinds:
                continue
            quantized = timeline_by_placement.get(placement.placement_id)
            if timeline is not None and quantized is None:
                raise RemixQaError(f"render timeline is missing placement {placement.placement_id}")
            if quantized is not None:
                frame_count = min(
                    quantized.src_end_frame - quantized.src_start_frame,
                    quantized.tl_end_frame - quantized.tl_start_frame,
                )
                if source_frame_count is not None:
                    frame_count = min(frame_count, max(0, source_frame_count - quantized.src_start_frame))
                if output_frame_count is not None:
                    frame_count = min(frame_count, max(0, output_frame_count - quantized.tl_start_frame))
                if frame_count <= 0:
                    raise RemixQaError(
                        f"placement {placement.placement_id} has no decodable boundary frame"
                    )
                offset = max(0, frame_count - 1) if side == "tail" else min(1, frame_count - 1)
                quantized_frame_s = timeline.fps_den / timeline.fps_num
                source_tc = (quantized.src_start_frame + offset) * quantized_frame_s
                output_tc = (quantized.tl_start_frame + offset) * quantized_frame_s
            elif side == "tail":
                source_tc = max(placement.video.src_in, placement.video.src_out - frame_s)
                output_tc = max(0.0, placement.tl_end - frame_s)
            else:
                source_tc = min(placement.video.src_out - 1e-6, placement.video.src_in + frame_s)
                output_tc = placement.tl_start + frame_s
            probes.append((placement.placement_id, label, source_tc, output_tc))
        for placement_id, label, source_tc, output_tc in probes:
            source_frame = _frame_png(film_path, source_tc)
            output_frame = _frame_png(output_path, output_tc)
            if source_frame is None or output_frame is None:
                similarities_by_placement.setdefault(placement_id, []).append(0.0)
                continue
            if source_frame.shape != output_frame.shape:
                output_frame = cv2.resize(output_frame, (source_frame.shape[1], source_frame.shape[0]), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(frame_dir / f"boundary-{index:04d}-{label}-source.jpg"), source_frame)
            cv2.imwrite(str(frame_dir / f"boundary-{index:04d}-{label}-output.jpg"), output_frame)
            difference = np.mean(np.abs(source_frame.astype(np.float32) - output_frame.astype(np.float32))) / 255.0
            similarities_by_placement.setdefault(placement_id, []).append(
                max(0.0, 1.0 - float(difference))
            )
    for placement in edl.placements:
        if placement.kind != "commentary":
            continue
        quantized = timeline_by_placement.get(placement.placement_id)
        if timeline is not None and quantized is None:
            raise RemixQaError(f"render timeline is missing placement {placement.placement_id}")
        if quantized is not None:
            frame_count = min(
                quantized.src_end_frame - quantized.src_start_frame,
                quantized.tl_end_frame - quantized.tl_start_frame,
            )
            offset = frame_count // 2
            quantized_frame_s = timeline.fps_den / timeline.fps_num
            source_tc = (quantized.src_start_frame + offset) * quantized_frame_s
            output_tc = (quantized.tl_start_frame + offset) * quantized_frame_s
        else:
            duration = placement.tl_end - placement.tl_start
            source_tc = placement.video.src_in + duration / 2
            output_tc = placement.tl_start + duration / 2
        source_frame = _frame_png(film_path, source_tc)
        output_frame = _frame_png(output_path, output_tc)
        if source_frame is None or output_frame is None:
            continue
        if source_frame.shape != output_frame.shape:
            output_frame = cv2.resize(output_frame, (source_frame.shape[1], source_frame.shape[0]), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(frame_dir / f"{placement.placement_id}-inside-source.jpg"), source_frame)
        cv2.imwrite(str(frame_dir / f"{placement.placement_id}-inside-output.jpg"), output_frame)
    return BoundaryFrameMeasurement(
        placement_similarities=tuple(
            (placement_id, min(similarities))
            for placement_id, similarities in sorted(similarities_by_placement.items())
        )
    )


def commentary_leakage_placement_ids(film_path: Path, output_path: Path, edl: RemixEdl) -> list[str]:
    placement_ids: list[str] = []
    for placement in edl.placements:
        if placement.kind != "commentary":
            continue
        duration = min(2.0, placement.tl_end - placement.tl_start)
        source_audio = decode_audio(film_path, start_s=placement.video.src_in, duration_s=duration)
        output_audio = decode_audio(output_path, start_s=placement.tl_start, duration_s=duration)
        correlation, _lag = best_correlation(source_audio, output_audio, max_lag_samples=320)
        if correlation >= 0.80:
            placement_ids.append(placement.placement_id)
    return placement_ids


def narrator_phrase_leakage_placement_ids(
    *,
    output_path: Path,
    edl: RemixEdl,
    transcript: ReactionTranscript,
    blocks: ReactionBlocks,
    plan: RemixPlan,
    script: CommentaryScript,
    work_dir: Path,
    model_name: str,
    device: str,
) -> list[str]:
    from faster_whisper import WhisperModel

    turns = {turn.turn_id: turn for turn in transcript.turns}
    source_phrases = [
        turns[turn_id].text
        for block in blocks.blocks
        if block.kind == "commentary"
        for turn_id in block.turn_ids
        if turn_id in turns
    ]
    plan_slots = {item.item_id: item.slot_id for item in plan.items if item.kind == "commentary_slot"}
    expected_by_slot = {slot.slot_id: slot.text_ja for slot in script.slots}
    if not source_phrases:
        return []
    model = WhisperModel(model_name, device=device)
    work_dir.mkdir(parents=True, exist_ok=True)
    placement_ids: list[str] = []
    for placement in edl.placements:
        if placement.kind != "commentary":
            continue
        clip = work_dir / f"{placement.placement_id}.wav"
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                f"{placement.tl_start:.6f}",
                "-i",
                str(output_path),
                "-t",
                f"{placement.tl_end - placement.tl_start:.6f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(clip),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0 or not clip.is_file():
            raise RemixQaError(f"could not prepare narrator leakage sample for {placement.placement_id}")
        segments, _info = model.transcribe(str(clip), language="ja", vad_filter=True, condition_on_previous_text=False)
        observed = " ".join(str(segment.text).strip() for segment in segments).strip()
        normalized_observed = _normalize_text(observed)
        if not normalized_observed:
            continue
        expected = _normalize_text(expected_by_slot.get(plan_slots.get(placement.item_id) or "", ""))
        expected_similarity = SequenceMatcher(None, normalized_observed, expected).ratio() if expected else 0.0
        source_similarity = max(
            SequenceMatcher(None, normalized_observed, _normalize_text(phrase)).ratio() for phrase in source_phrases
        )
        if source_similarity >= 0.55 and source_similarity > expected_similarity + 0.10:
            placement_ids.append(placement.placement_id)
    return placement_ids


def _normalize_text(value: str) -> str:
    return re.sub(r"\W+", "", value.casefold(), flags=re.UNICODE)


def boundary_audio_defects(output_path: Path, edl: RemixEdl) -> tuple[int, int]:
    samples = decode_audio(output_path)
    if len(samples) < 2:
        return 1, 0
    sample_rate = 16000
    silence_count = 0
    click_count = 0
    for placement in edl.placements[:-1]:
        boundary = round(placement.tl_end * sample_rate)
        if boundary <= sample_rate // 4 or boundary >= len(samples) - sample_rate // 4:
            continue
        left = samples[boundary - 1]
        right = samples[boundary]
        local = samples[boundary - 160 : boundary + 160]
        local_rms = math.sqrt(float(np.mean(local * local))) if len(local) else 0.0
        if abs(float(right - left)) > max(12000.0, local_rms * 12.0):
            click_count += 1
        window = samples[boundary - 2000 : boundary + 2000]
        if len(window) >= 4000 and math.sqrt(float(np.mean(window * window))) < 8.0:
            silence_count += 1
    return silence_count, click_count


def measured_peak_dbfs(path: Path, *, start_s: float | None = None, duration_s: float | None = None) -> float | None:
    args = ["ffmpeg", "-hide_banner", "-nostats"]
    if start_s is not None:
        args.extend(["-ss", f"{start_s:.6f}"])
    args.extend(["-i", str(path)])
    if duration_s is not None:
        args.extend(["-t", f"{duration_s:.6f}"])
    args.extend(["-af", "ebur128=peak=true", "-f", "null", "-"])
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    matches = re.findall(r"Peak:\s+(-?\d+(?:\.\d+)?)\s+dBFS", result.stderr or "")
    return float(matches[-1]) if matches else None


def program_peak_dbfs(path: Path) -> float:
    measured = measured_peak_dbfs(path)
    if measured is not None:
        return measured
    samples = decode_audio(path)
    if not len(samples):
        return -120.0
    ratio = float(np.max(np.abs(samples))) / 32768.0
    return 20.0 * math.log10(max(ratio, 1e-12))


def commentary_peak_dbfs(output_path: Path, edl: RemixEdl) -> float | None:
    peaks: list[float] = []
    for placement in edl.placements:
        if placement.kind != "commentary":
            continue
        peak = measured_peak_dbfs(
            output_path,
            start_s=placement.tl_start,
            duration_s=placement.tl_end - placement.tl_start,
        )
        if peak is not None:
            peaks.append(peak)
    return max(peaks) if peaks else None


def visual_operation_counts(manifest: RemixCommandManifest) -> dict[str, int]:
    text = " ".join(arg.lower() for command in manifest.commands for arg in command.args)
    return {
        "mask_operations": sum(text.count(name) for name in ("maskedmerge=", "alphamerge=", "delogo=")),
        "subtitle_additions": text.count("subtitles=") + text.count("ass="),
        "text_overlays": text.count("drawtext="),
        "blur_operations": text.count("boxblur=") + text.count("gblur="),
        "other_overlays": text.count("overlay="),
    }


def declared_reaction_mismatches(edl: RemixEdl) -> tuple[int, int, int, int]:
    reaction = [item for item in edl.placements if item.kind in {"reaction", "mixed", "unknown"}]
    speed = sum(item.video.speed != 1.0 for item in reaction)
    gain = sum(item.audio.source_gain_db != 0.0 for item in reaction)
    span = sum(
        item.audio.mode != "source"
        or item.audio.source_in != item.video.src_in
        or item.audio.source_out != item.video.src_out
        for item in reaction
    )
    return len(reaction), speed, gain, span


def declared_reaction_mismatch_placement_ids(edl: RemixEdl) -> list[str]:
    return [
        item.placement_id
        for item in edl.placements
        if item.kind in {"reaction", "mixed", "unknown"}
        and (
            item.video.speed != 1.0
            or item.audio.source_gain_db != 0.0
            or item.audio.mode != "source"
            or item.audio.source_in != item.video.src_in
            or item.audio.source_out != item.video.src_out
        )
    ]


def decoded_video_frame_count(path: Path) -> int:
    frame_result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if frame_result.returncode != 0:
        raise RemixQaError((frame_result.stderr or "").strip() or "could not count decoded video frames")
    try:
        return int(frame_result.stdout.strip())
    except ValueError as exc:
        raise RemixQaError("ffprobe did not report a decoded video frame count") from exc


def decoded_media_counts(path: Path, *, audio_channels: int) -> tuple[int, int]:
    frame_count = decoded_video_frame_count(path)
    if audio_channels <= 0:
        raise RemixQaError("audio channel count must be positive")
    process = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    byte_count = 0
    while True:
        chunk = process.stdout.read(1024 * 1024)
        if not chunk:
            break
        byte_count += len(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    return_code = process.wait()
    if return_code != 0:
        raise RemixQaError(stderr.decode("utf-8", errors="replace").strip() or "could not count decoded audio samples")
    bytes_per_sample_frame = 2 * audio_channels
    if byte_count % bytes_per_sample_frame:
        raise RemixQaError("decoded PCM byte count is not aligned to the output channel count")
    return frame_count, byte_count // bytes_per_sample_frame


def commentary_provenance(commentary_audio: CommentaryAudio) -> tuple[int, int, int, float]:
    provider_mismatches = sum(item.provider != commentary_audio.voice_policy.provider for item in commentary_audio.items)
    voice_mismatches = sum(item.voice_id != commentary_audio.voice_policy.voice_id for item in commentary_audio.items)
    matches = [item.asr_text_match for item in commentary_audio.items if item.asr_text_match is not None]
    return len(commentary_audio.items), provider_mismatches, voice_mismatches, min(matches) if matches else 0.0
