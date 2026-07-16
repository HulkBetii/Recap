from __future__ import annotations

from common.schema import FilmMapSegment, ReviewBeat
from review.micro_beats import DEFAULT_HARD_MAX_AUDIO_S, estimate_audio_s, split_long_beats


def seg(idx: int) -> FilmMapSegment:
    return FilmMapSegment(id=idx, type="speech", tc_start=idx * 10.0, tc_end=idx * 10.0 + 10.0, ko="x", en=f"segment {idx}", scene_desc=None)

def span_seg(idx: int, start: float, end: float) -> FilmMapSegment:
    return FilmMapSegment(id=idx, type="speech", tc_start=start, tc_end=end, ko="x", en=f"segment {idx}", scene_desc=None)


def test_split_long_movie_beat_into_many_micro_beats() -> None:
    film_map = [seg(i) for i in range(40)]
    narration = " ".join(
        f"Cau {index} mo ta mot bien co lon trong cau chuyen va day cau chuyen tien len."
        for index in range(30)
    )
    beat = ReviewBeat(
        beat_id=0,
        narration=narration,
        from_seg_id=0,
        to_seg_id=29,
        src_tc_start=0,
        src_tc_end=300,
        is_hook=False,
    )

    beats, report = split_long_beats([beat], film_map, max_audio_s=18, target_audio_s=12, tts_cps=10, enabled=True)

    assert len(beats) > 3
    assert [item.beat_id for item in beats] == list(range(len(beats)))
    assert 0 in report.split_beat_ids
    assert report.n_split_beats == 1
    assert report.max_est_beat_audio_s <= DEFAULT_HARD_MAX_AUDIO_S
    assert all(estimate_audio_s(item.narration, 10) <= DEFAULT_HARD_MAX_AUDIO_S for item in beats)
    assert beats[0].src_tc_start == 0
    assert beats[-1].src_tc_end == 300
    assert all(beats[index].to_seg_id < beats[index + 1].from_seg_id for index in range(len(beats) - 1))


def test_split_keeps_source_spans_monotonic_and_from_film_map() -> None:
    film_map = [seg(i) for i in range(8)]
    beat = ReviewBeat(
        beat_id=3,
        narration="Mot cau mo dau day du y nghia. Mot cau tiep theo day cau chuyen di tiep. Mot cau cuoi khoa lai cao trao.",
        from_seg_id=1,
        to_seg_id=6,
        src_tc_start=10,
        src_tc_end=70,
        is_hook=False,
    )

    beats, _report = split_long_beats([beat], film_map, max_audio_s=4, target_audio_s=3, tts_cps=12, enabled=True)

    assert [item.beat_id for item in beats] == list(range(len(beats)))
    assert beats[0].from_seg_id == 1
    assert beats[-1].to_seg_id == 6
    for item in beats:
        assert item.src_tc_start == film_map[item.from_seg_id].tc_start
        assert item.src_tc_end == film_map[item.to_seg_id].tc_end

def test_source_dense_short_beat_splits_and_prefers_dominant_tail_segment() -> None:
    film_map = [
        span_seg(0, 0, 2),
        span_seg(1, 2, 5),
        span_seg(2, 5, 8),
        span_seg(3, 8, 45),
        span_seg(4, 45, 82),
    ]
    beat = ReviewBeat(
        beat_id=0,
        narration=(
            "Trong luc cau chuyen doi sang mot goc toi khac. "
            "Mot nguoi dan ong bi dam con do keo len tang cao. "
            "Chung bat dau ep nan nhan chuan bi mot ke hoach nguy hiem."
        ),
        from_seg_id=0,
        to_seg_id=4,
        src_tc_start=0,
        src_tc_end=82,
        is_hook=False,
    )

    beats, report = split_long_beats([beat], film_map, max_audio_s=18, target_audio_s=12, tts_cps=20, enabled=True)

    assert len(beats) >= 2
    assert 0 in report.split_beat_ids
    assert "source-dense" in report.warnings[0]
    assert beats[0].from_seg_id == 0
    assert beats[-1].from_seg_id == 4
    assert beats[-1].src_tc_start == 45


def test_splitter_keeps_hook_but_warns_when_too_long() -> None:
    film_map = [seg(i) for i in range(12)]
    hook = ReviewBeat(
        beat_id=0,
        narration=" ".join(f"Hook sentence {index} pushes the opening setup longer than it should be." for index in range(10)),
        from_seg_id=0,
        to_seg_id=11,
        src_tc_start=0,
        src_tc_end=120,
        is_hook=True,
    )

    beats, report = split_long_beats([hook], film_map, max_audio_s=18, target_audio_s=12, tts_cps=10, enabled=True)

    assert beats == [hook]
    assert report.n_split_beats == 0
    assert any("hook beat 0" in warning for warning in report.warnings)


def test_splitter_keeps_unsplittable_long_beat_with_warning() -> None:
    film_map = [seg(i) for i in range(2)]
    beat = ReviewBeat(
        beat_id=0,
        narration="This single long sentence has no safe sentence boundary so the splitter must keep it intact even though it is far too long for matching",
        from_seg_id=0,
        to_seg_id=1,
        src_tc_start=0,
        src_tc_end=20,
        is_hook=False,
    )

    beats, report = split_long_beats([beat], film_map, max_audio_s=4, target_audio_s=3, tts_cps=6, enabled=True)

    assert beats == [beat]
    assert report.n_split_beats == 0
    assert report.n_beats_over_max_audio == 1
    assert any("cannot split safely" in warning for warning in report.warnings)
