from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from common.schema import (
    AudioAssetManifest,
    BeatEditOverride,
    BeatTiming,
    EditOverrides,
    EdlPlacement,
    PlacementEditOverride,
    SeriesEvent,
    SeriesEventBank,
    SeriesReviewBeat,
    SeriesSourceRef,
    Shot,
)
from postprocess.__main__ import run_postprocess
from postprocess.assets import AudioAssetError, load_audio_manifest, validate_audio_assets
from postprocess.planner import build_edit_plan

CREATED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _mock_audio_asset_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("postprocess.assets.probe_audio_stream_count", lambda _path: 1)
    monkeypatch.setattr("postprocess.assets.probe_duration", lambda _path: 1.0)


def _asset_manifest(tmp_path: Path, *, two_action_tracks: bool = False) -> tuple[Path, AudioAssetManifest, Path]:
    audio_dir = tmp_path / "library"
    audio_dir.mkdir()
    for filename in ["default.mp3", "action.mp3", "whoosh.wav", "impact.wav"]:
        (audio_dir / filename).write_bytes(filename.encode("ascii"))
    if two_action_tracks:
        (audio_dir / "action-two.mp3").write_bytes(b"action-two")
    assets = [
        {
            "asset_id": "music-default",
            "path": "default.mp3",
            "kind": "music",
            "mood": "default",
            "loopable": True,
            "license_source": "CC0",
        },
        {
            "asset_id": "music-action",
            "path": "action.mp3",
            "kind": "music",
            "mood": "action",
            "loopable": True,
            "license_source": "Creator license",
            "attribution_required": True,
            "attribution": "Action Track by Example Artist",
        },
        {
            "asset_id": "sfx-whoosh",
            "path": "whoosh.wav",
            "kind": "sfx",
            "sfx_kind": "whoosh",
            "license_source": "CC0",
        },
        {
            "asset_id": "sfx-impact",
            "path": "impact.wav",
            "kind": "sfx",
            "sfx_kind": "impact",
            "license_source": "CC0",
        },
    ]
    if two_action_tracks:
        assets.insert(
            2,
            {
                "asset_id": "music-action-two",
                "path": "action-two.mp3",
                "kind": "music",
                "mood": "action",
                "loopable": True,
                "license_source": "CC0",
            },
        )
    manifest_path = tmp_path / "audio_assets.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "base_dir": "library",
                "assets": assets,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest = validate_audio_assets(manifest_path)
    _, root = load_audio_manifest(manifest_path)
    return manifest_path, manifest, root


def _source_ref(beat_id: int, event_id: str) -> SeriesSourceRef:
    return SeriesSourceRef(
        event_id=event_id,
        episode_key="ep1",
        src="ep1/anime.mp4",
        source_path="anime.mp4",
        from_seg_id=beat_id,
        to_seg_id=beat_id,
        src_tc_start=beat_id * 6.0,
        src_tc_end=beat_id * 6.0 + 6.0,
    )


def _planner_inputs(count: int = 13) -> tuple[
    list[EdlPlacement],
    list[SeriesReviewBeat],
    list[BeatTiming],
    SeriesEventBank,
    dict[str, list[Shot]],
]:
    placements: list[EdlPlacement] = []
    beats: list[SeriesReviewBeat] = []
    timings: list[BeatTiming] = []
    events: list[SeriesEvent] = []
    shots: list[Shot] = []
    for index in range(count):
        event_type = "setup"
        if index == 0:
            event_type = "reveal"
        elif index == 1:
            event_type = "transition"
        elif index == 5:
            event_type = "climax"
        source_start = index * 6.0
        timeline_start = index * 4.0
        event_id = f"ep1:event:{index}"
        placements.append(
            EdlPlacement(
                tl_start=timeline_start,
                tl_end=timeline_start + 4.0,
                src="ep1/anime.mp4",
                src_in=source_start,
                src_out=source_start + 4.0,
                beat_id=index,
                shot_index=index,
                speed=1.0,
            )
        )
        beats.append(
            SeriesReviewBeat(
                beat_id=index,
                narration=f"Beat {index}",
                source_refs=[_source_ref(index, event_id)],
                is_hook=index == 0,
            )
        )
        timings.append(
            BeatTiming(
                beat_id=index,
                audio_path=f"audio/{index}.mp3",
                tl_start=timeline_start,
                tl_end=timeline_start + 4.0,
                duration=4.0,
            )
        )
        events.append(
            SeriesEvent(
                event_id=event_id,
                series_id="anime-s01",
                episode_key="ep1",
                episode_number=1,
                title="Episode 1",
                source_path="anime.mp4",
                recap_mode="full",
                summary=f"Event {index}",
                event_type=event_type,
                from_seg_id=index,
                to_seg_id=index,
                tc_start=source_start,
                tc_end=source_start + 6.0,
                importance=0.9 if index in {0, 5} else 0.5,
                is_hook_candidate=index == 0,
            )
        )
        shots.append(
            Shot(
                src="anime.mp4",
                index=index,
                tc_start=source_start,
                tc_end=source_start + 6.0,
                duration=6.0,
                thumb=f"shots/{index:03d}.jpg",
                motion_score=0.7,
                face_count=0,
                face_area=0.0,
                brightness=0.5,
                is_usable=True,
            )
        )
    bank = SeriesEventBank(
        series_id="anime-s01",
        recap_format="episode_arc_chaptered",
        episode_keys=["ep1"],
        target_video_s=count * 4.0,
        char_budget=1000,
        events=events,
        created_at=CREATED_AT,
    )
    return placements, beats, timings, bank, {"ep1": shots}


