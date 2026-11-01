from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common.schema import EditPlan, EditPlanQa, EdlPlacement, EdlSourceMap, RenderMeta, write_json
from scripts.enhanced_real_media_smoke import (
    ProductionInputs,
    RealMediaSmokeError,
    SmokePaths,
    analyze_enhanced_audio,
    analyze_picture,
    build_excerpt_artifacts,
    build_match_command,
    build_paths,
    max_cues_in_rolling_window,
    production_artifact_guard,
    safe_excerpt_cutoff,
    sample_window_correlation,
    validate_dynamic_outputs,
    validate_postprocess_outputs,
    validate_render_output,
    write_acceptance_freeze_override,
)


def source_ref(*, beat_id: int, start: float, end: float) -> dict[str, object]:
    return {
        "event_id": f"ep1:event:{beat_id}",
        "episode_key": "ep1",
        "src": "ep1/anime.mp4",
        "source_path": "anime.mp4",
        "from_seg_id": beat_id,
        "to_seg_id": beat_id,
        "src_tc_start": start,
        "src_tc_end": end,
    }


def review_beat(*, beat_id: int, start: float, end: float) -> dict[str, object]:
    return {
        "beat_id": beat_id,
        "narration": f"Beat {beat_id}",
        "source_refs": [source_ref(beat_id=beat_id, start=start, end=end)],
        "is_hook": beat_id == 0,
    }


def timing(*, beat_id: int, start: float, end: float) -> dict[str, object]:
    return {
        "beat_id": beat_id,
        "audio_path": f"audio/{beat_id}.mp3",
        "tl_start": start,
        "tl_end": end,
        "duration": end - start,
    }


def placement(
    *, index: int, beat_id: int, tl_start: float, tl_end: float, src_in: float, src_out: float
) -> EdlPlacement:
    return EdlPlacement(
        tl_start=tl_start,
        tl_end=tl_end,
        src="ep1/anime.mp4",
        src_in=src_in,
        src_out=src_out,
        beat_id=beat_id,
        shot_index=index,
        reused=False,
        speed=1.0,
    )


def shot(
    *, index: int, start: float, end: float, is_story: bool = True, motion_score: float = 0.7
) -> dict[str, object]:
    return {
        "src": "anime.mp4",
        "index": index,
        "tc_start": start,
        "tc_end": end,
        "duration": end - start,
        "thumb": f"shots/{index:03d}.jpg",
        "motion_score": motion_score,
        "face_count": 0,
        "face_area": 0.0,
        "brightness": 0.5,
        "is_usable": True,
        "unusable_reasons": [],
        "is_story": is_story,
        "exclude_reason": None if is_story else "opening_theme",
        "is_end_credit": False,
        "credit_like_score": 0.0,
    }


def test_build_paths_keeps_acceptance_outside_production_run(tmp_path: Path) -> None:
    args = argparse.Namespace(
        season_run_dir=tmp_path / "season",
        audio_assets=tmp_path / "audio_assets.test.yaml",
        work_dir=Path("work") / "enhanced-real-test",
        excerpt_seconds=90.0,
        seed=1234,
        report=None,
    )
    paths = build_paths(args)
    assert paths.full_dir.parent == paths.work_dir
    assert paths.excerpt_dir.parent == paths.work_dir

    args.work_dir = args.season_run_dir / "acceptance"
    with pytest.raises(RealMediaSmokeError, match="release work dir"):
        build_paths(args)


