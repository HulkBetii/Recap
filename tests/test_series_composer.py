from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from common.schema import SeriesReviewBeat
from series_composer.builder import (
    SeasonTargetSettings,
    arc_indexes_for_revision,
    build_arc_composer_prompt,
    build_event_bank,
    build_season_target_plan,
    build_series_arc_plan,
    build_series_chapters,
    combine_arc_drafts,
    composer_qa_report,
    compose_deterministic_fallback,
    compose_with_client,
    fallback_arc_draft,
    parse_composer_response,
    source_ref_from_event,
    to_tts_review_script,
)

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

def write_manifest_three(path: Path, sources: list[Path]) -> None:
    path.write_text(
        json.dumps(
            {
                "series_id": "grand-blue-s03",
                "series_title": "Grand Blue Season 3",
                "season": 3,
                "episodes": [
                    {
                        "episode_key": f"s03e0{index}",
                        "episode_number": index,
                        "title": f"Episode {index}",
                        "source_path": str(source),
                        "arc": "summer",
                        "spoiler_limit_episode": index,
                    }
                    for index, source in enumerate(sources, start=1)
                ],
            }
        ),
        encoding="utf-8",
    )

def write_manifest_many(path: Path, sources: list[Path], arcs: list[str | None] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "series_id": "grand-blue-s03",
                "series_title": "Grand Blue Season 3",
                "season": 3,
                "episodes": [
                    {
                        "episode_key": f"s03e{index:02d}",
                        "episode_number": index,
                        "title": f"Episode {index}",
                        "source_path": str(source),
                        "arc": (arcs[index - 1] if arcs else None),
                        "spoiler_limit_episode": index,
                    }
                    for index, source in enumerate(sources, start=1)
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
    duration_s: float = 10.0,
    arc: str | None = "summer",
    extra_sections: list[tuple[str, str]] | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    section_defs = [(section_type, section_summary), *(extra_sections or [])]
    section_len = duration_s / len(section_defs)
    run_dir.joinpath("episode_meta.json").write_text(
        json.dumps(
            {
                "series_id": "grand-blue-s03",
                "episode_key": episode_key,
                "episode_number": int(episode_key[-2:]),
                "title": episode_key.upper(),
                "source_path": str(source_path),
                "arc": arc,
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
                    "arc": arc,
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
                    "id": index,
                    "type": "speech",
                    "tc_start": round(index * section_len, 3),
                    "tc_end": round((index + 1) * section_len, 3),
                    "ko": "dialogue",
                    "en": summary,
                    "scene_desc": None,
                }
                for index, (_kind, summary) in enumerate(section_defs)
            ]
        ),
        encoding="utf-8",
    )
    run_dir.joinpath("film_map.meta.json").write_text(
        json.dumps(
            {
                "input_path": str(source_path),
                "duration": duration_s,
                "created_at": CREATED_AT,
                "whisper_model": "large-v3",
                "translate_model": "gpt-4.1-mini",
                "vision_model": "gpt-4.1-mini",
                "gap_threshold": 4.0,
                "max_vision_frames": 0,
                "speech_count": len(section_defs),
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
                    "section_id": index,
                    "type": kind,
                    "tc_start": round(index * section_len, 3),
                    "tc_end": round((index + 1) * section_len, 3),
                    "segment_ids": [index],
                    "summary": summary,
                    "characters": ["Iori"],
                    "locations": [],
                    "events": [summary],
                    "confidence": 0.9,
                    "warnings": [],
                }
                for index, (kind, summary) in enumerate(section_defs)
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

def test_parse_composer_response_ignores_placeholder_event_ids(tmp_path: Path) -> None:
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

    beats = parse_composer_response(
        {
            "beats": [
                {
                    "event_ids": ["...", "s03e01:section:0"],
                    "narration": "Hook",
                    "is_hook": True,
                }
            ]
        },
        bank,
    )

    assert [ref.event_id for ref in beats[0].source_refs] == ["s03e01:section:0"]

def test_combine_arc_drafts_promotes_first_available_beat_to_hook(tmp_path: Path) -> None:
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
        section_summary="Episode one setup matters.",
    )
    write_episode_artifacts(
        tmp_path / "s03e02",
        episode_key="s03e02",
        source_path=source_two,
        recap_mode="quick",
        importance_score=0.5,
        section_type="reveal",
        section_summary="Episode two reveal matters.",
    )
    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={"s03e01": tmp_path / "s03e01", "s03e02": tmp_path / "s03e02"},
    )
    late_beat = SeriesReviewBeat(
        beat_id=12,
        narration="Tập hai mở tiếp mạch truyện sau khi arc đầu không trả về hook hợp lệ.",
        source_refs=[source_ref_from_event(bank.events[1])],
        is_hook=False,
    )

    combined = combine_arc_drafts([[], [late_beat]])

    assert [beat.beat_id for beat in combined] == [0]
    assert combined[0].is_hook
    assert combined[0].source_refs[0].event_id == "s03e02:section:0"

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
    long_hook = (
        "Mở đầu bằng cú rơi nhịp của Iori khi kế hoạch tưởng đã ổn bỗng lộ ra một lỗ hổng lớn. "
        "Cú đảo này làm cả nhóm mất thế chủ động và buộc câu chuyện chuyển sang một hướng khó lường hơn. "
    )
    long_follow = (
        "Từ đó cả nhóm phải vừa che giấu vừa ứng biến, khiến trò đùa nhỏ biến thành chuỗi hiểu lầm nối thẳng sang tập sau. "
        "Hệ quả mới kéo các nhân vật vào một vòng rối khác, nên đoạn này cần được giữ để mạch recap không hụt nguyên nhân. "
    )
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
            narration="Hook đủ dài để không bị quá ngắn.",
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

def test_episode_chaptered_event_bank_builds_per_episode_targets(tmp_path: Path) -> None:
    sources = [tmp_path / f"Grand_Blue.S03E0{index}.mp4" for index in range(1, 4)]
    manifest = tmp_path / "series_manifest.json"
    write_manifest_three(manifest, sources)
    extra_sections = [
        ("setup", "The club finds a new setup."),
        ("conflict", "The joke escalates into a conflict."),
        ("investigation", "The group tries to understand the mess."),
        ("reveal", "A reveal reframes the chaos."),
        ("ending", "The episode leaves a useful memory hook."),
    ]
    for index, source in enumerate(sources, start=1):
        write_episode_artifacts(
            tmp_path / f"s03e0{index}",
            episode_key=f"s03e0{index}",
            source_path=source,
            recap_mode="quick",
            importance_score=0.5,
            section_type="inciting_incident",
            section_summary="The episode starts a new comic problem.",
            duration_s=1440.0,
            extra_sections=extra_sections,
        )

    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={f"s03e0{index}": tmp_path / f"s03e0{index}" for index in range(1, 4)},
        tts_cps=15.0,
        mode_target_ratios={"quick": 0.14},
        recap_format="episode_chaptered",
    )

    assert bank.recap_format == "episode_chaptered"
    assert bank.target_video_s == pytest.approx(604.8)
    assert bank.char_budget == 9072
    assert [target.target_video_s for target in bank.episode_targets] == [201.6, 201.6, 201.6]
    assert [target.char_budget for target in bank.episode_targets] == [3024, 3024, 3024]
    assert all(target.target_beats >= 5 for target in bank.episode_targets)

