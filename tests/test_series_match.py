from __future__ import annotations

import argparse
import json
from pathlib import Path

from series_match.__main__ import run_series_match

CREATED_AT = "2026-07-21T00:00:00Z"

def shot(
    *,
    src: str,
    index: int,
    start: float,
    end: float,
    is_story: bool = True,
    is_usable: bool = True,
) -> dict[str, object]:
    return {
        "src": src,
        "index": index,
        "tc_start": start,
        "tc_end": end,
        "duration": end - start,
        "thumb": f"shots/{index:03d}.jpg",
        "motion_score": 0.7,
        "face_count": 0,
        "face_area": 0.0,
        "brightness": 0.5,
        "is_usable": is_usable,
        "unusable_reasons": [],
        "is_story": is_story,
        "exclude_reason": None if is_story else "opening_theme",
        "is_end_credit": False,
        "credit_like_score": 0.0,
    }

def write_shots(run_dir: Path, src: str, *, non_story_first: bool = False) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    shots = [
        shot(src=src, index=0, start=0.0, end=3.0, is_story=not non_story_first),
        shot(src=src, index=1, start=3.0, end=6.0, is_story=True),
    ]
    run_dir.joinpath("shots.json").write_text(json.dumps(shots), encoding="utf-8")

def test_series_match_writes_multi_source_edl_and_excludes_non_story(tmp_path: Path) -> None:
    source_one = tmp_path / "Grand_Blue.S03E01.mp4"
    source_two = tmp_path / "Grand_Blue.S03E02.mp4"
    write_shots(tmp_path / "s03e01", "Grand_Blue.S03E01.mp4", non_story_first=True)
    write_shots(tmp_path / "s03e02", "Grand_Blue.S03E02.mp4")
    script = [
        {
            "beat_id": 0,
            "narration": "Hook nối cú mở đầu.",
            "is_hook": True,
            "source_refs": [
                {
                    "event_id": "s03e01:section:0",
                    "episode_key": "s03e01",
                    "src": "s03e01/Grand_Blue.S03E01.mp4",
                    "source_path": str(source_one),
                    "from_seg_id": 0,
                    "to_seg_id": 1,
                    "src_tc_start": 0.0,
                    "src_tc_end": 6.0,
                }
            ],
        },
        {
            "beat_id": 1,
            "narration": "Tập sau nối tiếp bằng một tình huống mới.",
            "is_hook": False,
            "source_refs": [
                {
                    "event_id": "s03e02:section:0",
                    "episode_key": "s03e02",
                    "src": "s03e02/Grand_Blue.S03E02.mp4",
                    "source_path": str(source_two),
                    "from_seg_id": 0,
                    "to_seg_id": 1,
                    "src_tc_start": 0.0,
                    "src_tc_end": 6.0,
                }
            ],
        },
    ]
    timings = [
        {"beat_id": 0, "audio_path": "audio/0.mp3", "tl_start": 0.0, "tl_end": 2.0, "duration": 2.0},
        {"beat_id": 1, "audio_path": "audio/1.mp3", "tl_start": 2.0, "tl_end": 4.0, "duration": 2.0},
    ]
    script_path = tmp_path / "series_review_script.json"
    timing_path = tmp_path / "beats_timing.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    timing_path.write_text(json.dumps(timings), encoding="utf-8")
    output = tmp_path / "edl.json"
    source_map = tmp_path / "edl.source_map.json"

    args = argparse.Namespace(
        series_review_script=script_path,
        beats_timing=timing_path,
        episode_run_dir=[f"s03e01={tmp_path / 's03e01'}", f"s03e02={tmp_path / 's03e02'}"],
        output=output,
        output_source_map=source_map,
        output_qa=tmp_path / "edl.qa.json",
        min_clip=3.0,
        max_clip=5.0,
        min_visual_clip=0.6,
        log_level="ERROR",
        work_dir=tmp_path / "work",
    )

    assert run_series_match(args) == 0
    edl = json.loads(output.read_text(encoding="utf-8"))
    sources = json.loads(source_map.read_text(encoding="utf-8"))["sources"]

    assert {placement["src"] for placement in edl} == {
        "s03e01/Grand_Blue.S03E01.mp4",
        "s03e02/Grand_Blue.S03E02.mp4",
    }
    assert edl[0]["shot_index"] == 1
    assert sources["s03e01/Grand_Blue.S03E01.mp4"] == str(source_one.resolve())
    assert sources["s03e02/Grand_Blue.S03E02.mp4"] == str(source_two.resolve())

def test_series_match_avoids_sub_min_visual_tail_fragment(tmp_path: Path) -> None:
    source = tmp_path / "Grand_Blue.S03E01.mp4"
    run_dir = tmp_path / "s03e01"
    run_dir.mkdir(parents=True)
    shots = [
        shot(src="Grand_Blue.S03E01.mp4", index=0, start=0.0, end=5.0),
        shot(src="Grand_Blue.S03E01.mp4", index=1, start=5.0, end=5.4),
        shot(src="Grand_Blue.S03E01.mp4", index=2, start=6.0, end=8.0),
    ]
    run_dir.joinpath("shots.json").write_text(json.dumps(shots), encoding="utf-8")
    script = [
        {
            "beat_id": 0,
            "narration": "Hook cần đủ footage nhưng không được tạo flash-cut.",
            "is_hook": True,
            "source_refs": [
                {
                    "event_id": "s03e01:section:0",
                    "episode_key": "s03e01",
                    "src": "s03e01/Grand_Blue.S03E01.mp4",
                    "source_path": str(source),
                    "from_seg_id": 0,
                    "to_seg_id": 1,
                    "src_tc_start": 0.0,
                    "src_tc_end": 5.4,
                }
            ],
        }
    ]
    timings = [{"beat_id": 0, "audio_path": "audio/0.mp3", "tl_start": 0.0, "tl_end": 5.4, "duration": 5.4}]
    script_path = tmp_path / "series_review_script.json"
    timing_path = tmp_path / "beats_timing.json"
    output = tmp_path / "edl.json"
    source_map = tmp_path / "edl.source_map.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    timing_path.write_text(json.dumps(timings), encoding="utf-8")

    args = argparse.Namespace(
        series_review_script=script_path,
        beats_timing=timing_path,
        episode_run_dir=[f"s03e01={run_dir}"],
        output=output,
        output_source_map=source_map,
        output_qa=tmp_path / "edl.qa.json",
        min_clip=3.0,
        max_clip=5.0,
        min_visual_clip=0.6,
        log_level="ERROR",
        work_dir=tmp_path / "work",
    )

    assert run_series_match(args) == 0
    edl = json.loads(output.read_text(encoding="utf-8"))

    assert [round(item["tl_end"] - item["tl_start"], 3) for item in edl] == [4.8, 0.6]
    assert edl[-1]["shot_index"] == 2
