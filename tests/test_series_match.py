from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from common.schema import BeatTiming, SeriesReviewBeat, Shot
from series_match.__main__ import SeriesMatchError, build_edl, choose_clip_duration, run_series_match

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
    meta = json.loads(output.with_name("edl.meta.json").read_text(encoding="utf-8"))
    qa = json.loads((tmp_path / "edl.qa.json").read_text(encoding="utf-8"))

    assert {placement["src"] for placement in edl} == {
        "s03e01/Grand_Blue.S03E01.mp4",
        "s03e02/Grand_Blue.S03E02.mp4",
    }
    assert edl[0]["shot_index"] == 1
    assert sources["s03e01/Grand_Blue.S03E01.mp4"] == str(source_one.resolve())
    assert sources["s03e02/Grand_Blue.S03E02.mp4"] == str(source_two.resolve())
    assert meta["algorithm_version"] == "series-v2"
    assert qa["algorithm_version"] == "series-v2"
    assert qa["beats"][0]["covered_event_ids"] == ["s03e01:section:0"]

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
    qa = json.loads((tmp_path / "edl.qa.json").read_text(encoding="utf-8"))

    assert [round(item["tl_end"] - item["tl_start"], 3) for item in edl] == [4.8, 0.6]
    assert edl[-1]["shot_index"] == 2
    assert qa["beats"][0]["short_fallback_diagnostics"] == [
        {
            "event_id": "s03e01:section:0",
            "episode_key": "s03e01",
            "shot_index": 2,
            "duration_s": 0.6,
            "reason": "candidate_below_min_clip",
            "episode_fallback": True,
            "reused": False,
        }
    ]

def test_choose_clip_skips_candidate_that_leaves_unfillable_tail() -> None:
    assert choose_clip_duration(
        available=0.668,
        remaining=0.974,
        max_clip=5.0,
        min_visual_clip=0.6,
    ) == 0.0


def source_ref(
    *,
    event_id: str,
    episode_key: str,
    start: float = 0.0,
    end: float = 10.0,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "episode_key": episode_key,
        "src": f"{episode_key}/{episode_key}.mp4",
        "source_path": f"C:/{episode_key}.mp4",
        "from_seg_id": 0,
        "to_seg_id": 1,
        "src_tc_start": start,
        "src_tc_end": end,
    }


def schema_shots(episode_key: str, spans: list[tuple[float, float]]) -> list[Shot]:
    return [
        Shot.model_validate(shot(src=f"{episode_key}.mp4", index=index, start=start, end=end))
        for index, (start, end) in enumerate(spans)
    ]


def build_test_edl(
    *,
    refs: list[dict[str, object]],
    duration: float,
    shots_by_episode: dict[str, list[Shot]],
    min_clip: float = 3.0,
    min_visual_clip: float = 0.6,
    qa_beats: list[dict[str, object]] | None = None,
):
    beat = SeriesReviewBeat.model_validate(
        {
            "beat_id": 0,
            "narration": "Một beat tổng hợp nhiều sự kiện.",
            "is_hook": True,
            "source_refs": refs,
        }
    )
    timing = BeatTiming.model_validate(
        {
            "beat_id": 0,
            "audio_path": "audio/0.mp3",
            "tl_start": 0.0,
            "tl_end": duration,
            "duration": duration,
        }
    )
    return build_edl(
        beats=[beat],
        timings=[timing],
        shots_by_episode=shots_by_episode,
        min_clip=min_clip,
        max_clip=5.0,
        min_visual_clip=min_visual_clip,
        qa_beats=qa_beats,
    )


