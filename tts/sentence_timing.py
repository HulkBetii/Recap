"""Sentence-level timing of the rendered voiceover (G3 sidecar `sentence_timing.json`).

G5 places footage per narration sentence, so it needs to know when each sentence is actually spoken.
A first estimate splits each beat's audio in proportion to the letters of its sentences (after TTS
text normalization, sentence by sentence, so the sentence count always matches the narration). Each
estimated boundary is then snapped to the nearest real pause in the audio: TTS voices pause 0.5-0.7 s
at a sentence end and 0.2-0.35 s at a comma, so the longest nearby silence marks the boundary.

WhisperX forced alignment was tried first and rejected: on a whole 30 s beat it compressed every
sentence into the first seconds of the audio (the same failure AGENTS.md section 18 records for ASR).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from common.media import detect_silences
from common.schema import BeatTiming, ReviewBeat, SentenceTimestamp
from common.text import split_sentences
from tts.sanitize import NormalizationMode, normalize_tts_text

SentenceTimingMode = Literal["off", "proportional", "silence"]
SENTENCE_TIMING_VERSION = "sentence-timing-v1"
# Shortest window a sentence may get.
MIN_SENTENCE_S = 0.25
# Character-equivalent weight of the pause a voice leaves after a sentence.
SENTENCE_PAUSE_WEIGHT = 6
# A boundary snaps to a silence at most this far from its estimate (and at most half-way to a neighbour).
SNAP_WINDOW_S = 2.5
# Score = silence length - penalty * distance from the estimate: a 0.6 s pause 2 s away still beats a
# 0.25 s comma pause right at the estimate.
SNAP_DISTANCE_PENALTY = 0.1

SilenceDetector = Callable[[Path], list[tuple[float, float]]]


def sentence_tts_texts(narration: str, *, mode: NormalizationMode, lexicon: dict[str, str] | None) -> list[str]:
    texts: list[str] = []
    for sentence in split_sentences(narration):
        try:
            texts.append(normalize_tts_text(sentence, mode=mode, lexicon=lexicon).tts_text)
        except ValueError:
            # A sentence that normalizes to nothing (emoji/URL only) is not spoken.
            texts.append("")
    return texts


def proportional_starts(texts: list[str], duration: float) -> list[float]:
    """Start offset of every sentence when speech time is proportional to its letters plus a sentence pause."""
    weights = [sum(character.isalnum() for character in text) + SENTENCE_PAUSE_WEIGHT for text in texts]
    total = sum(weights)
    starts: list[float] = []
    consumed = 0
    for weight in weights:
        starts.append(duration * consumed / total)
        consumed += weight
    return starts


def snap_starts(estimates: list[float], silences: list[tuple[float, float]], duration: float) -> tuple[list[float], int]:
    """Move each estimated sentence start to the end of the best nearby silence; returns starts and snap count."""
    starts = [0.0]
    used: set[int] = set()
    snapped = 0
    for index in range(1, len(estimates)):
        estimate = estimates[index]
        neighbours = [estimate - estimates[index - 1]]
        if index + 1 < len(estimates):
            neighbours.append(estimates[index + 1] - estimate)
        window = min(SNAP_WINDOW_S, 0.5 * min(neighbours))
        best: tuple[float, int, float] | None = None
        for position, (silence_start, silence_end) in enumerate(silences):
            if position in used or silence_end <= starts[-1] + MIN_SENTENCE_S or silence_end >= duration - MIN_SENTENCE_S:
                continue
            distance = abs(silence_end - estimate)
            if distance > window:
                continue
            score = (silence_end - silence_start) - SNAP_DISTANCE_PENALTY * distance
            if best is None or score > best[0]:
                best = (score, position, silence_end)
        if best is None:
            starts.append(max(estimate, starts[-1] + MIN_SENTENCE_S))
        else:
            used.add(best[1])
            starts.append(best[2])
            snapped += 1
    return starts, snapped


def well_formed(starts: list[float], duration: float) -> bool:
    bounds = [*starts, duration]
    return all(later - earlier >= MIN_SENTENCE_S for earlier, later in zip(bounds, bounds[1:]))


def beat_sentence_timing(
    timing: BeatTiming,
    starts: list[float],
    method: Literal["silence", "proportional"],
) -> list[SentenceTimestamp]:
    """Sentence windows tile the beat; the pause after a sentence belongs to that sentence."""
    boundaries = [round(timing.tl_start + offset, 3) for offset in starts] + [round(timing.tl_end, 3)]
    boundaries[0] = round(timing.tl_start, 3)
    return [
        SentenceTimestamp(beat_id=timing.beat_id, idx=index, tl_start=boundaries[index], tl_end=boundaries[index + 1], method=method)
        for index in range(len(starts))
    ]


def cache_key(audio_path: Path, texts: list[str], mode: SentenceTimingMode) -> str:
    digest = hashlib.sha256()
    digest.update(SENTENCE_TIMING_VERSION.encode())
    digest.update(mode.encode())
    digest.update(audio_path.read_bytes())
    digest.update(json.dumps(texts, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def build_sentence_timing(
    *,
    beats: list[ReviewBeat],
    timings: list[BeatTiming],
    audio_dir: Path,
    text_mode: NormalizationMode,
    lexicon: dict[str, str] | None,
    mode: SentenceTimingMode,
    silence_detector: SilenceDetector = detect_silences,
    cache_path: Path | None = None,
    logger: logging.Logger | None = None,
) -> tuple[list[SentenceTimestamp], list[str], int]:
    """Return sentence windows for every beat, warnings, and the number of beats whose boundaries all snapped."""
    log = logger or logging.getLogger("tts")
    cache: dict[str, dict] = {}
    if cache_path is not None and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    timing_by_beat = {timing.beat_id: timing for timing in timings}
    warnings: list[str] = []
    stamps: list[SentenceTimestamp] = []
    n_snapped = 0
    new_cache: dict[str, dict] = {}
    for beat in beats:
        timing = timing_by_beat[beat.beat_id]
        texts = sentence_tts_texts(beat.narration, mode=text_mode, lexicon=lexicon)
        audio_path = audio_dir / f"{beat.beat_id}.mp3"
        key = cache_key(audio_path, texts, mode)
        cached = cache.get(key)
        if cached is not None:
            starts, method = list(cached["starts"]), cached["method"]
        else:
            starts = proportional_starts(texts, timing.duration)
            method: Literal["silence", "proportional"] = "proportional"
            if mode == "silence" and len(texts) > 1:
                try:
                    silences = silence_detector(audio_path)
                except Exception as exc:  # noqa: BLE001 - one unreadable beat keeps its proportional estimate
                    warnings.append(f"beat {beat.beat_id}: silence detection failed: {exc}")
                    log.warning(warnings[-1])
                else:
                    snapped_starts, snapped = snap_starts(starts, silences, timing.duration)
                    if well_formed(snapped_starts, timing.duration):
                        starts = snapped_starts
                        if snapped == len(texts) - 1:
                            method = "silence"
                        else:
                            warnings.append(f"beat {beat.beat_id}: {len(texts) - 1 - snapped} sentence boundary(ies) had no nearby pause")
        new_cache[key] = {"starts": starts, "method": method}
        n_snapped += method == "silence"
        stamps.extend(beat_sentence_timing(timing, starts, method))
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(new_cache, ensure_ascii=False), encoding="utf-8")
    return stamps, warnings, n_snapped