def test_build_excerpt_trims_partial_final_beat_and_keeps_id_sets_equal(tmp_path: Path) -> None:
    full_dir = tmp_path / "full"
    excerpt_dir = tmp_path / "excerpt"
    full_dir.mkdir()
    write_json(
        full_dir / "edl.json",
        [
            placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.0, src_in=0.0, src_out=2.0),
            placement(index=1, beat_id=1, tl_start=2.0, tl_end=4.0, src_in=10.0, src_out=12.0),
        ],
    )
    (full_dir / "series_review_script.json").write_text(
        json.dumps([review_beat(beat_id=0, start=0.0, end=2.0), review_beat(beat_id=1, start=10.0, end=12.0)]),
        encoding="utf-8",
    )
    (full_dir / "beats_timing.json").write_text(
        json.dumps([timing(beat_id=0, start=0.0, end=2.0), timing(beat_id=1, start=2.0, end=4.0)]),
        encoding="utf-8",
    )
    (full_dir / "edl.source_map.json").write_text(
        json.dumps({"version": 1, "sources": {"ep1/anime.mp4": str(tmp_path / "anime.mp4")}}),
        encoding="utf-8",
    )

    edl, beats, timings = build_excerpt_artifacts(
        full_edl_path=full_dir / "edl.json",
        full_source_map_path=full_dir / "edl.source_map.json",
        review_script_path=full_dir / "series_review_script.json",
        beats_timing_path=full_dir / "beats_timing.json",
        output_dir=excerpt_dir,
        duration_s=3.25,
    )

    assert edl[-1].tl_end == pytest.approx(98 / 30)
    assert edl[-1].tl_end - edl[-1].tl_start >= 0.6
    assert edl[-1].src_out == pytest.approx(11 + 8 / 30)
    assert timings[-1].tl_end == pytest.approx(98 / 30)
    assert timings[-1].duration == pytest.approx(38 / 30)
    assert {beat.beat_id for beat in beats} == {timing.beat_id for timing in timings} == {0, 1}


def test_safe_excerpt_cutoff_avoids_sub_floor_tail() -> None:
    edl = [
        placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.0, src_in=0.0, src_out=2.0),
        placement(index=1, beat_id=1, tl_start=2.0, tl_end=4.0, src_in=10.0, src_out=12.0),
    ]
    cutoff = safe_excerpt_cutoff(edl, 2.1)
    assert cutoff == 2.0
    assert cutoff * 30 == round(cutoff * 30)


def test_acceptance_freeze_override_selects_highest_motion_eligible_placement(tmp_path: Path) -> None:
    episode = tmp_path / "ep1"
    episode.mkdir()
    (episode / "shots.json").write_text(
        json.dumps(
            [
                shot(index=0, start=0.0, end=2.5, motion_score=0.2),
                shot(index=1, start=2.5, end=5.0, motion_score=0.9),
            ]
        ),
        encoding="utf-8",
    )
    edl_path = tmp_path / "edl.json"
    write_json(
        edl_path,
        [
            placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.5, src_in=0.0, src_out=2.5),
            placement(index=1, beat_id=0, tl_start=2.5, tl_end=5.0, src_in=2.5, src_out=5.0),
        ],
    )
    review_path = tmp_path / "series_review_script.json"
    review_path.write_text(json.dumps([review_beat(beat_id=0, start=0.0, end=5.0)]), encoding="utf-8")
    override_path = tmp_path / "edit_overrides.acceptance.json"

    selected = write_acceptance_freeze_override(
        edl_path=edl_path,
        review_script_path=review_path,
        episode_run_dirs={"ep1": episode},
        output_path=override_path,
    )

    assert selected == 1
    assert json.loads(override_path.read_text(encoding="utf-8"))["placements"] == [
        {"placement_index": 1, "freeze": True}
    ]