def test_multi_event_beat_allocates_every_ref_in_declared_order() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, warnings = build_test_edl(
        refs=[
            source_ref(event_id="ep1:event", episode_key="ep1"),
            source_ref(event_id="ep2:event", episode_key="ep2"),
        ],
        duration=8.0,
        shots_by_episode={
            "ep1": schema_shots("ep1", [(0.0, 10.0)]),
            "ep2": schema_shots("ep2", [(0.0, 10.0)]),
        },
        qa_beats=qa_beats,
    )

    assert [placement.src for placement in placements] == ["ep1/ep1.mp4", "ep2/ep2.mp4"]
    assert [placement.tl_end - placement.tl_start for placement in placements] == pytest.approx([4.0, 4.0])
    assert qa_beats[0]["requested_event_ids"] == ["ep1:event", "ep2:event"]
    assert qa_beats[0]["covered_event_ids"] == ["ep1:event", "ep2:event"]
    assert qa_beats[0]["missing_event_ids"] == []
    assert warnings == []


def test_three_refs_receive_fair_quotas() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, _ = build_test_edl(
        refs=[source_ref(event_id=f"ep{i}:event", episode_key=f"ep{i}") for i in range(1, 4)],
        duration=9.0,
        shots_by_episode={f"ep{i}": schema_shots(f"ep{i}", [(0.0, 10.0)]) for i in range(1, 4)},
        qa_beats=qa_beats,
    )

    assert [placement.src for placement in placements] == ["ep1/ep1.mp4", "ep2/ep2.mp4", "ep3/ep3.mp4"]
    assert qa_beats[0]["quotas_s"] == {"ep1:event": 3.0, "ep2:event": 3.0, "ep3:event": 3.0}


def test_same_episode_refs_prefer_unused_source_ranges() -> None:
    placements, _ = build_test_edl(
        refs=[
            source_ref(event_id="ep1:first", episode_key="ep1", start=0.0, end=4.0),
            source_ref(event_id="ep1:second", episode_key="ep1", start=4.0, end=8.0),
        ],
        duration=6.0,
        shots_by_episode={"ep1": schema_shots("ep1", [(0.0, 4.0), (4.0, 8.0)])},
    )

    assert [(placement.src_in, placement.src_out) for placement in placements] == [(0.0, 3.0), (4.0, 7.0)]
    assert all(not placement.reused for placement in placements)


def test_same_episode_overlap_reuses_only_after_unique_capacity_is_exhausted() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, _ = build_test_edl(
        refs=[
            source_ref(event_id="ep1:first", episode_key="ep1", start=0.0, end=3.0),
            source_ref(event_id="ep1:second", episode_key="ep1", start=0.0, end=3.0),
        ],
        duration=6.0,
        shots_by_episode={"ep1": schema_shots("ep1", [(0.0, 3.0)])},
        qa_beats=qa_beats,
    )

    assert [placement.reused for placement in placements] == [False, True]
    assert qa_beats[0]["fallback_event_ids"] == ["ep1:second"]


def test_quota_is_capped_by_capacity_and_redistributed() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, _ = build_test_edl(
        refs=[
            source_ref(event_id="short:event", episode_key="short"),
            source_ref(event_id="long:event", episode_key="long"),
        ],
        duration=5.0,
        shots_by_episode={
            "short": schema_shots("short", [(0.0, 1.0)]),
            "long": schema_shots("long", [(0.0, 5.0)]),
        },
        qa_beats=qa_beats,
    )

    assert qa_beats[0]["quotas_s"] == {"short:event": 1.0, "long:event": 4.0}
    assert [placement.tl_end - placement.tl_start for placement in placements] == pytest.approx([1.0, 4.0])


def test_beat_shorter_than_hard_floor_for_all_refs_fails() -> None:
    with pytest.raises(SeriesMatchError, match="cannot represent 2 source refs"):
        build_test_edl(
            refs=[
                source_ref(event_id="ep1:event", episode_key="ep1"),
                source_ref(event_id="ep2:event", episode_key="ep2"),
            ],
            duration=1.1,
            shots_by_episode={
                "ep1": schema_shots("ep1", [(0.0, 5.0)]),
                "ep2": schema_shots("ep2", [(0.0, 5.0)]),
            },
        )


