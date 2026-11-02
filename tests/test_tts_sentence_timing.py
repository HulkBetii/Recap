from __future__ import annotations

from pathlib import Path

from common.schema import BeatTiming, ReviewBeat, validate_sentence_timing
from common.text import split_sentences
from tts.sentence_timing import build_sentence_timing, proportional_starts, sentence_tts_texts, snap_starts


def test_sentence_texts_keep_sentence_count_when_normalization_rewrites_dots() -> None:
    narration = "Hệ thống A.I. bị lỗi. Anh bỏ chạy!"
    texts = sentence_tts_texts(narration, mode="vi", lexicon={})
    # Per-sentence normalization turns "A.I." into letters, but indexes still follow the shared splitter.
    assert len(texts) == len(split_sentences(narration)) and "A.I." not in " ".join(texts)


def test_snap_prefers_sentence_pause_over_nearby_comma_pause() -> None:
    estimates = [0.0, 6.0, 12.0]
    silences = [(5.9, 6.1), (5.2, 5.8), (11.0, 11.6)]  # comma pause right at 6.0, longer sentence pause at 5.8
    starts, snapped = snap_starts(estimates, silences, 18.0)
    assert snapped == 2 and starts == [0.0, 5.8, 11.6]


def test_build_tiles_each_beat_and_falls_back_when_detection_fails(tmp_path: Path) -> None:
    beats = [
        ReviewBeat(beat_id=0, narration="Câu một ngắn. Câu hai dài hơn một chút!", from_seg_id=0, to_seg_id=1, src_tc_start=0.0, src_tc_end=5.0),
        ReviewBeat(beat_id=1, narration="Chỉ một câu.", from_seg_id=2, to_seg_id=3, src_tc_start=5.0, src_tc_end=9.0),
        ReviewBeat(beat_id=2, narration="Câu ba. Câu bốn.", from_seg_id=4, to_seg_id=5, src_tc_start=9.0, src_tc_end=12.0),
    ]
    timings = [
        BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0.0, tl_end=6.0, duration=6.0),
        BeatTiming(beat_id=1, audio_path="audio/1.mp3", tl_start=6.4, tl_end=8.4, duration=2.0),
        BeatTiming(beat_id=2, audio_path="audio/2.mp3", tl_start=8.8, tl_end=12.8, duration=4.0),
    ]
    for index in range(3):
        (tmp_path / f"{index}.mp3").write_bytes(f"audio {index}".encode())

    def detector(path: Path) -> list[tuple[float, float]]:
        if path.name == "2.mp3":
            raise RuntimeError("broken file")
        return [(2.0, 2.6)]

    stamps, warnings, n_snapped = build_sentence_timing(
        beats=beats, timings=timings, audio_dir=tmp_path, text_mode="vi", lexicon={}, mode="silence", silence_detector=detector
    )
    validate_sentence_timing(stamps, timings)
    assert [(stamp.beat_id, stamp.idx, stamp.tl_start, stamp.method) for stamp in stamps] == [
        (0, 0, 0.0, "silence"),
        (0, 1, 2.6, "silence"),
        (1, 0, 6.4, "proportional"),
        (2, 0, 8.8, "proportional"),
        (2, 1, round(8.8 + proportional_starts(["Câu ba.", "Câu bốn."], 4.0)[1], 3), "proportional"),
    ]
    assert n_snapped == 1 and any("silence detection failed" in warning for warning in warnings)