def test_validate_dynamic_outputs_requires_diagnostics_for_contiguous_reuse(tmp_path: Path) -> None:
    episode = tmp_path / "ep1"
    episode.mkdir()
    (episode / "shots.json").write_text(
        json.dumps([shot(index=0, start=0.0, end=2.4), shot(index=1, start=2.4, end=4.8)]),
        encoding="utf-8",
    )
    edl_path = tmp_path / "edl.json"
    write_json(
        edl_path,
        [
            placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.4, src_in=0.0, src_out=2.4),
            placement(index=1, beat_id=0, tl_start=2.4, tl_end=4.8, src_in=2.4, src_out=4.8),
        ],
    )
    beats_path = tmp_path / "series_review_script.json"
    beats_path.write_text(json.dumps([review_beat(beat_id=0, start=0.0, end=4.8)]), encoding="utf-8")
    timings_path = tmp_path / "beats_timing.json"
    timings_path.write_text(json.dumps([timing(beat_id=0, start=0.0, end=4.8)]), encoding="utf-8")
    qa_path = tmp_path / "edl.qa.json"
    qa_path.write_text(
        json.dumps(
            {
                "algorithm_version": "series-v3-dynamic-anime",
                "clip_profile": "dynamic_anime",
                "n_adjacent_repeat_fallbacks": 0,
                "n_contiguous_source_fallbacks": 1,
            }
        ),
        encoding="utf-8",
    )

    report = validate_dynamic_outputs(
        edl_path=edl_path,
        qa_path=qa_path,
        beats_path=beats_path,
        timings_path=timings_path,
        episode_run_dirs={"ep1": episode},
    )
    assert report["contiguous_source_fallbacks"] == 1

    payload = json.loads(qa_path.read_text(encoding="utf-8"))
    payload["n_contiguous_source_fallbacks"] = 0
    qa_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RealMediaSmokeError, match="not fully covered"):
        validate_dynamic_outputs(
            edl_path=edl_path,
            qa_path=qa_path,
            beats_path=beats_path,
            timings_path=timings_path,
            episode_run_dirs={"ep1": episode},
        )