def test_ref_without_minimum_footage_fails_with_event_id() -> None:
    with pytest.raises(SeriesMatchError, match="beat 0 event ep2:event"):
        build_test_edl(
            refs=[
                source_ref(event_id="ep1:event", episode_key="ep1"),
                source_ref(event_id="ep2:event", episode_key="ep2"),
            ],
            duration=4.0,
            shots_by_episode={
                "ep1": schema_shots("ep1", [(0.0, 5.0)]),
                "ep2": schema_shots("ep2", [(0.0, 0.5)]),
            },
        )


def test_strict_window_then_episode_fallback_is_reported() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, warnings = build_test_edl(
        refs=[source_ref(event_id="ep1:event", episode_key="ep1", start=0.0, end=0.5)],
        duration=3.0,
        shots_by_episode={"ep1": schema_shots("ep1", [(0.0, 0.5), (1.0, 5.0)])},
        qa_beats=qa_beats,
    )

    assert placements[0].shot_index == 1
    assert qa_beats[0]["fallback_event_ids"] == ["ep1:event"]
    assert "used episode-level fallback" in warnings[0]


def test_episode_normal_candidate_wins_over_short_strict_candidate() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, _ = build_test_edl(
        refs=[source_ref(event_id="ep1:event", episode_key="ep1", start=0.0, end=0.8)],
        duration=6.0,
        shots_by_episode={"ep1": schema_shots("ep1", [(0.0, 0.8), (1.0, 8.0)])},
        qa_beats=qa_beats,
    )

    assert [placement.shot_index for placement in placements] == [1, 1]
    assert [placement.tl_end - placement.tl_start for placement in placements] == pytest.approx([3.0, 3.0])
    assert qa_beats[0]["fallback_event_ids"] == ["ep1:event"]
    assert qa_beats[0]["short_fallbacks"] == []


def test_min_clip_prefers_normal_candidate_and_balances_long_split() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, _ = build_test_edl(
        refs=[source_ref(event_id="ep1:event", episode_key="ep1")],
        duration=6.0,
        shots_by_episode={"ep1": schema_shots("ep1", [(0.0, 0.8), (1.0, 11.0)])},
        qa_beats=qa_beats,
    )

    assert [placement.shot_index for placement in placements] == [1, 1]
    assert [placement.tl_end - placement.tl_start for placement in placements] == pytest.approx([3.0, 3.0])
    assert qa_beats[0]["short_fallbacks"] == []


def test_min_clip_balances_three_clips_without_a_short_tail() -> None:
    qa_beats: list[dict[str, object]] = []
    placements, _ = build_test_edl(
        refs=[source_ref(event_id="ep1:event", episode_key="ep1", end=20.0)],
        duration=10.5,
        shots_by_episode={"ep1": schema_shots("ep1", [(0.0, 20.0)])},
        qa_beats=qa_beats,
    )

    assert [placement.tl_end - placement.tl_start for placement in placements] == pytest.approx([3.5, 3.5, 3.5])
    assert qa_beats[0]["short_fallbacks"] == []


@pytest.mark.parametrize(
    ("min_visual_clip", "min_clip", "max_clip"),
    [(0.0, 3.0, 5.0), (1.0, 0.9, 5.0), (0.6, 6.0, 5.0)],
)
def test_invalid_clip_length_order_fails(
    min_visual_clip: float,
    min_clip: float,
    max_clip: float,
) -> None:
    with pytest.raises(SeriesMatchError, match="min_visual_clip <= min_clip <= max_clip"):
        build_test_edl(
            refs=[source_ref(event_id="ep1:event", episode_key="ep1")],
            duration=3.0,
            shots_by_episode={"ep1": schema_shots("ep1", [(0.0, 5.0)])},
            min_clip=min_clip,
            min_visual_clip=min_visual_clip,
        )