def test_episode_chaptered_composer_revises_missing_episode_chapter(tmp_path: Path) -> None:
    sources = [tmp_path / f"Grand_Blue.S03E0{index}.mp4" for index in range(1, 4)]
    manifest = tmp_path / "series_manifest.json"
    write_manifest_three(manifest, sources)
    for index, source in enumerate(sources, start=1):
        write_episode_artifacts(
            tmp_path / f"s03e0{index}",
            episode_key=f"s03e0{index}",
            source_path=source,
            recap_mode="quick",
            importance_score=0.5,
            section_type="setup",
            section_summary=f"Episode {index} setup matters.",
            extra_sections=[("reveal", f"Episode {index} reveal pays off.")],
        )
    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={f"s03e0{index}": tmp_path / f"s03e0{index}" for index in range(1, 4)},
        tts_cps=10.0,
        mode_target_ratios={"quick": 0.14},
        recap_format="episode_chaptered",
    )
    client = SequenceChatClient(
        [
            {
                "beats": [
                    {
                        "event_ids": ["s03e03:section:1"],
                        "narration": "Hook chung đặt vấn đề lớn cho cả cụm tập.",
                        "is_hook": True,
                    },
                    {
                        "event_ids": ["s03e01:section:0"],
                        "narration": "Tập một thiết lập tình huống và để lại chi tiết cần nhớ.",
                        "is_hook": False,
                    },
                    {
                        "event_ids": ["s03e03:section:0"],
                        "narration": "Tập ba tiếp tục bằng hệ quả lớn hơn.",
                        "is_hook": False,
                    },
                ]
            },
            {
                "beats": [
                    {
                        "event_ids": ["s03e03:section:1"],
                        "narration": "Hook chung đặt vấn đề lớn cho cả cụm tập.",
                        "is_hook": True,
                    },
                    {
                        "event_ids": ["s03e01:section:0"],
                        "narration": "Tập một thiết lập tình huống và để lại chi tiết cần nhớ.",
                        "is_hook": False,
                    },
                    {
                        "event_ids": ["s03e02:section:0"],
                        "narration": "Tập hai nối tiếp nguyên nhân, biến chuyển đó thành một lời hứa mới.",
                        "is_hook": False,
                    },
                    {
                        "event_ids": ["s03e03:section:0"],
                        "narration": "Tập ba đưa tất cả hệ quả về cùng một điểm chốt cho phần sau.",
                        "is_hook": False,
                    },
                ]
            },
        ]
    )

    beats, meta = asyncio.run(compose_with_client(client, bank, qa_max_revisions=1))
    chapters = build_series_chapters(beats, bank)

    assert len(client.prompts) == 2
    assert "EPISODE_TARGET_PLAN" in client.prompts[0]
    assert "missing_episode_chapter" in client.prompts[1]
    assert meta.qa_report == []
    assert meta.model_versions["qa_revisions"] == "1"
    assert [chapter.episode_key for chapter in chapters] == [None, "s03e01", "s03e02", "s03e03"]