def test_validate_dynamic_outputs_rejects_non_story_shot(tmp_path: Path) -> None:
    episode = tmp_path / "ep1"
    episode.mkdir()
    (episode / "shots.json").write_text(
        json.dumps([shot(index=0, start=0.0, end=2.0, is_story=False)]), encoding="utf-8"
    )
    edl_path = tmp_path / "edl.json"
    write_json(edl_path, [placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.0, src_in=0.0, src_out=2.0)])
    beats_path = tmp_path / "series_review_script.json"
    beats_path.write_text(json.dumps([review_beat(beat_id=0, start=0.0, end=2.0)]), encoding="utf-8")
    timings_path = tmp_path / "beats_timing.json"
    timings_path.write_text(json.dumps([timing(beat_id=0, start=0.0, end=2.0)]), encoding="utf-8")
    qa_path = tmp_path / "edl.qa.json"
    qa_path.write_text(
        json.dumps(
            {
                "algorithm_version": "series-v3-dynamic-anime",
                "clip_profile": "dynamic_anime",
                "n_adjacent_repeat_fallbacks": 0,
                "n_contiguous_source_fallbacks": 0,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RealMediaSmokeError, match="non-story"):
        validate_dynamic_outputs(
            edl_path=edl_path,
            qa_path=qa_path,
            beats_path=beats_path,
            timings_path=timings_path,
            episode_run_dirs={"ep1": episode},
        )


def test_validate_postprocess_outputs_checks_bounds_density_and_spacing(tmp_path: Path) -> None:
    edl = [
        placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.0, src_in=0.0, src_out=2.0),
        placement(index=1, beat_id=1, tl_start=2.0, tl_end=20.0, src_in=10.0, src_out=28.0),
    ]
    edl_path = tmp_path / "edl.json"
    write_json(edl_path, edl)
    plan = EditPlan.model_validate(
        {
            "seed": 1234,
            "total_duration_s": 20.0,
            "placements": [
                {"placement_index": 0, "beat_id": 0, "src_in": 0.0, "src_out": 2.0, "zoom_start": 1.03, "zoom_end": 1.03},
                {"placement_index": 1, "beat_id": 1, "src_in": 10.0, "src_out": 28.0, "freeze_duration_s": 0.45},
            ],
            "music_cues": [
                {"asset_id": "music-default", "tl_start": 0.0, "tl_end": 20.0, "mood": "default"}
            ],
            "sfx_cues": [
                {"asset_id": "whoosh", "kind": "whoosh", "tl_start": 1.0, "gain_db": -14.0},
                {"asset_id": "impact", "kind": "impact", "tl_start": 19.55, "gain_db": -10.0},
            ],
        }
    )
    qa = EditPlanQa(
        source_bound_checks=[
            {"placement_index": 0, "effect_in_bounds": True},
            {"placement_index": 1, "effect_in_bounds": True},
        ],
        cue_density={"music_coverage_ratio": 1.0},
    )
    write_json(tmp_path / "edit_plan.json", plan)
    write_json(tmp_path / "edit_plan.qa.json", qa)

    report = validate_postprocess_outputs(
        plan_path=tmp_path / "edit_plan.json", qa_path=tmp_path / "edit_plan.qa.json", edl_path=edl_path
    )
    assert report["n_freeze"] == 1
    assert report["n_music_cues"] == 1

    bad_qa = qa.model_copy(update={"source_bound_checks": [{"effect_in_bounds": False}]})
    write_json(tmp_path / "edit_plan.qa.json", bad_qa)
    with pytest.raises(RealMediaSmokeError, match="source-bound"):
        validate_postprocess_outputs(
            plan_path=tmp_path / "edit_plan.json", qa_path=tmp_path / "edit_plan.qa.json", edl_path=edl_path
        )
    bad_current_qa = qa.model_copy(
        update={"source_bound_checks": [{"in_bounds": False, "effect_in_bounds": True}]}
    )
    write_json(tmp_path / "edit_plan.qa.json", bad_current_qa)
    with pytest.raises(RealMediaSmokeError, match="source-bound"):
        validate_postprocess_outputs(
            plan_path=tmp_path / "edit_plan.json", qa_path=tmp_path / "edit_plan.qa.json", edl_path=edl_path
        )


def test_postprocess_acceptance_requires_effects_and_rolling_whoosh_cap(tmp_path: Path) -> None:
    assert max_cues_in_rolling_window([0.0, 6.0, 12.0, 18.0, 24.0, 30.0, 36.0], 60.0) == 7
    edl_path = tmp_path / "edl.json"
    write_json(edl_path, [placement(index=0, beat_id=0, tl_start=0.0, tl_end=4.0, src_in=0.0, src_out=4.0)])
    plan = EditPlan.model_validate(
        {
            "seed": 1234,
            "total_duration_s": 4.0,
            "placements": [{"placement_index": 0, "beat_id": 0, "src_in": 0.0, "src_out": 4.0}],
            "music_cues": [
                {"asset_id": "music-default", "tl_start": 0.0, "tl_end": 4.0, "mood": "default"}
            ],
        }
    )
    write_json(tmp_path / "edit_plan.json", plan)
    write_json(
        tmp_path / "edit_plan.qa.json",
        EditPlanQa(
            source_bound_checks=[{"placement_index": 0, "in_bounds": True}],
            cue_density={"music_coverage_ratio": 1.0},
        ),
    )
    with pytest.raises(RealMediaSmokeError, match="no planned zoom"):
        validate_postprocess_outputs(
            plan_path=tmp_path / "edit_plan.json",
            qa_path=tmp_path / "edit_plan.qa.json",
            edl_path=edl_path,
            require_zoom=True,
        )
    with pytest.raises(RealMediaSmokeError, match="no planned freeze"):
        validate_postprocess_outputs(
            plan_path=tmp_path / "edit_plan.json",
            qa_path=tmp_path / "edit_plan.qa.json",
            edl_path=edl_path,
            require_freeze=True,
        )

    dense_plan_payload = plan.model_dump(mode="json")
    dense_plan_payload.update(
        {
            "total_duration_s": 42.0,
            "music_cues": [
                {"asset_id": "music-default", "tl_start": 0.0, "tl_end": 42.0, "mood": "default"}
            ],
            "sfx_cues": [
                {"asset_id": "whoosh", "kind": "whoosh", "tl_start": float(index * 6), "gain_db": -14.0}
                for index in range(7)
            ],
        }
    )
    dense_plan = EditPlan.model_validate(dense_plan_payload)
    write_json(tmp_path / "edit_plan.json", dense_plan)
    write_json(
        edl_path,
        [placement(index=0, beat_id=0, tl_start=0.0, tl_end=42.0, src_in=0.0, src_out=42.0)],
    )
    with pytest.raises(RealMediaSmokeError, match="rolling 60 second"):
        validate_postprocess_outputs(
            plan_path=tmp_path / "edit_plan.json",
            qa_path=tmp_path / "edit_plan.qa.json",
            edl_path=edl_path,
        )


def test_match_command_is_dynamic_and_writes_only_to_acceptance_work(tmp_path: Path) -> None:
    paths = SmokePaths(
        season_run_dir=tmp_path / "season",
        production_final_dir=tmp_path / "season" / "series_recap",
        work_dir=tmp_path / "work",
        full_dir=tmp_path / "work" / "full",
        excerpt_dir=tmp_path / "work" / "excerpt",
        report=tmp_path / "work" / "report.json",
    )
    inputs = ProductionInputs(
        review_script=paths.production_final_dir / "series_review_script.json",
        event_bank=paths.production_final_dir / "series_event_bank.json",
        beats_timing=paths.production_final_dir / "beats_timing.json",
        voiceover=paths.production_final_dir / "voiceover.mp3",
        episode_run_dirs={"ep1": paths.season_run_dir / "ep1"},
    )
    command = build_match_command(inputs, paths)
    assert command[command.index("--clip-profile") + 1] == "dynamic_anime"
    assert Path(command[command.index("--output") + 1]).is_relative_to(paths.work_dir)
    assert Path(command[command.index("--output-source-map") + 1]).is_relative_to(paths.work_dir)
    assert Path(command[command.index("--output-qa") + 1]).is_relative_to(paths.work_dir)


def test_validate_render_output_uses_media_probes_and_render_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    excerpt = tmp_path / "excerpt"
    excerpt.mkdir()
    output = excerpt / "enhanced-real-smoke.mp4"
    output.write_bytes(b"mp4")
    write_json(
        excerpt / "edl.json",
        [placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.0, src_in=0.0, src_out=2.0)],
    )
    (excerpt / "edl.source_map.json").write_text(
        json.dumps({"version": 1, "sources": {"ep1/anime.mp4": str(tmp_path / "anime.mp4")}}),
        encoding="utf-8",
    )
    write_json(
        excerpt / "edit_plan.json",
        EditPlan.model_validate(
            {
                "seed": 1234,
                "total_duration_s": 2.0,
                "placements": [{"placement_index": 0, "beat_id": 0, "src_in": 0.0, "src_out": 2.0}],
                "music_cues": [
                    {"asset_id": "music-default", "tl_start": 0.0, "tl_end": 2.0, "mood": "default"}
                ],
            }
        ),
    )
    write_json(
        excerpt / "render.meta.json",
        RenderMeta(
            width=1920,
            height=1080,
            fps=30.0,
            codec="h264",
            video_duration_s=2.0,
            audio_duration_s=2.0,
            audio_delay_s=0.0,
            duration_match=True,
            n_placements=1,
            n_temp_clips=1,
            audio_stream_count=1,
            original_audio_included=False,
            warnings=[],
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            cache_hits=[],
        ),
    )
    monkeypatch.setattr(
        "scripts.enhanced_real_media_smoke.probe_video_stream",
        lambda _path: {"width": 1920, "height": 1080, "fps": 30.0, "frame_count": 60},
    )
    monkeypatch.setattr("scripts.enhanced_real_media_smoke.probe_duration", lambda _path: 2.0)
    monkeypatch.setattr("scripts.enhanced_real_media_smoke.probe_audio_stream_count", lambda _path: 1)
    monkeypatch.setattr(
        "scripts.enhanced_real_media_smoke.analyze_picture",
        lambda **_kwargs: {"decoded_frames": 60, "zoom_mad": None, "freeze_frame_mad": None},
    )
    paths = SmokePaths(
        season_run_dir=tmp_path / "season",
        production_final_dir=tmp_path / "season" / "series_recap",
        work_dir=tmp_path,
        full_dir=tmp_path / "full",
        excerpt_dir=excerpt,
        report=tmp_path / "report.json",
    )

    monkeypatch.setattr(
        "scripts.enhanced_real_media_smoke.analyze_enhanced_audio",
        lambda **_kwargs: {"music_min_5s_rms": 0.01, "final_audio_matches_master_packets": True},
    )
    report = validate_render_output(
        paths=paths,
        duration_s=2.0,
        require_zoom=False,
        require_freeze=False,
        require_sfx=False,
    )
    assert report["audio_stream_count"] == 1
    assert report["original_audio_included"] is False


