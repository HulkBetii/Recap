from __future__ import annotations

import argparse
import json

from match.anchors import ContentAnchorPlan
from match.__main__ import content_anchors_allowed, run_match
from match.intra_beat import IntraBeatAlignmentResult
from match.refinement_ab import run_sentence_refinement_ab
from match.semantic import SemanticResult
from match.version import MATCH_ALGORITHM_VERSION


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
    assert meta["algorithm_version"] == MATCH_ALGORITHM_VERSION
    qa = json.loads((tmp_path / "edl.qa.json").read_text(encoding="utf-8"))
    assert "candidate_capacity_s" in qa["beats"][0]
    assert qa["sentence_refinement_mode"] == "off"
    assert "sentence_refinement_summary" in qa
    edl = json.loads((tmp_path / "edl.json").read_text(encoding="utf-8"))
    assert edl[0]["tl_start"] == 0
    assert edl[-1]["tl_end"] == 8


def test_match_cli_uses_cache(tmp_path) -> None:
    paths = write_inputs(tmp_path)
    run_match(make_args(tmp_path, paths))
    run_match(make_args(tmp_path, paths))
    meta = json.loads((tmp_path / "edl.meta.json").read_text(encoding="utf-8"))
    assert meta["cache_hits"] == ["plan.json"]


def test_match_cache_invalidates_sentence_refinement_mode(tmp_path) -> None:
    paths = write_inputs(tmp_path)
    run_match(make_args(tmp_path, paths))
    args = make_args(tmp_path, paths)
    args.sentence_refinement_mode = "guarded"

    run_match(args)

    meta = json.loads((tmp_path / "edl.meta.json").read_text(encoding="utf-8"))
    qa = json.loads((tmp_path / "edl.qa.json").read_text(encoding="utf-8"))
    assert meta["cache_hits"] == []
    assert qa["sentence_refinement_requested_mode"] == "guarded"


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