def test_audio_asset_preflight_requires_complete_local_library(tmp_path: Path) -> None:
    manifest_path, manifest, root = _asset_manifest(tmp_path)

    assert manifest.assets[0].asset_id == "music-default"
    assert root == (tmp_path / "library").resolve()

    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["assets"] = [asset for asset in raw["assets"] if asset.get("sfx_kind") != "impact"]
    manifest_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AudioAssetError, match="impact"):
        validate_audio_assets(manifest_path)

    raw["assets"][0]["license_source"] = ""
    manifest_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AudioAssetError, match="license_source"):
        validate_audio_assets(manifest_path)


def test_audio_asset_preflight_rejects_invalid_stream_or_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _, _ = _asset_manifest(tmp_path)
    monkeypatch.setattr("postprocess.assets.probe_audio_stream_count", lambda _path: 0)
    with pytest.raises(AudioAssetError, match="exactly one audio stream"):
        validate_audio_assets(manifest_path)

    monkeypatch.setattr("postprocess.assets.probe_audio_stream_count", lambda _path: 1)
    monkeypatch.setattr("postprocess.assets.probe_duration", lambda _path: 0.0)
    with pytest.raises(AudioAssetError, match="positive finite duration"):
        validate_audio_assets(manifest_path)


def test_planner_builds_deterministic_visual_music_and_sfx_cues(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs()

    first = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
        seed=42,
    )
    second = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
        seed=42,
    )
    plan, meta, qa, attribution = first

    assert plan.model_dump(mode="json") == second[0].model_dump(mode="json")
    assert plan.original_audio_included is False
    assert plan.placements[0].freeze_duration_s == 0.45
    assert plan.placements[1].speed_ramp[-1].speed == 1.35
    assert plan.placements[1].source_extension_s > 0
    assert sum(edit.aspect_ratio == 2.35 for edit in plan.placements) == 1
    assert plan.music_cues[0].tl_start == 0.0
    assert plan.music_cues[-1].tl_end == placements[-1].tl_end
    assert any(cue.asset_id == "music-action" for cue in plan.music_cues)
    assert {cue.kind for cue in plan.sfx_cues} == {"whoosh", "impact"}
    assert meta.n_speed_ramp == 1
    assert meta.n_freeze == 2
    assert qa.cue_density["whoosh_per_minute"] <= 6
    assert all(check["in_bounds"] for check in qa.source_bound_checks)
    assert "Action Track by Example Artist" in attribution


def test_reveal_uses_strong_visual_grade_but_keeps_suspense_mood(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs(count=3)
    bank.events[2].event_type = "reveal"

    plan, _, _, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
    )

    reveal = plan.placements[2]
    assert reveal.role == "reveal"
    assert reveal.mood == "suspense"
    assert reveal.contrast == 1.10
    assert reveal.saturation == 1.12


def test_override_precedence_is_placement_then_beat_then_auto(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs(count=2)
    overrides = EditOverrides(
        beats=[BeatEditOverride(beat_id=0, zoom_start=1.03, zoom_end=1.04, mood="emotional", contrast=1.2)],
        placements=[
            PlacementEditOverride(placement_index=0, zoom_start=1.07, zoom_end=1.09),
            PlacementEditOverride(placement_index=1, disable_effects=True),
        ],
    )

    plan, _, _, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
        overrides=overrides,
    )

    assert plan.placements[0].mood == "emotional"
    assert plan.placements[0].zoom_start == 1.07
    assert plan.placements[0].zoom_end == 1.09
    assert plan.placements[0].contrast == 1.2
    assert plan.placements[1].disabled is True
    assert plan.placements[1].zoom_start == 1.0
    assert plan.placements[1].contrast == 1.0


def test_freeze_override_skips_static_shot(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs(count=2)
    shots["ep1"][0] = shots["ep1"][0].model_copy(update={"motion_score": 0.0})

    plan, _, qa, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
        overrides=EditOverrides(beats=[BeatEditOverride(beat_id=0, freeze=True)]),
    )

    assert plan.placements[0].freeze_duration_s == 0.0
    assert any(
        item["effect"] == "freeze" and "static" in item["reason"]
        for item in qa.skipped_effects
    )


