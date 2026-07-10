from __future__ import annotations

import json
from pathlib import Path

from broll.apply import apply_broll_plan, ken_burns_filter
from broll.planner import build_broll_plan, repair_mojibake
from common.schema import EdlPlacement
from render.__main__ import resolve_source_path


def edl_item(tl_start: float, tl_end: float, src_in: float, shot_index: int, reused: bool = False, beat_id: int | None = None) -> dict:
    beat = shot_index if beat_id is None else beat_id
    return {
        "tl_start": tl_start,
        "tl_end": tl_end,
        "src": "film.mp4",
        "src_in": src_in,
        "src_out": src_in + (tl_end - tl_start),
        "beat_id": beat,
        "shot_index": shot_index,
        "reused": reused,
        "speed": 1.0,
    }


def shot_item(index: int, start: float, end: float, *, usable: bool = True, story: bool = True, brightness: float = 0.4) -> dict:
    return {
        "src": "film.mp4",
        "index": index,
        "tc_start": start,
        "tc_end": end,
        "duration": end - start,
        "thumb": f"shots/film-{index:03d}.jpg",
        "motion_score": 0.5,
        "face_count": 1,
        "face_area": 0.1,
        "brightness": brightness,
        "is_usable": usable,
        "is_story": story,
        "exclude_reason": None if story else "intro_opening",
    }


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_broll_plan_selects_different_frame_shot_and_prioritizes_reuse(tmp_path: Path) -> None:
    edl = tmp_path / "edl.json"
    placements = [edl_item(0, 2, 0, 0), edl_item(2, 4, 10, 1, True, beat_id=1), edl_item(4, 6, 12, 1, True, beat_id=2), edl_item(6, 8, 30, 3)]
    write_json(edl, placements)
    shots = tmp_path / "shots.json"
    write_json(shots, [shot_item(0, 0, 2), shot_item(1, 10, 14), shot_item(2, 14, 17), shot_item(3, 30, 32)])
    qa = tmp_path / "edl.qa.json"
    write_json(qa, {"beats": [{"beat_id": 1, "warnings": ["high repeat ratio"], "selected": []}, {"beat_id": 2, "warnings": ["high repeat ratio"], "selected": []}]})
    sync = tmp_path / "edl.sync.qa.json"
    write_json(sync, {"beats": [{"beat_id": 2, "warnings": ["source_order_mismatch"], "source_order_mismatch": True}]})
    review = tmp_path / "review_script.json"
    write_json(review, [{"beat_id": index, "narration": f"Nội dung beat {index}", "src_tc_start": 0, "src_tc_end": 2, "is_hook": index == 0} for index in range(4)])

    plan = build_broll_plan(
        edl_path=edl,
        shots_path=shots,
        qa_path=qa,
        sync_qa_path=sync,
        review_script_path=review,
        review_intent_path=None,
        output_plan_path=tmp_path / "broll_plan.json",
        max_replacement_ratio=0.30,
        exclude_opening_s=0,
        min_frame_shot_distance=1,
    )

    assert plan.n_candidates == 1
    assert plan.candidates[0].beat_id == 2
    assert plan.candidates[0].frame_shot_index != plan.candidates[0].shot_index
    assert plan.candidates[0].frame_tc > 0
    payload = json.loads((tmp_path / "broll_plan.json").read_text(encoding="utf-8"))
    assert "prompt" not in payload["candidates"][0]
    assert "frame_tc" in payload["candidates"][0]


def test_broll_plan_ignores_unusable_and_non_story_shots(tmp_path: Path) -> None:
    edl = tmp_path / "edl.json"
    write_json(edl, [edl_item(0, 2, 10, 1, True, beat_id=1)])
    shots = tmp_path / "shots.json"
    write_json(shots, [shot_item(1, 10, 12), shot_item(2, 12, 14, usable=False), shot_item(3, 14, 16, story=False), shot_item(4, 16, 18)])

    plan = build_broll_plan(
        edl_path=edl,
        shots_path=shots,
        output_plan_path=tmp_path / "broll_plan.json",
        max_replacement_ratio=1.0,
        exclude_opening_s=0,
        min_frame_shot_distance=1,
    )

    assert plan.n_candidates == 1
    assert plan.candidates[0].frame_shot_index == 4


def test_broll_apply_extract_failure_keeps_original(tmp_path: Path, monkeypatch) -> None:
    edl = tmp_path / "edl.json"
    write_json(edl, [edl_item(0, 2, 10, 1, True, beat_id=1)])
    shots = tmp_path / "shots.json"
    write_json(shots, [shot_item(1, 10, 12), shot_item(2, 12, 14)])
    plan = build_broll_plan(edl_path=edl, shots_path=shots, output_plan_path=tmp_path / "broll_plan.json", max_replacement_ratio=1.0, exclude_opening_s=0, min_frame_shot_distance=1)
    assert plan.n_candidates == 1
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")

    def fail_extract(*args, **kwargs):
        raise RuntimeError("extract failed")

    monkeypatch.setattr("broll.apply.extract_frame", fail_extract)

    qa = apply_broll_plan(
        edl_path=edl,
        plan_path=tmp_path / "broll_plan.json",
        film_path=film,
        frame_dir=tmp_path / "frames",
        clip_dir=tmp_path / "clips",
        output_edl_path=tmp_path / "edl.broll.json",
        output_manifest_path=tmp_path / "broll_manifest.json",
        output_qa_path=tmp_path / "broll.qa.json",
    )

    output = json.loads((tmp_path / "edl.broll.json").read_text(encoding="utf-8"))
    assert qa.n_replaced == 0
    assert qa.n_failed_frames == 1
    assert qa.n_frame_fallbacks == 1
    assert output[0]["src"] == "film.mp4"