def test_long_beat_alignment_skips_clean_content_anchor_plan(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    review = tmp_path / "review.json"
    timing = tmp_path / "timing.json"
    shots = tmp_path / "shots.json"
    film_map = tmp_path / "film_map.json"
    review.write_text(json.dumps([{
        "beat_id": 0,
        "narration": "First sentence is long enough. Second sentence keeps going. Third sentence changes the focus. Fourth sentence resolves it.",
        "from_seg_id": 0,
        "to_seg_id": 3,
        "src_tc_start": 0,
        "src_tc_end": 240,
        "is_hook": False,
    }]), encoding="utf-8")
    timing.write_text(json.dumps([{"beat_id": 0, "audio_path": "0.mp3", "tl_start": 0, "tl_end": 60, "duration": 60}]), encoding="utf-8")
    shots.write_text(json.dumps([
        {
            "src": "film.mp4",
            "index": index,
            "tc_start": 120 + index * 5,
            "tc_end": 125 + index * 5,
            "duration": 5,
            "thumb": f"{index}.jpg",
            "motion_score": 1.0,
            "face_count": 0,
            "face_area": 0,
            "brightness": 0.5,
            "is_usable": True,
        }
        for index in range(12)
    ]), encoding="utf-8")
    film_map.write_text(json.dumps([
        {"id": index, "type": "speech", "tc_start": index * 60, "tc_end": index * 60 + 10, "ko": f"segment {index}", "en": f"segment {index}"}
        for index in range(4)
    ]), encoding="utf-8")
    args = make_args(tmp_path, [review, timing, shots], force=True)
    args.film_map = film_map
    args.semantic_mode = "bge-m3"
    args.content_anchors = True
    args.opening_intra_beat_align = True
    args.sentence_refinement_mode = "guarded"
    args.match_strategy = "chronological"

    monkeypatch.setattr(
        "match.__main__.compute_semantic_result",
        lambda *args, **kwargs: SemanticResult(
            scores={(0, index): 0.5 for index in range(12)},
            segment_scores={(0, index): 0.8 for index in range(4)},
            query_shot_scores={(0, 0, 0): 0.7},
            provider="bge-m3",
        ),
    )
    monkeypatch.setattr(
        "match.__main__.plan_content_anchors",
        lambda **kwargs: ContentAnchorPlan(
            intervals=[(120.0, 180.0)],
            candidate_ids=set(range(12)),
            dark_candidate_ids=set(),
            segment_ids=[0, 1, 2, 3],
            capacity_s=60.0,
            threshold=0.35,
        ),
    )
    calls = []

    def fake_apply_intra_beat_alignment(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs["beat"].beat_id)
        assert kwargs["mode"] == "content_anchor_long_beat"
        return IntraBeatAlignmentResult(kwargs["baseline_placements"], [], [], [])

    monkeypatch.setattr("match.__main__.apply_intra_beat_alignment", fake_apply_intra_beat_alignment)

    assert run_match(args) == 0
    assert calls == []


def test_sentence_refinement_ab_writes_report_without_main_output(tmp_path) -> None:
    paths = write_inputs(tmp_path)
    args = make_args(tmp_path, paths)
    args.output.write_text("main-edl", encoding="utf-8")
    calls = []

    def fake_runner(run_args):  # type: ignore[no-untyped-def]
        calls.append(run_args.sentence_refinement_mode)
        run_args.output.parent.mkdir(parents=True, exist_ok=True)
        run_args.output.write_text("[]", encoding="utf-8")
        run_args.output.with_name("edl.meta.json").write_text(
            json.dumps({
                "algorithm_version": MATCH_ALGORITHM_VERSION,
                "n_placements": 1,
                "coverage_ok": True,
            }),
            encoding="utf-8",
        )
        drift = 20.0 if run_args.sentence_refinement_mode == "off" else 5.0
        run_args.output_qa.write_text(
            json.dumps({
                "sentence_refinement_summary": {"used_beats": int(run_args.sentence_refinement_mode == "guarded")},
                "beats": [{
                    "beat_id": 0,
                    "narration_preview": "beat",
                    "max_source_drift_s": drift,
                    "repeat_ratio": 0.0,
                    "short_clip_count": 0,
                    "warnings": [],
                    "sentence_refinement_used": run_args.sentence_refinement_mode == "guarded",
                    "sentence_refinement_reason": "eligible",
                    "sentence_refinement_replaced_duration_s": 10.0,
                    "sentence_refinement_max_source_jump_s": 0.0,
                    "sentence_refinement_avg_source_jump_s": 0.0,
                    "sentence_refinement_low_confidence_count": 0,
                }],
            }),
            encoding="utf-8",
        )
        return 0

    assert run_sentence_refinement_ab(args, fake_runner) == 0
    assert calls == ["off", "guarded"]
    assert args.output.read_text(encoding="utf-8") == "main-edl"
    report = json.loads((tmp_path / "match_refinement_ab.qa.json").read_text(encoding="utf-8"))
    assert report["summary"]["improved_beats"] == [0]
    assert (tmp_path / "match_refinement_ab.html").exists()


def test_match_cli_hard_excludes_end_credit_shots(tmp_path) -> None:
    review = tmp_path / "review.json"
    timing = tmp_path / "timing.json"
    shots = tmp_path / "shots.json"
    review.write_text(json.dumps([{"beat_id": 0, "narration": "ending", "from_seg_id": 0, "to_seg_id": 0, "src_tc_start": 0, "src_tc_end": 10, "is_hook": False}]), encoding="utf-8")
    timing.write_text(json.dumps([{"beat_id": 0, "audio_path": "0.mp3", "tl_start": 0, "tl_end": 5, "duration": 5}]), encoding="utf-8")
    shots.write_text(json.dumps([
        {"src": "film.mp4", "index": 0, "tc_start": 0, "tc_end": 5, "duration": 5, "thumb": "0.jpg", "motion_score": 1.0, "face_count": 0, "face_area": 0, "brightness": 0.5, "is_usable": True, "is_end_credit": True, "credit_like_score": 1.0},
        {"src": "film.mp4", "index": 1, "tc_start": 5, "tc_end": 10, "duration": 5, "thumb": "1.jpg", "motion_score": 0.2, "face_count": 0, "face_area": 0, "brightness": 0.5, "is_usable": True},
    ]), encoding="utf-8")
    args = make_args(tmp_path, [review, timing, shots])
    args.exclude_end_credits = True

    assert run_match(args) == 0
    edl = json.loads((tmp_path / "edl.json").read_text(encoding="utf-8"))
    meta = json.loads((tmp_path / "edl.meta.json").read_text(encoding="utf-8"))
    qa = json.loads((tmp_path / "edl.qa.json").read_text(encoding="utf-8"))
    assert {item["shot_index"] for item in edl} == {1}
    assert meta["n_end_credit_excluded"] == 1
    assert qa["end_credit_guard_enabled"] is True
    assert qa["excluded_end_credit_candidates"] == [0]
