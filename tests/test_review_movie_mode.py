from __future__ import annotations

from common.schema import FilmMapSegment, ReviewBeat
from review.models import OutlineBeat, OutlineResult
from review.movie_mode import apply_hook_mode, check_opening_coherence, resolve_target_ratio
from review.llm_flow import normalize_outline_payload


def seg(idx: int, start: float, end: float, text: str = "Story scene") -> FilmMapSegment:
    return FilmMapSegment(id=idx, type="speech", tc_start=start, tc_end=end, ko=text, en=text, scene_desc=None)


def test_movie_auto_ratio_is_clamped_to_movie_range() -> None:
    film_map = [seg(i, i * 10, i * 10 + 5, f"Alice secret ghost ritual {i}") for i in range(30)]
    result = resolve_target_ratio("auto", content_type="movie", film_map=film_map, duration_s=900)
    assert result.target_ratio_mode == "auto"
    assert 0.18 <= result.target_ratio <= 0.26
    assert result.complexity_score > 0


def test_anime_movie_auto_ratio_is_clamped_to_movie_range() -> None:
    film_map = [seg(i, i * 10, i * 10 + 5, f"Aki hidden gate battle {i}") for i in range(30)]
    result = resolve_target_ratio("auto", content_type="anime_movie", film_map=film_map, duration_s=900)
    assert result.target_ratio_mode == "auto"
    assert 0.18 <= result.target_ratio <= 0.26

def test_episode_auto_keeps_legacy_ratio() -> None:
    result = resolve_target_ratio("auto", content_type="episode", film_map=[seg(0, 0, 5)], duration_s=100)
    assert result.target_ratio == 0.33

def test_anime_series_auto_keeps_episode_ratio() -> None:
    result = resolve_target_ratio("auto", content_type="anime_series", film_map=[seg(0, 0, 5)], duration_s=100)
    assert result.target_ratio == 0.33


def test_movie_setup_hook_uses_opening_beat_not_late_twist() -> None:
    film_map = [seg(0, 0, 5), seg(1, 30, 35), seg(2, 500, 510)]
    outline = OutlineResult(
        glossary=[],
        outline=[
            OutlineBeat(from_seg_id=2, to_seg_id=2, summary="late twist", is_hook=True),
            OutlineBeat(from_seg_id=0, to_seg_id=1, summary="setup", is_hook=False),
        ],
        hook=[2],
    )
    updated = apply_hook_mode(outline, content_type="movie", hook_mode="setup", film_map=film_map)
    assert updated.outline[0].is_hook is True
    assert updated.outline[0].from_seg_id == 0


def test_anime_movie_setup_hook_respects_story_start() -> None:
    film_map = [seg(0, 40, 60), seg(1, 190, 210), seg(2, 230, 250)]
    outline = OutlineResult(
        glossary=[],
        outline=[
            OutlineBeat(from_seg_id=0, to_seg_id=0, summary="opening theme", is_hook=True),
            OutlineBeat(from_seg_id=1, to_seg_id=2, summary="real story setup", is_hook=False),
        ],
        hook=[0],
    )
    updated = apply_hook_mode(outline, content_type="anime_movie", hook_mode="setup", film_map=film_map, story_start_s=185)
    assert updated.outline[0].is_hook is True
    assert updated.outline[0].from_seg_id == 1

def test_opening_coherence_rejects_late_or_contextless_opening() -> None:
    beats = [ReviewBeat(beat_id=0, narration="S?c qu?, kh?ng ai ng? c? twist n?y.", from_seg_id=9, to_seg_id=9, src_tc_start=500, src_tc_end=520, is_hook=True)]
    report = check_opening_coherence(beats, content_type="movie", hook_mode="setup")
    assert report.passed is False
    assert any("too late" in issue for issue in report.issues)


def test_outline_payload_accepts_object_hook() -> None:
    data = normalize_outline_payload({"outline": [], "hook": {"from_seg_id": 1, "to_seg_id": 2}})
    assert data["hook"] == [1, 2]


def test_movie_setup_hook_respects_story_start() -> None:
    film_map = [seg(0, 40, 60), seg(1, 190, 210), seg(2, 230, 250)]
    outline = OutlineResult(
        glossary=[],
        outline=[
            OutlineBeat(from_seg_id=0, to_seg_id=0, summary="excluded intro setup", is_hook=True),
            OutlineBeat(from_seg_id=1, to_seg_id=2, summary="real story setup", is_hook=False),
        ],
        hook=[0],
    )
    updated = apply_hook_mode(outline, content_type="movie", hook_mode="setup", film_map=film_map, story_start_s=185)
    assert updated.outline[0].is_hook is True
    assert updated.outline[0].from_seg_id == 1


def test_opening_coherence_rejects_source_before_story_start() -> None:
    beats = [ReviewBeat(beat_id=0, narration="Eun-seo ?ang s?ng m?t m?nh trong c?n nh? l? v? c?u chuy?n b?t ??u khi c?c d?u hi?u qu? d? xu?t hi?n.", from_seg_id=0, to_seg_id=0, src_tc_start=40, src_tc_end=180, is_hook=True)]
    report = check_opening_coherence(beats, content_type="movie", hook_mode="setup", story_start_s=185)
    assert report.passed is False
    assert any("ends before story_start_s" in issue for issue in report.issues)
