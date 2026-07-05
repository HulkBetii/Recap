from __future__ import annotations

from common.schema import FilmMapSegment, ReviewBeat
from review.non_story import drop_non_story_beats


def seg(idx: int, start: float, end: float, text: str, type_: str = "speech") -> FilmMapSegment:
    if type_ == "visual":
        return FilmMapSegment(id=idx, type="visual", tc_start=start, tc_end=end, ko=None, en=None, scene_desc=text)
    return FilmMapSegment(id=idx, type="speech", tc_start=start, tc_end=end, ko="ko", en=text, scene_desc=None)


def beat(idx: int, start: float, end: float, narration: str, hook: bool = False) -> ReviewBeat:
    return ReviewBeat(beat_id=idx, narration=narration, from_seg_id=idx, to_seg_id=idx, src_tc_start=start, src_tc_end=end, is_hook=hook)


def test_drop_credit_outro_beat_and_reassign_ids() -> None:
    film_map = [
        seg(0, 0, 10, "hero fights demon"),
        seg(1, 100, 110, "end credits black screen white text", "visual"),
    ]
    beats = [
        beat(0, 0, 10, "Anh hùng đánh bại con quỷ.", hook=True),
        beat(1, 100, 110, "Phần còn lại là credit và thông tin sản xuất, màn hình chuyển sang nền đen."),
    ]

    filtered, report = drop_non_story_beats(beats, film_map, duration_s=120, tail_s=40)

    assert [item.beat_id for item in filtered] == [0]
    assert report.dropped_beat_ids == [1]
    assert report.warnings


def test_keep_real_plot_ending_near_tail() -> None:
    film_map = [seg(0, 90, 110, "Eunseo fights the demon and survives with blood on her face")]
    beats = [beat(0, 90, 110, "Eunseo đối đầu ác linh trong nghi lễ cuối cùng và sống sót.")]

    filtered, report = drop_non_story_beats(beats, film_map, duration_s=120, tail_s=40)

    assert len(filtered) == 1
    assert report.dropped_beat_ids == []


def test_hook_credit_like_beat_is_kept_with_warning() -> None:
    film_map = [seg(0, 100, 110, "end credits black screen white text", "visual")]
    beats = [beat(0, 100, 110, "Credit hiện trên nền đen.", hook=True)]

    filtered, report = drop_non_story_beats(beats, film_map, duration_s=120, tail_s=40)

    assert len(filtered) == 1
    assert report.dropped_beat_ids == []
    assert "hook beat" in report.warnings[0]