def test_analyze_picture_measures_push_in_growth_and_relative_freeze_motion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edl = [placement(index=0, beat_id=0, tl_start=0.0, tl_end=4.0, src_in=0.0, src_out=4.0)]
    plan = EditPlan.model_validate(
        {
            "seed": 1234,
            "total_duration_s": 4.0,
            "placements": [
                {
                    "placement_index": 0,
                    "beat_id": 0,
                    "src_in": 0.0,
                    "src_out": 4.0,
                    "zoom_start": 1.02,
                    "zoom_end": 1.08,
                    "freeze_duration_s": 0.45,
                }
            ],
            "music_cues": [
                {"asset_id": "music-default", "tl_start": 0.0, "tl_end": 4.0, "mood": "default"}
            ],
        }
    )
    frames = [bytes([100 + (index % 2) * 4]) * 16 for index in range(108)]
    frames.extend(bytes([100 + (index % 2)]) * 16 for index in range(12))
    monkeypatch.setattr("scripts.enhanced_real_media_smoke.video_frames", lambda _path: frames)

    def fake_single_frame(path: Path, timestamp_s: float) -> bytes:
        if path.name == "output.mp4":
            return bytes([110 if timestamp_s < 2.0 else 125]) * 16
        return bytes([100]) * 16

    monkeypatch.setattr("scripts.enhanced_real_media_smoke.single_frame", fake_single_frame)
    metrics = analyze_picture(
        output=tmp_path / "output.mp4",
        plan=plan,
        edl=edl,
        source_map=EdlSourceMap(sources={"ep1/anime.mp4": str(tmp_path / "anime.mp4")}),
        require_zoom=True,
        require_freeze=True,
    )

    assert metrics["zoom_growth_ratio"] > 1.03
    assert metrics["freeze_frame_mad"] > 0.35
    assert metrics["freeze_frame_mad"] < metrics["moving_frame_mad"] * 0.7


