from __future__ import annotations

from datetime import datetime, timezone

from common.schema import FilmMapSegment, IntroDetection, NonStoryRange, ReviewBeat, StorySection, VideoProfile
from review.models import OutlineBeat, OutlineResult
from review.movie_mode import AutoDurationPolicy, apply_hook_mode, check_opening_coherence, resolve_qa_iterations, resolve_target_ratio
from review.llm_flow import normalize_outline_payload


def seg(idx: int, start: float, end: float, text: str = "Story scene") -> FilmMapSegment:
    return FilmMapSegment(id=idx, type="speech", tc_start=start, tc_end=end, ko=text, en=text, scene_desc=None)


def profile(duration_s: float, ranges: list[tuple[float, float]] | None = None) -> VideoProfile:
    return VideoProfile(
        input_path="film.mp4",
        duration_s=duration_s,
        intro=IntroDetection(detected=False, confidence=0.0, reasons=[]),
        non_story_ranges=[NonStoryRange(start_s=start, end_s=end, label="opening", confidence=0.9) for start, end in (ranges or [])],
        classifier="heuristic",
        created_at=datetime.now(timezone.utc),
    )


def story_sections() -> list[StorySection]:
    return [
        StorySection(
            section_id=i,
            type="conflict",
            tc_start=i * 500.0,
            tc_end=i * 500.0 + 400.0,
            summary=f"section {i}",
            characters=["Kim Dokja", "Yoo Joonghyuk", "Jung Heewon", "Lee Hyunsung", "Lee Jihye", "Shin Yoosung", "Han Sooyoung", "Lee Seolhwa", "Gong Pildu", "Anna Croft"],
            locations=[],
            events=["survive", "reveal", "fight"],
            confidence=0.8,
        )
        for i in range(7)
    ]


def test_movie_auto_ratio_keeps_light_movies_short() -> None:
    film_map = [seg(i, i * 60, i * 60 + 10, "A simple family scene") for i in range(10)]
    result = resolve_target_ratio("auto", content_type="movie", film_map=film_map, duration_s=3600, video_profile=profile(3600))
    assert result.target_ratio_mode == "auto"
    assert 0.18 <= result.target_ratio <= 0.22
    assert result.complexity_score > 0
    assert result.auto_duration_policy == "balanced-v1"


def test_movie_auto_ratio_allows_dense_survival_fantasy() -> None:
    text = "Kim Dokja Yoo Joonghyuk Jung Heewon Lee Hyunsung Lee Jihye Shin Yoosung Han Sooyoung Lee Seolhwa Gong Pildu Anna Croft survival system game secret monster death sinh tồn tận thế trò chơi kịch bản bí mật hồi quy chòm sao năng lực nhiệm vụ luật sống sót "
    film_map = [seg(i, i * 10, i * 10 + 8, text * 3) for i in range(240)]
    result = resolve_target_ratio("auto", content_type="movie", film_map=film_map, duration_s=3600, video_profile=profile(3600), story_sections=story_sections())
    assert result.complexity_score >= 0.80
    assert 0.35 <= result.target_ratio <= 0.38
    assert result.auto_duration_cap_applied == "none"


def test_movie_auto_uses_story_duration_minus_non_story_union() -> None:
    film_map = [seg(i, i * 40, i * 40 + 20, "survival game") for i in range(20)]
    result = resolve_target_ratio("auto", content_type="movie", film_map=film_map, duration_s=1000, video_profile=profile(1000, [(0, 100), (50, 200), (900, 1000)]))
    assert result.story_duration_s == 700
    assert result.target_duration_base_s == 700


def test_movie_auto_soft_cap_blocks_medium_complexity_long_outputs() -> None:
    text = "Kim secret survival game monster sinh tồn bí mật "
    film_map = [seg(i, i * 30, i * 30 + 18, text) for i in range(180)]
    result = resolve_target_ratio("auto", content_type="movie", film_map=film_map, duration_s=12000, video_profile=profile(12000))
    assert result.complexity_score < 0.80
    assert round(result.target_ratio * result.target_duration_base_s, 3) == 2100.0
    assert "soft_cap_s" in (result.auto_duration_cap_applied or "")


def test_movie_auto_hard_cap_limits_extreme_long_outputs() -> None:
    text = "Kim Dokja Yoo Joonghyuk Jung Heewon Lee Hyunsung Lee Jihye Shin Yoosung Han Sooyoung Lee Seolhwa Gong Pildu Anna Croft survival system game secret monster death sinh tồn tận thế trò chơi kịch bản bí mật hồi quy chòm sao năng lực nhiệm vụ luật sống sót "
    film_map = [seg(i, i * 15, i * 15 + 12, text * 3) for i in range(400)]
    result = resolve_target_ratio("auto", content_type="movie", film_map=film_map, duration_s=12000, video_profile=profile(12000), story_sections=story_sections())
    assert result.complexity_score >= 0.80
    assert round(result.target_ratio * result.target_duration_base_s, 3) == 2700.0
    assert "hard_cap_s" in (result.auto_duration_cap_applied or "")


def test_movie_auto_respects_configured_max_ratio_under_hard_guard() -> None:
    text = "Kim Dokja Yoo Joonghyuk Jung Heewon Lee Hyunsung Lee Jihye Shin Yoosung Han Sooyoung Lee Seolhwa Gong Pildu Anna Croft survival system game secret monster death sinh tồn tận thế trò chơi kịch bản bí mật hồi quy chòm sao năng lực nhiệm vụ luật sống sót "
    film_map = [seg(i, i * 10, i * 10 + 8, text * 3) for i in range(240)]
    result = resolve_target_ratio("auto", content_type="movie", film_map=film_map, duration_s=3600, video_profile=profile(3600), story_sections=story_sections(), auto_policy=AutoDurationPolicy(max_ratio=0.32))
    assert result.target_ratio == 0.32
    assert "max_ratio" in (result.auto_duration_cap_applied or "")


def test_episode_auto_keeps_legacy_ratio() -> None:
    result = resolve_target_ratio("auto", content_type="episode", film_map=[seg(0, 0, 5)], duration_s=100)
    assert result.target_ratio == 0.33


def test_fixed_ratio_keeps_full_duration_base() -> None:
    result = resolve_target_ratio(0.33, content_type="movie", film_map=[seg(0, 0, 5)], duration_s=1000, video_profile=profile(1000, [(0, 200)]))
    assert result.target_ratio == 0.33
    assert result.story_duration_s == 800
    assert result.target_duration_base_s == 1000


def test_long_movie_clamps_qa_iterations() -> None:
    result = resolve_qa_iterations(2, content_type="movie", target_video_s=2100, char_budget=31_000)
    assert result.requested_max_qa_iterations == 2
    assert result.effective_max_qa_iterations == 1
    assert result.policy == "long-movie-v1"
    assert result.warning and "long movie QA clamp" in result.warning


def test_short_movie_keeps_configured_qa_iterations() -> None:
    result = resolve_qa_iterations(2, content_type="movie", target_video_s=1800, char_budget=27_000)
    assert result.effective_max_qa_iterations == 2
    assert result.policy == "configured"


def test_episode_keeps_configured_qa_iterations() -> None:
    result = resolve_qa_iterations(2, content_type="episode", target_video_s=2400, char_budget=36_000)
    assert result.effective_max_qa_iterations == 2
    assert result.policy == "configured"


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