def write_detailed_episode_set(
    tmp_path: Path,
    *,
    count: int,
    arcs: list[str | None] | None = None,
    tts_cps: float = 24.0,
    recap_mode: str = "quick",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    sources = [tmp_path / f"Grand_Blue.S03E{index:02d}.mp4" for index in range(1, count + 1)]
    manifest = tmp_path / "series_manifest_many.json"
    write_manifest_many(manifest, sources, arcs)
    extra_sections = [
        ("setup", "The episode plants a small but important setup."),
        ("conflict", "The cast turns the setup into a visible conflict."),
        ("investigation", "The group tries to understand the new mess."),
        ("reveal", "A reveal changes how the audience reads the joke."),
        ("ending", "The ending leaves a useful continuity hook."),
    ]
    for index, source in enumerate(sources, start=1):
        write_episode_artifacts(
            tmp_path / f"s03e{index:02d}",
            episode_key=f"s03e{index:02d}",
            source_path=source,
            recap_mode=recap_mode,
            importance_score=0.5,
            section_type="inciting_incident",
            section_summary=f"Episode {index} starts a comic problem.",
            duration_s=1440.0,
            arc=arcs[index - 1] if arcs else None,
            extra_sections=extra_sections,
        )
    bank = build_event_bank(
        manifest_path=manifest,
        episode_run_dirs={f"s03e{index:02d}": tmp_path / f"s03e{index:02d}" for index in range(1, count + 1)},
        tts_cps=tts_cps,
        recap_format="episode_arc_chaptered",
        detail_level="detailed",
        arc_size=3,
    )
    return manifest, bank

def test_episode_arc_chaptered_event_bank_builds_detailed_12_episode_plan(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=12)
    plan = build_series_arc_plan(bank)

    assert bank.recap_format == "episode_arc_chaptered"
    assert plan.detail_level == "detailed"
    assert plan.arc_count == 4
    assert [arc.episode_keys for arc in plan.arcs] == [
        ["s03e01", "s03e02", "s03e03"],
        ["s03e04", "s03e05", "s03e06"],
        ["s03e07", "s03e08", "s03e09"],
        ["s03e10", "s03e11", "s03e12"],
    ]
    assert 2100 <= plan.total_target_video_s <= 2700
    assert plan.total_target_video_s <= 3000
    assert bank.target_video_s == plan.total_target_video_s
    assert all(target.target_video_s >= 90 for target in bank.episode_targets)
    assert all(target.target_beats > 0 for target in bank.episode_targets)

def test_detailed_full_season_scales_to_configured_maximum(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=12, recap_mode="full")

    assert bank.target_video_s == 2700.0
    assert bank.char_budget == 64_800
    assert sum(target.target_video_s for target in bank.episode_targets) == 2700.0
    assert "season target lowered to target_total_max_s" in bank.warnings

def test_season_scaling_fails_when_episode_minimums_exceed_cap(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=12)

    with pytest.raises(ValueError, match="episode minimum floor"):
        build_season_target_plan(
            recap_format="episode_arc_chaptered",
            episode_targets=bank.episode_targets,
            tts_cps=24.0,
            settings=SeasonTargetSettings(
                target_total_min_s=0.0,
                target_total_max_s=1000.0,
                target_total_hard_cap_s=1000.0,
                episode_min_s=90.0,
            ),
        )

def test_arc_prompt_includes_proportional_hard_character_limit(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=12, recap_mode="full")
    arc = bank.season_target_plan.arcs[0]

    prompt = build_arc_composer_prompt(bank=bank, arc=arc, arc_index=0)

    assert "HARD MAXIMUM" in prompt
    assert "18000 characters" in prompt

def test_episode_arc_chaptered_groups_manual_arcs_and_non_multiple_counts(tmp_path: Path) -> None:
    _manifest, manual_bank = write_detailed_episode_set(
        tmp_path / "manual",
        count=5,
        arcs=["arrival", "arrival", "club-test", "club-test", "aftermath"],
    )
    manual_plan = build_series_arc_plan(manual_bank)

    assert [arc.episode_keys for arc in manual_plan.arcs] == [
        ["s03e01", "s03e02"],
        ["s03e03", "s03e04"],
        ["s03e05"],
    ]

    _manifest, chunked_bank = write_detailed_episode_set(tmp_path / "chunked", count=10)
    chunked_plan = build_series_arc_plan(chunked_bank)

    assert [len(arc.episode_keys) for arc in chunked_plan.arcs] == [3, 3, 3, 1]

def test_later_arc_fallback_draft_does_not_require_hook(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=6, tts_cps=1.0)
    plan = build_series_arc_plan(bank)

    beats = fallback_arc_draft(bank, plan.arcs[1], arc_index=1)

    assert beats
    assert all(not beat.is_hook for beat in beats)
    assert [beat.beat_id for beat in beats] == list(range(len(beats)))

def test_deterministic_fallback_is_hooked_and_long_enough(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=12, tts_cps=1.0)

    beats, meta = compose_deterministic_fallback(bank, reason="test")

    assert beats[0].is_hook
    assert len(beats) >= 12
    assert sum(len(beat.narration) for beat in beats) >= bank.char_budget * 0.75
    assert meta.qa_report[0]["code"] == "deterministic_composer_fallback"

def test_composer_qa_rejects_repeated_template_and_sentences(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=4, tts_cps=1.0)
    repeated = (
        "Ở tập 8, chuyện cần giữ lại là: đây là một bước ngoặt của mạch truyện. "
        "Mốc này quan trọng vì nó làm thay đổi trạng thái câu chuyện. "
        "Mốc này quan trọng vì nó làm thay đổi trạng thái câu chuyện."
    )
    beats = [
        SeriesReviewBeat(
            beat_id=index,
            narration=repeated.replace("tập 8", f"tập {index + 1}"),
            source_refs=[source_ref_from_event(event)],
            is_hook=index == 0,
        )
        for index, event in enumerate(bank.events[:4])
    ]

    codes = {item["code"] for item in composer_qa_report(beats, bank)}

    assert "intra_beat_sentence_repetition" in codes
    assert "repeated_narration_template" in codes
    assert "generic_fallback_narration" in codes

def test_composer_qa_rejects_foreign_or_unaccented_narration(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=2, tts_cps=1.0)
    beats = [
        SeriesReviewBeat(
            beat_id=0,
            narration="Hook mở đầu since there seems to be a limit. 起きろ.",
            source_refs=[source_ref_from_event(bank.events[0])],
            is_hook=True,
        ),
        SeriesReviewBeat(
            beat_id=1,
            narration="Tap hai tiep tuc bang he qua moi",
            source_refs=[source_ref_from_event(bank.events[1])],
            is_hook=False,
        ),
    ]

    codes = {item["code"] for item in composer_qa_report(beats, bank)}

    assert "foreign_language_in_narration" in codes
    assert "unaccented_vietnamese_narration" in codes

def test_revision_maps_beat_level_qa_to_owning_arc(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=12)
    event = next(event for event in bank.events if event.episode_key == "s03e08")
    beats = [
        SeriesReviewBeat(
            beat_id=45,
            narration="Đây là một beat cần sửa vì còn cụm từ tiếng nước ngoài.",
            source_refs=[source_ref_from_event(event)],
            is_hook=False,
        )
    ]

    indexes = arc_indexes_for_revision(
        [{"level": "error", "code": "foreign_language_in_narration", "beat_ids": [45]}],
        bank,
        beats,
    )

    assert indexes == {2}

def long_narration(label: str) -> str:
    return (
        f"{label} được kể lại bằng nhân quả rõ ràng, giữ tên nhân vật và trạng thái câu chuyện ổn định. "
        f"{label} tiếp tục thêm chi tiết để người xem nhớ vì sao tập này quan trọng cho mạch sau. "
    )

def arc_response(
    episode_numbers: list[int],
    *,
    include_hook: bool = False,
    skip_episode: int | None = None,
    hook_episode: int | None = None,
) -> dict[str, object]:
    beats: list[dict[str, object]] = []
    if include_hook:
        hook_episode_number = hook_episode or max(episode_numbers)
        beats.append(
            {
                "event_ids": [f"s03e{hook_episode_number:02d}:section:1"],
                "narration": long_narration("Hook mùa phim"),
                "is_hook": True,
            }
        )
    for episode_number in episode_numbers:
        if episode_number == skip_episode:
            continue
        beats.append(
            {
                "event_ids": [f"s03e{episode_number:02d}:section:0"],
                "narration": long_narration(f"Tập {episode_number}"),
                "is_hook": False,
            }
        )
    return {"beats": beats}

def test_episode_arc_chaptered_composer_uses_arc_prompts_and_final_stitch(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=12, tts_cps=1.0)
    responses = [
        arc_response([1, 2, 3], include_hook=True, hook_episode=12),
        arc_response([4, 5, 6]),
        arc_response([7, 8, 9]),
        arc_response([10, 11, 12]),
        {
            "beats": [
                *arc_response([1, 2, 3], include_hook=True, hook_episode=12)["beats"],
                *arc_response([4, 5, 6])["beats"],
                *arc_response([7, 8, 9])["beats"],
                *arc_response([10, 11, 12])["beats"],
            ]
        },
    ]
    client = SequenceChatClient(responses)

    beats, meta = asyncio.run(compose_with_client(client, bank, qa_max_revisions=0))

    assert len(client.prompts) == 5
    assert sum("You are drafting one arc" in prompt for prompt in client.prompts) == 4
    assert "final stitch pass" in client.prompts[-1]
    assert beats[0].is_hook
    assert beats[0].source_refs[0].episode_key == "s03e12"
    assert [beat.source_refs[0].episode_key for beat in beats[1:]] == [f"s03e{index:02d}" for index in range(1, 13)]
    assert meta.model_versions["prompt_count"] == "5"
    assert meta.model_versions["arc_count"] == "4"

def test_episode_arc_chaptered_revision_fills_missing_episode_chapter(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=3, tts_cps=1.0)
    client = SequenceChatClient(
        [
            arc_response([1, 2, 3], include_hook=True, skip_episode=2),
            {
                "beats": arc_response([1, 2, 3], include_hook=True, skip_episode=2)["beats"],
            },
            arc_response([1, 2, 3], include_hook=True),
            {
                "beats": arc_response([1, 2, 3], include_hook=True)["beats"],
            },
        ]
    )

    beats, meta = asyncio.run(compose_with_client(client, bank, qa_max_revisions=1))

    assert len(client.prompts) == 4
    assert "missing_episode_chapter" in client.prompts[2]
    assert meta.qa_report == []
    assert meta.model_versions["qa_revisions"] == "1"
    assert [beat.source_refs[0].episode_key for beat in beats[1:]] == ["s03e01", "s03e02", "s03e03"]

def test_episode_arc_chaptered_invalid_revision_keeps_prior_valid_draft(tmp_path: Path) -> None:
    _manifest, bank = write_detailed_episode_set(tmp_path, count=3, tts_cps=20.0)
    client = SequenceChatClient(
        [
            arc_response([1, 2, 3], include_hook=True),
            {
                "beats": arc_response([1, 2, 3], include_hook=True)["beats"],
            },
            {"not_beats": []},
            {
                "beats": arc_response([1, 2, 3], include_hook=True)["beats"],
            },
        ]
    )

    beats, meta = asyncio.run(compose_with_client(client, bank, qa_max_revisions=1))

    assert beats[0].is_hook
    assert any(item["code"] == "invalid_revision_json" for item in meta.qa_report)