def test_analyze_picture_rejects_any_black_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edl = [placement(index=0, beat_id=0, tl_start=0.0, tl_end=2.0, src_in=0.0, src_out=2.0)]
    plan = EditPlan.model_validate(
        {
            "seed": 1234,
            "total_duration_s": 2.0,
            "placements": [{"placement_index": 0, "beat_id": 0, "src_in": 0.0, "src_out": 2.0}],
            "music_cues": [
                {"asset_id": "music-default", "tl_start": 0.0, "tl_end": 2.0, "mood": "default"}
            ],
        }
    )
    frames = [bytes([100]) * 16 for _ in range(60)]
    frames[30] = bytes([0]) * 16
    monkeypatch.setattr("scripts.enhanced_real_media_smoke.video_frames", lambda _path: frames)

    with pytest.raises(RealMediaSmokeError, match="contains black frames"):
        analyze_picture(
            output=tmp_path / "output.mp4",
            plan=plan,
            edl=edl,
            source_map=EdlSourceMap(sources={"ep1/anime.mp4": str(tmp_path / "anime.mp4")}),
            require_zoom=False,
            require_freeze=False,
        )


def test_analyze_enhanced_audio_proves_beds_master_and_video_only_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render_work = tmp_path / "render"
    audio_cache = render_work / "audio_cache"
    temp_clips = render_work / "temp_clips"
    audio_cache.mkdir(parents=True)
    temp_clips.mkdir()
    output = tmp_path / "output.mp4"
    source = tmp_path / "source.mp4"
    video_only = render_work / "video_only.mp4"
    music = audio_cache / "music-key.m4a"
    sfx = audio_cache / "sfx-key.m4a"
    master = audio_cache / "master-key.m4a"
    temp = temp_clips / "clip.mp4"
    for path in (output, source, video_only, music, sfx, master, temp):
        path.write_bytes(path.name.encode("ascii"))
    plan = EditPlan.model_validate(
        {
            "seed": 1234,
            "total_duration_s": 10.0,
            "placements": [{"placement_index": 0, "beat_id": 0, "src_in": 0.0, "src_out": 10.0}],
            "music_cues": [
                {"asset_id": "music-default", "tl_start": 0.0, "tl_end": 10.0, "mood": "default"}
            ],
            "sfx_cues": [
                {"asset_id": "whoosh", "kind": "whoosh", "tl_start": 2.0, "gain_db": -14.0}
            ],
        }
    )

    def fake_stream_count(path: Path) -> int:
        return 0 if path in {video_only, temp} else 1

    monkeypatch.setattr("scripts.enhanced_real_media_smoke.probe_audio_stream_count", fake_stream_count)
    monkeypatch.setattr("scripts.enhanced_real_media_smoke.audio_packet_hash", lambda _path: "master-packets")

    include_beds_in_master = True

    def fake_samples(path: Path, *, sample_rate: int = 8000) -> list[int]:
        music_samples = [500 if index % 2 == 0 else -500 for index in range(10 * sample_rate)]
        if path == music:
            return music_samples
        sfx_samples = [0] * (10 * sample_rate)
        sfx_samples[2 * sample_rate : 3 * sample_rate] = [
            1000 if index % 2 == 0 else -1000 for index in range(sample_rate)
        ]
        if path == sfx:
            return sfx_samples
        if not include_beds_in_master:
            return [300 if index % 4 < 2 else -300 for index in range(10 * sample_rate)]
        return [music_sample + sfx_sample for music_sample, sfx_sample in zip(music_samples, sfx_samples)]

    monkeypatch.setattr("scripts.enhanced_real_media_smoke.pcm_samples", fake_samples)
    report = analyze_enhanced_audio(
        output=output,
        plan=plan,
        render_work_dir=render_work,
        source_map=EdlSourceMap(sources={"ep1/anime.mp4": str(source)}),
        require_sfx=True,
    )
    assert report["music_min_5s_rms"] > 0
    assert report["music_master_correlation"] > 0
    assert len(report["music_master_cue_correlations"]) == 1
    assert report["sfx_cue_rms"][0] > 0
    assert report["sfx_master_correlations"][0] > 0
    assert report["final_audio_matches_master_packets"] is True
    assert report["source_audio_streams_exercised"] == 1

    include_beds_in_master = False
    with pytest.raises(RealMediaSmokeError, match="music cue"):
        analyze_enhanced_audio(
            output=output,
            plan=plan,
            render_work_dir=render_work,
            source_map=EdlSourceMap(sources={"ep1/anime.mp4": str(source)}),
            require_sfx=True,
        )