def test_broll_apply_generates_clip_from_extracted_frame(tmp_path: Path, monkeypatch) -> None:
    edl = tmp_path / "edl.json"
    write_json(edl, [edl_item(0, 2, 10, 1, True, beat_id=1)])
    shots = tmp_path / "shots.json"
    write_json(shots, [shot_item(1, 10, 12), shot_item(2, 12, 14)])
    build_broll_plan(edl_path=edl, shots_path=shots, output_plan_path=tmp_path / "broll_plan.json", max_replacement_ratio=1.0, exclude_opening_s=0, min_frame_shot_distance=1)
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")

    def fake_extract(input_path, timestamp, output_path):
        Path(output_path).write_bytes(b"frame")

    def fake_render(**kwargs):
        Path(kwargs["output_path"]).write_bytes(b"clip")

    monkeypatch.setattr("broll.apply.extract_frame", fake_extract)
    monkeypatch.setattr("broll.apply.render_ken_burns_clip", fake_render)

    qa = apply_broll_plan(
        edl_path=edl,
        plan_path=tmp_path / "broll_plan.json",
        film_path=film,
        frame_dir=tmp_path / "frames",
        clip_dir=tmp_path / "clips",
        output_edl_path=tmp_path / "edl.broll.json",
        output_manifest_path=tmp_path / "broll_manifest.json",
        output_qa_path=tmp_path / "broll.qa.json",
    )

    output = json.loads((tmp_path / "edl.broll.json").read_text(encoding="utf-8"))
    assert qa.n_replaced == 1
    assert qa.n_extracted_frames == 1
    assert output[0]["src"].endswith(".mp4")


def test_ken_burns_filter_contains_zoompan_and_limits() -> None:
    text = ken_burns_filter(width=1920, height=1080, fps=30, duration_s=2.0, preset="zoom_in")
    assert "zoompan=" in text
    assert "s=1920x1080" in text
    assert "trim=duration=2.000000" in text
    assert "format=yuv420p" in text


def test_render_resolve_source_prefers_existing_placement_src(tmp_path: Path) -> None:
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    broll = tmp_path / "broll.mp4"
    broll.write_bytes(b"broll")
    placement = EdlPlacement(tl_start=0, tl_end=1, src=str(broll), src_in=0, src_out=1, beat_id=0, shot_index=0, speed=1.0)
    assert resolve_source_path(placement, film) == broll.resolve()
    missing = EdlPlacement(tl_start=0, tl_end=1, src="film.mp4", src_in=0, src_out=1, beat_id=0, shot_index=0, speed=1.0)
    assert resolve_source_path(missing, film) == film.resolve()


def test_preview_repairs_mojibake(tmp_path: Path) -> None:
    mojibake = "Tráº§n Giang"
    assert repair_mojibake(mojibake) == "Trần Giang"


def test_broll_plan_skips_short_duration_and_records_qa(tmp_path: Path, monkeypatch) -> None:
    edl = tmp_path / "edl.json"
    write_json(edl, [edl_item(0, 0.8, 10, 1, True, beat_id=1), edl_item(0.8, 2.8, 20, 5, True, beat_id=2)])
    shots = tmp_path / "shots.json"
    write_json(shots, [shot_item(1, 10, 12), shot_item(5, 20, 22), shot_item(10, 30, 34)])
    plan = build_broll_plan(edl_path=edl, shots_path=shots, output_plan_path=tmp_path / "broll_plan.json", max_replacement_ratio=1.0, exclude_opening_s=0, min_broll_duration_s=1.0)
    assert plan.n_skipped_short_duration == 1
    assert all(candidate.duration_s >= 1.0 for candidate in plan.candidates)
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    monkeypatch.setattr("broll.apply.extract_frame", lambda input_path, timestamp, output_path: Path(output_path).write_bytes(b"frame"))
    monkeypatch.setattr("broll.apply.render_ken_burns_clip", lambda **kwargs: Path(kwargs["output_path"]).write_bytes(b"clip"))
    qa = apply_broll_plan(edl_path=edl, plan_path=tmp_path / "broll_plan.json", film_path=film, frame_dir=tmp_path / "frames", clip_dir=tmp_path / "clips", output_edl_path=tmp_path / "edl.broll.json", output_manifest_path=tmp_path / "broll_manifest.json", output_qa_path=tmp_path / "broll.qa.json")
    assert qa.n_skipped_short_duration == 1


def test_broll_frame_selector_respects_distance_and_reuse_window(tmp_path: Path) -> None:
    edl = tmp_path / "edl.json"
    write_json(edl, [edl_item(0, 2, 100, 10, True, beat_id=1), edl_item(5, 7, 104, 11, True, beat_id=2)])
    shots = tmp_path / "shots.json"
    write_json(shots, [
        shot_item(10, 100, 102),
        shot_item(11, 104, 106),
        shot_item(12, 106, 108),
        shot_item(14, 110, 112),
        shot_item(18, 116, 118),
    ])
    plan = build_broll_plan(edl_path=edl, shots_path=shots, output_plan_path=tmp_path / "broll_plan.json", max_replacement_ratio=1.0, max_broll_per_parent_beat=2, exclude_opening_s=0, min_frame_shot_distance=3, frame_reuse_window_s=20)
    assert plan.n_candidates == 2
    assert all(abs(candidate.frame_shot_index - candidate.shot_index) >= 3 for candidate in plan.candidates)
    assert len({candidate.frame_shot_index for candidate in plan.candidates}) == len(plan.candidates)


def test_short_broll_uses_still_soft_zoom() -> None:
    from broll.apply import motion_preset

    assert motion_preset("bf_short", 1.2) == "still_soft_zoom"
    assert "1.025" in ken_burns_filter(width=1920, height=1080, fps=30, duration_s=1.2, preset="still_soft_zoom")
