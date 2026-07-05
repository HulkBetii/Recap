from __future__ import annotations

from common.schema import FilmMapSegment, ReviewBeat
from review.micro_beats import split_long_beats


def seg(idx: int) -> FilmMapSegment:
    return FilmMapSegment(id=idx, type="speech", tc_start=idx * 10.0, tc_end=idx * 10.0 + 10.0, ko="x", en=f"segment {idx}", scene_desc=None)


def test_split_long_movie_beat_into_micro_beats() -> None:
    film_map = [seg(i) for i in range(6)]
    beat = ReviewBeat(
        beat_id=0,
        narration="M?t c?u m? ??u r?t r?. C?u th? hai k? s? ki?n ti?p theo. C?u th? ba chuy?n sang manh m?i m?i. C?u cu?i k?t l?i nh?p h?nh ??ng.",
        from_seg_id=0,
        to_seg_id=5,
        src_tc_start=0,
        src_tc_end=60,
        is_hook=False,
    )
    beats, report = split_long_beats([beat], film_map, max_audio_s=4, target_audio_s=2, tts_cps=10, enabled=True)
    assert len(beats) > 1
    assert [item.beat_id for item in beats] == list(range(len(beats)))
    assert beats[0].src_tc_start == 0
    assert beats[-1].src_tc_end == 60
    assert report.split_beat_ids == [0]


def test_splitter_keeps_hook_and_short_beat() -> None:
    film_map = [seg(i) for i in range(2)]
    hook = ReviewBeat(beat_id=0, narration="Hook ng?n nh?ng r?.", from_seg_id=0, to_seg_id=1, src_tc_start=0, src_tc_end=20, is_hook=True)
    beats, report = split_long_beats([hook], film_map, max_audio_s=1, target_audio_s=1, tts_cps=5, enabled=True)
    assert beats == [hook]
    assert report.n_split_beats == 0