def test_audio_correlation_is_checked_per_music_cue_when_full_timeline_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render_work = tmp_path / "render"
    audio_cache = render_work / "audio_cache"
    temp_clips = render_work / "temp_clips"
    audio_cache.mkdir(parents=True)
    temp_clips.mkdir()
    output = tmp_path / "output.mp4"
    source = tmp_path / "source.mp4"
    video_only = render_work / "video_only.mp4"
    music = audio_cache / "music-key.m4a"
    sfx = audio_cache / "sfx-key.m4a"
    master = audio_cache / "master-key.m4a"
    temp = temp_clips / "clip.mp4"
    for path in (output, source, video_only, music, sfx, master, temp):
        path.write_bytes(path.name.encode("ascii"))
    plan = EditPlan.model_validate(
        {
            "seed": 1234,
            "total_duration_s": 4.0,
            "placements": [{"placement_index": 0, "beat_id": 0, "src_in": 0.0, "src_out": 4.0}],
            "music_cues": [
                {"asset_id": "music-a", "tl_start": 0.0, "tl_end": 2.0, "mood": "tension"},
                {"asset_id": "music-b", "tl_start": 2.0, "tl_end": 4.0, "mood": "calm"},
            ],
        }
    )
    sample_rate = 8000
    music_samples = [500 if index % 2 == 0 else -500 for index in range(4 * sample_rate)]
    master_samples = music_samples[: 2 * sample_rate] + [
        -value for value in music_samples[2 * sample_rate :]
    ]
    assert abs(sample_window_correlation(music_samples, master_samples, 0.0, 4.0)) < 1e-6

    def fake_stream_count(path: Path) -> int:
        return 0 if path in {video_only, temp} else 1

    monkeypatch.setattr("scripts.enhanced_real_media_smoke.probe_audio_stream_count", fake_stream_count)
    monkeypatch.setattr("scripts.enhanced_real_media_smoke.audio_packet_hash", lambda _path: "master-packets")

    def fake_samples(path: Path, *, sample_rate: int = 8000) -> list[int]:
        if path == music:
            return music_samples
        if path == master:
            return master_samples
        return [0] * (4 * sample_rate)

    monkeypatch.setattr("scripts.enhanced_real_media_smoke.pcm_samples", fake_samples)
    report = analyze_enhanced_audio(
        output=output,
        plan=plan,
        render_work_dir=render_work,
        source_map=EdlSourceMap(sources={"ep1/anime.mp4": str(source)}),
        require_sfx=False,
    )

    assert report["music_master_correlation"] == pytest.approx(1.0)
    assert [item["correlation"] for item in report["music_master_cue_correlations"]] == [1.0, 1.0]


