from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from common.schema import SeriesReviewBeat
from series_composer.builder import build_event_bank, composer_qa_report, compose_with_client, source_ref_from_event, to_tts_review_script

CREATED_AT = "2026-07-21T00:00:00Z"

def write_manifest(path: Path, source_one: Path, source_two: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "series_id": "grand-blue-s03",
                "series_title": "Grand Blue Season 3",
                "season": 3,
                "episodes": [
                    {
                        "episode_key": "s03e01",
                        "episode_number": 1,
                        "title": "Episode 1",
                        "source_path": str(source_one),
                        "arc": "summer",
                        "spoiler_limit_episode": 1,
                    },
                    {
                        "episode_key": "s03e02",
                        "episode_number": 2,
                        "title": "Episode 2",
                        "source_path": str(source_two),
                        "arc": "summer",
                        "spoiler_limit_episode": 2,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

def score_signals() -> dict[str, float]:
    return {
        "reveal": 0.7,
        "state_change": 0.7,
        "fight_action": 0.2,
        "new_entity": 0.5,
        "continuity_dependency": 0.7,
        "story_density": 0.8,
        "non_story_ratio": 0.1,
        "non_story_penalty": 0.0,
    }

def write_episode_artifacts(
    run_dir: Path,
    *,
    episode_key: str,
    source_path: Path,
    recap_mode: str,
    importance_score: float,
    section_type: str,
    section_summary: str,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("episode_meta.json").write_text(
        json.dumps(
            {
                "series_id": "grand-blue-s03",
                "episode_key": episode_key,
                "episode_number": int(episode_key[-2:]),
                "title": episode_key.upper(),
                "source_path": str(source_path),
                "arc": "summer",
                "spoiler_limit_episode": int(episode_key[-2:]),
                "requested_recap_mode": "auto",
                "recap_mode": recap_mode,
                "importance_score": importance_score,
                "score_signals": score_signals(),
                "score_reasons": ["fixture"],
                "short_circuit": recap_mode in {"merge", "skip"},
                "target_ratio_override": None,
                "quick_target_ratio": 0.12 if recap_mode == "quick" else None,
                "thresholds": {"full": 0.7, "quick": 0.35, "merge": 0.15},
                "previous_memory_count": 0,
                "warnings": [],
                "created_at": CREATED_AT,
            }
        ),
        encoding="utf-8",
    )
    run_dir.joinpath("episode_memory.json").write_text(
        json.dumps(
            {
                "kind": "episode_memory",
                "current": {
                    "series_id": "grand-blue-s03",
                    "episode_key": episode_key,
                    "episode_number": int(episode_key[-2:]),
                    "title": episode_key.upper(),
                    "source_path": str(source_path),
                    "arc": "summer",
                    "recap_mode": recap_mode,
                    "importance_score": importance_score,
                    "summary": f"{episode_key} memory",
                    "entity_hooks": ["Iori", "Chisa"],
                    "arc_hooks": ["diving club"],
                    "important_timecodes": [],
                    "created_at": CREATED_AT,
                },
                "previous": [],
                "spoiler_limit_episode": int(episode_key[-2:]),
                "review_guidance": [],
                "warnings": [],
                "created_at": CREATED_AT,
            }
        ),
        encoding="utf-8",
    )
    run_dir.joinpath("film_map.json").write_text(
        json.dumps(
            [
                {
                    "id": 0,
                    "type": "speech",
                    "tc_start": 0.0,
                    "tc_end": 5.0,
                    "ko": "dialogue",
                    "en": "dialogue",
                    "scene_desc": None,
                },
                {
                    "id": 1,
                    "type": "speech",
                    "tc_start": 5.0,
                    "tc_end": 10.0,
                    "ko": "reveal",
                    "en": "reveal",
                    "scene_desc": None,
                },
            ]
        ),
        encoding="utf-8",
    )
    run_dir.joinpath("film_map.meta.json").write_text(
        json.dumps(
            {
                "input_path": str(source_path),
                "duration": 10.0,
                "created_at": CREATED_AT,
                "whisper_model": "large-v3",
                "translate_model": "gpt-4.1-mini",
                "vision_model": "gpt-4.1-mini",
                "gap_threshold": 4.0,
                "max_vision_frames": 0,
                "speech_count": 2,
                "visual_count": 0,
                "cache_hits": [],
                "warnings_count": 0,
            }
        ),
        encoding="utf-8",
    )
    run_dir.joinpath("story_map.json").write_text(
        json.dumps(
            [
                {
                    "section_id": 0,
                    "type": section_type,
                    "tc_start": 0.0,
                    "tc_end": 10.0,
                    "segment_ids": [0, 1],
                    "summary": section_summary,
                    "characters": ["Iori"],
                    "locations": [],
                    "events": [section_summary],
                    "confidence": 0.9,
                    "warnings": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    run_dir.joinpath("shots.json").write_text("[]", encoding="utf-8")

def test_event_bank_preserves_episode_source_and_mode_target_length(tmp_path: Path) -> None:
    source_one = tmp_path / "Grand_Blue.S03E01.mp4"
    source_two = tmp_path / "Grand_Blue.S03E02.mp4"
    manifest = tmp_path / "series_manifest.json"
    write_manifest(manifest, source_one, source_two)
    write_episode_artifacts(
        tmp_path / "s03e01",
        episode_key="s03e01",
        source_path=source_one,
        recap_mode="full",
        importance_score=0.75,
        section_type="reveal",
        section_summary="Iori finds the key joke that changes the trip.",
    )
    write_episode_artifacts(
        tmp_path / "s03e02",
        episode_key="s03e02",
        source_path=source_two,
        recap_mode="quick",
        importance_score=0.5,
        section_type="setup",
        section_summary="The club resets the stakes for the next chaos.",
    )

    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={"s03e01": tmp_path / "s03e01", "s03e02": tmp_path / "s03e02"},
        tts_cps=10.0,
        mode_target_ratios={"full": 0.1, "quick": 0.05},
    )

    assert bank.episode_keys == ["s03e01", "s03e02"]
    assert bank.target_video_s == pytest.approx(1.5)
    assert bank.char_budget == 15
    assert bank.events[0].episode_key == "s03e01"
    assert bank.events[0].source_path.endswith("Grand_Blue.S03E01.mp4")
    assert bank.events[0].entity_hooks == ["Iori", "Chisa"]
    assert bank.events[1].recap_mode == "quick"

class FakeChatClient:
    async def ask(self, prompt: str) -> str:
        assert "EVENT_BANK" in prompt
        return json.dumps(
            {
                "beats": [
                    {
                        "event_ids": ["s03e01:section:0"],
                        "narration": "Mở đầu là cú xoay làm cả chuyến đi đổi hướng.",
                        "is_hook": True,
                    },
                    {
                        "event_ids": ["s03e02:section:0"],
                        "narration": "Sau đó mọi thứ được nối lại bằng một lời hứa cần nhớ.",
                        "is_hook": False,
                    },
                ]
            }
        )

class SequenceChatClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.responses.pop(0), ensure_ascii=False)

def test_composer_derives_source_refs_and_tts_script(tmp_path: Path) -> None:
    source_one = tmp_path / "Grand_Blue.S03E01.mp4"
    source_two = tmp_path / "Grand_Blue.S03E02.mp4"
    manifest = tmp_path / "series_manifest.json"
    write_manifest(manifest, source_one, source_two)
    write_episode_artifacts(
        tmp_path / "s03e01",
        episode_key="s03e01",
        source_path=source_one,
        recap_mode="full",
        importance_score=0.75,
        section_type="reveal",
        section_summary="Iori finds the key joke.",
    )
    write_episode_artifacts(
        tmp_path / "s03e02",
        episode_key="s03e02",
        source_path=source_two,
        recap_mode="quick",
        importance_score=0.5,
        section_type="setup",
        section_summary="The club resets the stakes.",
    )
    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={"s03e01": tmp_path / "s03e01", "s03e02": tmp_path / "s03e02"},
    )

    beats, meta = asyncio.run(compose_with_client(FakeChatClient(), bank))
    tts_script = to_tts_review_script(beats)

    assert beats[0].source_refs[0].src == "s03e01/Grand_Blue.S03E01.mp4"
    assert beats[1].source_refs[0].src_tc_start == 0.0
    assert meta.selected_event_ids == ["s03e01:section:0", "s03e02:section:0"]
    assert not meta.qa_report
    assert [beat.beat_id for beat in tts_script] == [0, 1]
    assert tts_script[0].src_tc_end == 10.0

def test_composer_revises_under_target_script(tmp_path: Path) -> None:
    source_one = tmp_path / "Grand_Blue.S03E01.mp4"
    source_two = tmp_path / "Grand_Blue.S03E02.mp4"
    manifest = tmp_path / "series_manifest.json"
    write_manifest(manifest, source_one, source_two)
    write_episode_artifacts(
        tmp_path / "s03e01",
        episode_key="s03e01",
        source_path=source_one,
        recap_mode="full",
        importance_score=0.75,
        section_type="reveal",
        section_summary="Iori discovers the trip can still fall apart.",
    )
    write_episode_artifacts(
        tmp_path / "s03e02",
        episode_key="s03e02",
        source_path=source_two,
        recap_mode="quick",
        importance_score=0.5,
        section_type="climax",
        section_summary="The club turns that discovery into a bigger mess.",
    )
    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={"s03e01": tmp_path / "s03e01", "s03e02": tmp_path / "s03e02"},
        tts_cps=200.0,
    )
    long_hook = "Mở đầu bằng cú rơi nhịp của Iori khi kế hoạch tưởng đã ổn bỗng lộ ra một lỗ hổng lớn. " * 2
    long_follow = "Từ đó cả nhóm phải vừa che giấu vừa ứng biến, khiến trò đùa nhỏ biến thành chuỗi hiểu lầm nối thẳng sang tập sau. " * 2
    client = SequenceChatClient(
        [
            {
                "beats": [
                    {"event_ids": ["s03e01:section:0"], "narration": "Quá ngắn.", "is_hook": True},
                    {"event_ids": ["s03e02:section:0"], "narration": "Vẫn quá ngắn.", "is_hook": False},
                ]
            },
            {
                "beats": [
                    {"event_ids": ["s03e01:section:0"], "narration": long_hook, "is_hook": True},
                    {"event_ids": ["s03e02:section:0"], "narration": long_follow, "is_hook": False},
                ]
            },
        ]
    )

    beats, meta = asyncio.run(compose_with_client(client, bank, qa_max_revisions=1))

    assert len(client.prompts) == 2
    assert "QA_REPORT" in client.prompts[1]
    assert sum(len(beat.narration) for beat in beats) >= bank.char_budget * 0.75
    assert meta.qa_report == []
    assert meta.model_versions["qa_revisions"] == "1"

def test_composer_qa_allows_cold_open_before_chronological_story(tmp_path: Path) -> None:
    source_one = tmp_path / "Grand_Blue.S03E01.mp4"
    source_two = tmp_path / "Grand_Blue.S03E02.mp4"
    manifest = tmp_path / "series_manifest.json"
    write_manifest(manifest, source_one, source_two)
    write_episode_artifacts(
        tmp_path / "s03e01",
        episode_key="s03e01",
        source_path=source_one,
        recap_mode="quick",
        importance_score=0.5,
        section_type="setup",
        section_summary="The club sets up the first misunderstanding.",
    )
    write_episode_artifacts(
        tmp_path / "s03e02",
        episode_key="s03e02",
        source_path=source_two,
        recap_mode="quick",
        importance_score=0.5,
        section_type="climax",
        section_summary="The misunderstanding pays off in the second episode.",
    )
    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={"s03e01": tmp_path / "s03e01", "s03e02": tmp_path / "s03e02"},
        tts_cps=1.0,
    )
    beats = [
        SeriesReviewBeat(
            beat_id=0,
            narration="Hook đủ dài để không bị under target.",
            source_refs=[source_ref_from_event(bank.events[1])],
            is_hook=True,
        ),
        SeriesReviewBeat(
            beat_id=1,
            narration="Sau hook, câu chuyện quay lại điểm bắt đầu.",
            source_refs=[source_ref_from_event(bank.events[0])],
            is_hook=False,
        ),
    ]

    report = composer_qa_report(beats, bank)

    assert not report
