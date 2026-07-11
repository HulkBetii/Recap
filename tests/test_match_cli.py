from __future__ import annotations

import argparse
import json

from match.__main__ import content_anchors_allowed, run_match


def write_inputs(tmp_path):  # type: ignore[no-untyped-def]
    review = [
        {"beat_id":0,"narration":"hook","from_seg_id":0,"to_seg_id":0,"src_tc_start":0,"src_tc_end":1,"is_hook":True},
        {"beat_id":1,"narration":"plot","from_seg_id":1,"to_seg_id":1,"src_tc_start":8,"src_tc_end":9,"is_hook":False},
    ]
    timing = [
        {"beat_id":0,"audio_path":"audio/0.mp3","tl_start":0,"tl_end":4,"duration":4},
        {"beat_id":1,"audio_path":"audio/1.mp3","tl_start":4,"tl_end":8,"duration":4},
    ]
    shots = [
        {"src":"film.mp4","index":0,"tc_start":0,"tc_end":5,"duration":5,"thumb":"s0.jpg","motion_score":0.9,"face_count":0,"face_area":0,"brightness":0.5,"is_usable":True},
        {"src":"film.mp4","index":1,"tc_start":5,"tc_end":10,"duration":5,"thumb":"s1.jpg","motion_score":0.8,"face_count":1,"face_area":0.1,"brightness":0.5,"is_usable":True},
    ]
    paths = []
    for name, data in [("review.json", review), ("timing.json", timing), ("shots.json", shots)]:
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        paths.append(path)
    return paths


def make_args(tmp_path, paths, force=False):  # type: ignore[no-untyped-def]
    review, timing, shots = paths
    return argparse.Namespace(
        review_script=review,
        beats_timing=timing,
        shots=shots,
        output=tmp_path / "edl.json",
        min_clip=3.0,
        max_clip=5.0,
        widen_margin=5.0,
        max_widen=2,
        allow_repeat=True,
        allow_speedfit=False,
        seed=1234,
        work_dir=tmp_path / "work" / "match",
        force=force,
        w_motion=0.60,
        w_face=0.18,
        w_bright=0.12,
        w_reuse=0.35,
        w_semantic=0.35,
        min_semantic_score=0.12,
        semantic_mode="off",
        semantic_model="BAAI/bge-m3",
        semantic_device="auto",
        semantic_batch_size=16,
        semantic_cache_dir=None,
        film_map=None,
        output_qa=None,
        log_level="ERROR",
    )


def test_match_cli_outputs_valid_edl_and_meta(tmp_path) -> None:
    paths = write_inputs(tmp_path)
    assert run_match(make_args(tmp_path, paths)) == 0
    assert (tmp_path / "edl.json").exists()
    assert (tmp_path / "edl.meta.json").exists()
    assert (tmp_path / "edl.qa.json").exists()
    meta = json.loads((tmp_path / "edl.meta.json").read_text(encoding="utf-8"))
    assert meta["n_beats_widened"] >= 1
    assert meta["n_placements"] > 0
    assert meta["algorithm_version"] == "4"
    qa = json.loads((tmp_path / "edl.qa.json").read_text(encoding="utf-8"))
    assert "candidate_capacity_s" in qa["beats"][0]
    edl = json.loads((tmp_path / "edl.json").read_text(encoding="utf-8"))
    assert edl[0]["tl_start"] == 0
    assert edl[-1]["tl_end"] == 8


def test_match_cli_uses_cache(tmp_path) -> None:
    paths = write_inputs(tmp_path)
    run_match(make_args(tmp_path, paths))
    run_match(make_args(tmp_path, paths))
    meta = json.loads((tmp_path / "edl.meta.json").read_text(encoding="utf-8"))
    assert meta["cache_hits"] == ["plan.json"]


def test_match_cli_deterministic(tmp_path) -> None:
    paths = write_inputs(tmp_path)
    run_match(make_args(tmp_path, paths))
    first = (tmp_path / "edl.json").read_text(encoding="utf-8")
    run_match(make_args(tmp_path, paths, force=True))
    second = (tmp_path / "edl.json").read_text(encoding="utf-8")
    assert first == second


def test_content_anchors_are_disabled_for_approximate_timecodes(tmp_path) -> None:
    film_map = tmp_path / "film_map.json"
    film_map.write_text("[]", encoding="utf-8")
    film_map.with_name("film_map.meta.json").write_text(json.dumps({"approximate_timecodes": True}), encoding="utf-8")

    assert content_anchors_allowed(film_map) is False


def test_content_anchors_are_disabled_for_corrupt_timecode_meta(tmp_path) -> None:
    film_map = tmp_path / "film_map.json"
    film_map.write_text("[]", encoding="utf-8")
    film_map.with_name("film_map.meta.json").write_text("{broken", encoding="utf-8")

    assert content_anchors_allowed(film_map) is False
