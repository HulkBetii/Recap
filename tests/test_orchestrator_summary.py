from __future__ import annotations

import json
from pathlib import Path

from orchestrator.summary import StageSummary, build_timecode_qa, collect_warnings, write_summary


def test_build_timecode_qa_passes_for_strict_timecodes() -> None:
    qa = build_timecode_qa(
        {
            "timecode_quality": "strict",
            "approximate_timecodes": False,
            "asr_provider": "openai-gpt4o-hybrid",
            "aligner_provider": "whisperx",
            "source_language": "vi",
            "translate_mode": "none",
            "speech_count": 10,
            "visual_count": 2,
            "asr_warnings": [],
        }
    )
    assert qa["status"] == "pass"
    assert qa["risk_level"] == "low"
    assert qa["recommended_next_step"] is None


def test_build_timecode_qa_warns_for_approximate_timecodes() -> None:
    qa = build_timecode_qa(
        {
            "timecode_quality": "approximate",
            "approximate_timecodes": True,
            "asr_provider": "openai-gpt4o-hybrid",
            "aligner_provider": "none",
            "source_language": "vi",
            "translate_mode": "none",
            "speech_count": 10,
            "visual_count": 2,
            "asr_warnings": ["rough chunk timestamps"],
        }
    )
    assert qa["status"] == "warn"
    assert qa["risk_level"] == "medium"
    assert qa["asr_warning_count"] == 1
    assert "whisperx" in qa["recommended_next_step"]


def test_summary_includes_timecode_qa_and_warning(tmp_path: Path) -> None:
    film_meta = tmp_path / "film_map.meta.json"
    film_meta.write_text(
        json.dumps(
            {
                "timecode_quality": "approximate",
                "approximate_timecodes": True,
                "asr_provider": "openai-gpt4o-hybrid",
                "aligner_provider": "none",
                "source_language": "vi",
                "translate_mode": "none",
                "speech_count": 10,
                "visual_count": 2,
                "asr_warnings": ["rough chunk timestamps"],
                "warnings_count": 0,
            }
        ),
        encoding="utf-8",
    )
    tts = tmp_path / "tts_meta.json"
    tts.write_text(json.dumps({"real_ratio": 0.2}), encoding="utf-8")
    edl = tmp_path / "edl.meta.json"
    edl.write_text(json.dumps({"n_beats_widened": 0}), encoding="utf-8")
    render = tmp_path / "render.meta.json"
    render.write_text(json.dumps({"duration_match": True}), encoding="utf-8")
    for name in ["video_profile.json", "story_map.meta.json", "review_script.meta.json", "shots.meta.json"]:
        (tmp_path / name).write_text(json.dumps({"warnings": []}), encoding="utf-8")

    summary = write_summary(
        path=tmp_path / "summary.json",
        stages=[StageSummary(stage="ingest", status="ran")],
        meta_paths={
            "preflight": tmp_path / "video_profile.json",
            "ingest": film_meta,
            "storymap": tmp_path / "story_map.meta.json",
            "review": tmp_path / "review_script.meta.json",
            "tts": tts,
            "shots": tmp_path / "shots.meta.json",
            "match": edl,
            "render": render,
        },
    )
    assert summary["timecode_qa"]["status"] == "warn"
    assert any("approximate_timecodes=true" in warning for warning in summary["warnings"])


def test_collect_warnings_marks_approximate_film_map_meta(tmp_path: Path) -> None:
    path = tmp_path / "film_map.meta.json"
    path.write_text(json.dumps({"approximate_timecodes": True, "warnings_count": 0}), encoding="utf-8")
    assert collect_warnings([path]) == [
        "film_map.meta.json: approximate_timecodes=true; footage matching may be less precise"
    ]
