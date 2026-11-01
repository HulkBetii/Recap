from __future__ import annotations

from common.schema import FilmMapSegment, IntroDetection, NonStoryRange, VideoProfile, validate_story_map
from storymap.builder import build_story_sections


def make_segment(index: int, start: float, end: float, text: str = "story") -> FilmMapSegment:
    return FilmMapSegment(id=index, type="speech", tc_start=start, tc_end=end, ko="안녕", en=text, scene_desc=None)


def test_story_map_excludes_confident_non_story_intro() -> None:
    film_map = [make_segment(0, 0, 10, "credits"), make_segment(1, 12, 20, "John enters the house")]
    profile = VideoProfile(
        input_path="film.mp4",
        duration_s=20,
        intro=IntroDetection(detected=True, start_s=0, end_s=10, confidence=0.9, reasons=["opening"]),
        non_story_ranges=[NonStoryRange(start_s=0, end_s=10, label="intro_opening", confidence=0.9)],
        classifier="mock",
        created_at="2026-07-05T00:00:00Z",
    )
    sections, report = build_story_sections(film_map, duration_s=20, video_profile=profile)
    story_sections = [section for section in sections if section.type != "non_story"]
    assert story_sections[0].tc_start >= 10
    assert report.qa["n_non_story"] == 1


def test_story_map_sections_are_sorted_and_continuous_ids() -> None:
    film_map = [make_segment(i, i * 10, i * 10 + 5, f"Alice event {i}") for i in range(7)]
    sections, _report = build_story_sections(film_map, duration_s=70, video_profile=None)
    assert [section.section_id for section in sections] == list(range(len(sections)))
    assert sections == sorted(sections, key=lambda section: section.tc_start)

def test_story_map_clamps_section_end_to_duration() -> None:
    film_map = [make_segment(0, 0, 5, "Alice starts"), make_segment(1, 8, 10.2, "Alice ends")]
    sections, _report = build_story_sections(film_map, duration_s=10, video_profile=None, target_story_sections=2)
    assert max(section.tc_end for section in sections) <= 10


def test_story_map_splits_bucket_at_non_story_gap() -> None:
    film_map = [make_segment(0, 0, 5), make_segment(1, 10, 15)]
    profile = VideoProfile(
        input_path="film.mp4",
        duration_s=15,
        intro=IntroDetection(detected=False, confidence=0, reasons=[]),
        non_story_ranges=[NonStoryRange(start_s=5, end_s=10, label="opening_theme", confidence=1.0)],
        classifier="mock",
        created_at="2026-07-30T00:00:00Z",
    )

    sections, _report = build_story_sections(
        film_map,
        duration_s=15,
        video_profile=profile,
        target_story_sections=1,
    )

    validate_story_map(sections, duration=15)
    assert [(section.type, section.tc_start, section.tc_end) for section in sections] == [
        ("setup", 0.0, 5.0),
        ("non_story", 5.0, 10.0),
        ("setup", 10.0, 15.0),
    ]
    assert [section.segment_ids for section in sections if section.type != "non_story"] == [[0], [1]]


def test_story_map_splits_bucket_across_adjacent_non_story_ranges() -> None:
    film_map = [make_segment(0, 0, 4), make_segment(1, 12, 16)]
    profile = VideoProfile(
        input_path="film.mp4",
        duration_s=16,
        intro=IntroDetection(detected=False, confidence=0, reasons=[]),
        non_story_ranges=[
            NonStoryRange(start_s=4, end_s=8, label="ending_theme", confidence=1.0),
            NonStoryRange(start_s=8, end_s=12, label="next_episode_preview", confidence=1.0),
        ],
        classifier="mock",
        created_at="2026-07-30T00:00:00Z",
    )

    sections, _report = build_story_sections(
        film_map,
        duration_s=16,
        video_profile=profile,
        target_story_sections=1,
    )

    validate_story_map(sections, duration=16)
    assert [(section.type, section.tc_start, section.tc_end) for section in sections] == [
        ("setup", 0.0, 4.0),
        ("non_story", 4.0, 8.0),
        ("non_story", 8.0, 12.0),
        ("setup", 12.0, 16.0),
    ]