def test_music_tracks_rotate_round_robin_with_seeded_start(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path, two_action_tracks=True)
    placements, beats, timings, bank, shots = _planner_inputs(count=20)
    bank.events[10].event_type = "ending"
    bank.events[15].event_type = "climax"

    first, _, _, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
        seed=7,
    )
    second, _, _, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
        seed=7,
    )

    action_tracks = [cue.asset_id for cue in first.music_cues if cue.mood == "action"]
    assert action_tracks == [cue.asset_id for cue in second.music_cues if cue.mood == "action"]
    assert len(action_tracks) == 2
    assert action_tracks[0] != action_tracks[1]


def test_auto_music_does_not_create_short_final_track(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs(count=6)

    plan, _, _, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
    )

    assert len(plan.music_cues) == 1
    assert plan.music_cues[0].tl_end - plan.music_cues[0].tl_start == 24.0


def test_reveal_never_becomes_transition_ramp_for_multi_episode_beat(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs(count=2)
    bank.events[1].event_type = "reveal"
    beats[1].source_refs.append(beats[1].source_refs[0].model_copy(update={"episode_key": "ep2"}))

    plan, _, _, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
    )

    assert plan.placements[1].role == "reveal"
    assert plan.placements[1].speed_ramp == []


def test_chapter_opening_beat_receives_ramp_and_whoosh(tmp_path: Path) -> None:
    _, manifest, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs(count=4)
    # Chaptered scripts keep each beat inside one episode and carry no bridge or
    # transition events, so the only real transition is the beat that opens the
    # next episode chapter.
    bank.events[1].event_type = "setup"
    for index in (2, 3):
        bank.events[index].event_type = "setup"
        bank.events[index].episode_key = "ep2"
        beats[index].source_refs = [beats[index].source_refs[0].model_copy(update={"episode_key": "ep2"})]
    shots["ep2"] = shots["ep1"]

    plan, meta, qa, _ = build_edit_plan(
        placements=placements,
        beats=beats,
        timings=timings,
        event_bank=bank,
        shots_by_episode=shots,
        audio_manifest=manifest,
        input_fingerprint="fixture",
    )

    chapter_opening = plan.placements[2]
    assert chapter_opening.speed_ramp[-1].speed == 1.35
    assert chapter_opening.source_extension_s > 0
    # The beat still grades as setup; only ramp eligibility changed.
    assert chapter_opening.role == "setup"
    assert plan.placements[3].speed_ramp == []
    assert meta.n_speed_ramp == 1
    whooshes = [cue for cue in plan.sfx_cues if cue.kind == "whoosh"]
    assert len(whooshes) == 1
    assert qa.cue_density["whoosh_per_minute"] <= 6


def test_cli_outputs_are_cache_resumable(tmp_path: Path) -> None:
    manifest_path, _, _ = _asset_manifest(tmp_path)
    placements, beats, timings, bank, shots = _planner_inputs(count=2)
    episode_dir = tmp_path / "ep1"
    episode_dir.mkdir()
    episode_dir.joinpath("shots.json").write_text(
        json.dumps([shot.model_dump(mode="json") for shot in shots["ep1"]]), encoding="utf-8"
    )
    edl_path = tmp_path / "edl.json"
    script_path = tmp_path / "series_review_script.json"
    timings_path = tmp_path / "beats_timing.json"
    bank_path = tmp_path / "series_event_bank.json"
    source_map_path = tmp_path / "edl.source_map.json"
    edl_path.write_text(json.dumps([item.model_dump(mode="json") for item in placements]), encoding="utf-8")
    script_path.write_text(json.dumps([item.model_dump(mode="json") for item in beats]), encoding="utf-8")
    timings_path.write_text(json.dumps([item.model_dump(mode="json") for item in timings]), encoding="utf-8")
    bank_path.write_text(bank.model_dump_json(), encoding="utf-8")
    source_map_path.write_text(
        json.dumps({"version": 1, "sources": {"ep1/anime.mp4": str(tmp_path / "anime.mp4")}}), encoding="utf-8"
    )
    output = tmp_path / "edit_plan.json"
    args = argparse.Namespace(
        edl=edl_path,
        series_review_script=script_path,
        event_bank=bank_path,
        beats_timing=timings_path,
        episode_run_dir=[f"ep1={episode_dir}"],
        source_map=source_map_path,
        audio_assets=manifest_path,
        overrides=None,
        output=output,
        output_meta=tmp_path / "edit_plan.meta.json",
        output_qa=tmp_path / "edit_plan.qa.json",
        output_attribution=tmp_path / "audio_attribution.txt",
        seed=1234,
        profile="dynamic_anime",
        work_dir=tmp_path / "work",
        force=False,
        log_level="ERROR",
    )

    assert run_postprocess(args) == 0
    first_mtime = output.stat().st_mtime_ns
    assert run_postprocess(args) == 0
    assert output.stat().st_mtime_ns == first_mtime
    assert json.loads(output.read_text(encoding="utf-8"))["original_audio_included"] is False
    assert (tmp_path / "work" / "cache_manifest.json").is_file()
