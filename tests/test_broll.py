from __future__ import annotations

import json
from pathlib import Path

from broll.apply import apply_broll_plan, ken_burns_filter
from broll.planner import build_broll_plan, repair_mojibake
from common.schema import EdlPlacement
from render.__main__ import resolve_source_path


def edl_item(tl_start: float, tl_end: float, beat_id: int = 0, shot_index: int = 0, reused: bool = False) -> dict:
    return {
        "tl_start": tl_start,
        "tl_end": tl_end,
        "src": "film.mp4",
        "src_in": tl_start,
        "src_out": tl_end,
        "beat_id": beat_id,
        "shot_index": shot_index,
        "reused": reused,
        "speed": 1.0,
    }


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_broll_plan_prioritizes_reuse_and_ratio(tmp_path: Path) -> None:
    edl = tmp_path / "edl.json"
    placements = [edl_item(0, 2, 0, 0), edl_item(2, 4, 1, 1, True), edl_item(4, 6, 2, 1, True), edl_item(6, 8, 3, 3)]
    write_json(edl, placements)
    qa = tmp_path / "edl.qa.json"
    write_json(qa, {"beats": [{"beat_id": 1, "warnings": ["high repeat ratio"], "selected": []}, {"beat_id": 2, "warnings": ["high repeat ratio"], "selected": []}]})
    sync = tmp_path / "edl.sync.qa.json"
    write_json(sync, {"beats": [{"beat_id": 2, "warnings": ["source_order_mismatch"], "source_order_mismatch": True}]})
    review = tmp_path / "review_script.json"
    write_json(review, [{"beat_id": index, "narration": f"Nội dung beat {index}", "src_tc_start": 0, "src_tc_end": 2, "is_hook": index == 0} for index in range(4)])

    plan = build_broll_plan(
        edl_path=edl,
        qa_path=qa,
        sync_qa_path=sync,
        review_script_path=review,
        review_intent_path=None,
        output_plan_path=tmp_path / "broll_plan.json",
        output_prompts_path=tmp_path / "broll_prompts.jsonl",
        max_replacement_ratio=0.30,
        exclude_opening_s=0,
    )

    assert plan.n_candidates == 1
    assert plan.candidates[0].beat_id == 2
    assert "source_order_mismatch" in plan.candidates[0].reasons
    prompt_text = (tmp_path / "broll_prompts.jsonl").read_text(encoding="utf-8").strip()
    assert prompt_text
    prompt = json.loads(prompt_text)["prompt"]
    assert prompt.isascii()
    assert "no text" in prompt
    assert "no watermark" in prompt


def test_broll_apply_missing_asset_keeps_original(tmp_path: Path) -> None:
    edl = tmp_path / "edl.json"
    write_json(edl, [edl_item(0, 2, 0, 0, True)])
    plan = build_broll_plan(
        edl_path=edl,
        qa_path=None,
        sync_qa_path=None,
        review_script_path=None,
        review_intent_path=None,
        output_plan_path=tmp_path / "broll_plan.json",
        output_prompts_path=tmp_path / "broll_prompts.jsonl",
        max_replacement_ratio=1.0,
        exclude_opening_s=0,
    )
    assert plan.n_candidates == 1

    qa = apply_broll_plan(
        edl_path=edl,
        plan_path=tmp_path / "broll_plan.json",
        asset_dir=tmp_path / "assets",
        clip_dir=tmp_path / "clips",
        output_edl_path=tmp_path / "edl.broll.json",
        output_manifest_path=tmp_path / "broll_manifest.json",
        output_qa_path=tmp_path / "broll.qa.json",
    )

    output = json.loads((tmp_path / "edl.broll.json").read_text(encoding="utf-8"))
    assert qa.n_replaced == 0
    assert qa.n_missing_assets == 1
    assert output[0]["src"] == "film.mp4"


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


def test_broll_prompt_is_english_only_and_preview_repairs_mojibake(tmp_path: Path) -> None:
    edl = tmp_path / "edl.json"
    write_json(edl, [edl_item(0, 1, 0, 0, True)])
    review = tmp_path / "review_script.json"
    expected = "Tr?n Giang v? ??m ??n em"
    mojibake = expected.encode("utf-8").decode("cp1252")
    write_json(review, [{"beat_id": 0, "narration": mojibake, "src_tc_start": 0, "src_tc_end": 1, "is_hook": True}])
    plan = build_broll_plan(
        edl_path=edl,
        qa_path=None,
        sync_qa_path=None,
        review_script_path=review,
        review_intent_path=None,
        output_plan_path=tmp_path / "broll_plan.json",
        output_prompts_path=tmp_path / "broll_prompts.jsonl",
        max_replacement_ratio=1.0,
        exclude_opening_s=0,
    )
    assert expected in plan.candidates[0].narration_preview
    assert plan.candidates[0].prompt.isascii()
    assert "Tr" not in plan.candidates[0].prompt or "Tr?n" not in plan.candidates[0].prompt
    assert repair_mojibake(mojibake) == expected