def test_production_guard_hashes_and_reports_mutation_on_failure(tmp_path: Path) -> None:
    final = tmp_path / "series_recap"
    episode = tmp_path / "ep1"
    final.mkdir()
    episode.mkdir()
    review = final / "series_review_script.json"
    event_bank = final / "series_event_bank.json"
    timings = final / "beats_timing.json"
    voiceover = final / "voiceover.mp3"
    shots = episode / "shots.json"
    source_map = final / "edl.source_map.json"
    for path in (review, event_bank, timings, voiceover, shots, source_map):
        path.write_bytes(b"aaaa")
    inputs = ProductionInputs(
        review_script=review,
        event_bank=event_bank,
        beats_timing=timings,
        voiceover=voiceover,
        episode_run_dirs={"ep1": episode},
    )
    failure_report = tmp_path / "work" / "production-mutation.json"
    with pytest.raises(RealMediaSmokeError, match="production artifacts changed"):
        with production_artifact_guard(inputs, source_map, failure_report=failure_report):
            review.write_bytes(b"bbbb")
            raise RuntimeError("simulated acceptance failure")
    payload = json.loads(failure_report.read_text(encoding="utf-8"))
    assert payload["reason"] == "production_artifact_mutation"
    assert str(review.resolve()) in payload["changed_paths"]


def test_production_guard_reports_deleted_artifact_on_failure(tmp_path: Path) -> None:
    final = tmp_path / "series_recap"
    episode = tmp_path / "ep1"
    final.mkdir()
    episode.mkdir()
    review = final / "series_review_script.json"
    event_bank = final / "series_event_bank.json"
    timings = final / "beats_timing.json"
    voiceover = final / "voiceover.mp3"
    shots = episode / "shots.json"
    source_map = final / "edl.source_map.json"
    for path in (review, event_bank, timings, voiceover, shots, source_map):
        path.write_bytes(b"artifact")
    inputs = ProductionInputs(
        review_script=review,
        event_bank=event_bank,
        beats_timing=timings,
        voiceover=voiceover,
        episode_run_dirs={"ep1": episode},
    )
    failure_report = tmp_path / "work" / "production-mutation.json"

    with pytest.raises(RealMediaSmokeError, match="production artifacts changed"):
        with production_artifact_guard(inputs, source_map, failure_report=failure_report):
            shots.unlink()
            raise RuntimeError("simulated acceptance failure")

    payload = json.loads(failure_report.read_text(encoding="utf-8"))
    assert str(shots.resolve()) in payload["changed_paths"]
