from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from common.integrity import stable_hash
from common.schema import (
    ReactionAnalysisRegion,
    ReactionAsrInfo,
    ReactionAudioStream,
    ReactionBlock,
    ReactionBlocks,
    ReactionCutPoint,
    ReactionPreservation,
    ReactionSource,
    ReactionTranscript,
    ReactionTurn,
    ReactionVideoStream,
    RemixExcludedBlock,
    RemixSemanticAnnotation,
    validate_remix_plan,
)
from reaction_remix.orchestrator.runner import _duration_restore_block_ids
from reaction_remix.plan.core import PlanSettings, RemixPlanError, _slot_target_duration, build_remix_plan
from reaction_remix.plan.session import CHATGPT_HOME_URL, resolve_session, save_session


def reaction_fixture():  # type: ignore[no-untyped-def]
    now = datetime.now(timezone.utc)
    source_hash = "a" * 64
    source = ReactionSource(
        input_path="C:/source.mp4",
        input_hash=source_hash,
        duration_s=100.0,
        video=ReactionVideoStream(
            stream_index=0,
            codec="h264",
            width=1920,
            height=1080,
            fps_num=30000,
            fps_den=1001,
            pixel_format="yuv420p",
            frame_rate_mode="cfr",
        ),
        audio=ReactionAudioStream(
            stream_index=1,
            codec="aac",
            sample_rate=44100,
            channels=2,
            channel_layout="stereo",
        ),
        has_burned_in_subtitles=True,
        created_at=now,
        config_hash="b" * 64,
    )
    turns = [
        ReactionTurn(
            turn_id=0,
            tc_start=0.1,
            tc_end=39.9,
            text="Japan was so convenient.",
            language="en",
            language_confidence=0.99,
            speaker_id="reactor-1",
            speaker_confidence=0.99,
            asr_confidence=0.99,
            region_id="region-1",
        ),
        ReactionTurn(
            turn_id=1,
            tc_start=40.1,
            tc_end=49.9,
            text="ここで次の反応を見てみよう。",
            language="ja",
            language_confidence=0.99,
            speaker_id="narrator",
            speaker_confidence=0.99,
            asr_confidence=0.99,
            region_id="region-2",
        ),
        ReactionTurn(
            turn_id=2,
            tc_start=50.1,
            tc_end=89.9,
            text="Tipping felt strange after Japan.",
            language="en",
            language_confidence=0.99,
            speaker_id="reactor-2",
            speaker_confidence=0.99,
            asr_confidence=0.99,
            region_id="region-3",
        ),
    ]
    transcript = ReactionTranscript(
        source_hash=source_hash,
        source_duration_s=100.0,
        regions=[
            ReactionAnalysisRegion(region_id=f"region-{index}", tc_start=start, tc_end=end, status="ok", attempts=1)
            for index, (start, end) in enumerate(((0.0, 40.0), (40.0, 50.0), (50.0, 100.0)), start=1)
        ],
        turns=turns,
        asr=ReactionAsrInfo(device="cuda", chunk_s=30.0, overlap_s=2.0),
        created_at=now,
    )
    spans = [(0.0, 40.0), (40.0, 50.0), (50.0, 90.0), (90.0, 100.0)]
    cuts = []
    for index, tc in enumerate((0.0, 40.0, 50.0, 90.0, 100.0)):
        cuts.append(
            ReactionCutPoint(
                cut_point_id=f"cut-{index:04d}",
                tc=tc,
                kind="source_boundary" if index in {0, 4} else "scene_boundary",
                confidence=0.99,
                speech_padding_s=0.12,
            )
        )
    kinds = ["reaction", "commentary", "reaction", "branding"]
    blocks_list = []
    for index, ((start, end), kind) in enumerate(zip(spans, kinds, strict=True), start=1):
        blocks_list.append(
            ReactionBlock(
                block_id=f"block-{index:04d}",
                kind=kind,
                tc_start=start,
                tc_end=end,
                content_tc_start=start + 0.1,
                content_tc_end=end - 0.1,
                start_cut_point_id=f"cut-{index - 1:04d}",
                end_cut_point_id=f"cut-{index:04d}",
                turn_ids=[index - 1] if index <= 3 else [],
                language_codes=["ja" if kind == "commentary" else "en"],
                speaker_ids=["narrator" if kind == "commentary" else f"reactor-{index}"],
                sequence_group_id="reaction-sequence" if index in {1, 3} else None,
                sequence_index=0 if index == 1 else 1 if index == 3 else None,
                preservation=ReactionPreservation(
                    audio="replace_commentary" if kind == "commentary" else "source_mix"
                ),
                eligible_commentary_visual=kind == "commentary",
                classification_confidence=0.99,
                language_confidence=0.99,
                speaker_confidence=0.99,
                boundary_confidence=0.99,
            )
        )
    blocks = ReactionBlocks(
        source_hash=source_hash,
        transcript_hash=stable_hash(transcript.model_dump(mode="json")),
        source_duration_s=100.0,
        cut_points=cuts,
        blocks=blocks_list,
        created_at=now,
    )
    return source, transcript, blocks


def planner_payload(*, omit_second_reaction: bool = False) -> dict:
    ordered = [
        {"block_id": "block-0001", "role": "hook", "reason": "Strong hook."},
        {"block_id": "block-0004", "role": "branding", "reason": "Preserve branding."},
    ]
    exclusions = [
        {"block_id": "block-0002", "reason_code": "commentary", "reason": "Replace narrator."},
    ]
    if omit_second_reaction:
        exclusions.append(
            {"block_id": "block-0003", "reason_code": "duplicate_reaction", "reason": "Similar idea."}
        )
    else:
        ordered.insert(1, {"block_id": "block-0003", "role": "climax", "reason": "Strong contrast."})
    return {
        "semantic_annotations": [
            {
                "block_id": f"block-{index:04d}",
                "summary_ja": "反応" if index in {1, 3} else "つなぎ",
                "country": "US" if index in {1, 3} else None,
                "topic": "culture shock" if index in {1, 3} else None,
                "sentiment": "surprised",
                "intensity": 0.6,
                "novelty": 0.7,
            }
            for index in range(1, 5)
        ],
        "ordered_blocks": ordered,
        "commentary_slots": [
            {
                "slot_id": "commentary-slot-0001",
                "after_block_id": "block-0001",
                "role": "punchline",
                "evidence_block_ids": ["block-0001"],
                "preferred_visual_block_ids": ["block-0002"],
                "reason": "Bridge reactions.",
            }
        ],
        "exclusions": exclusions,
    }


class FakePlanClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return json.dumps(self.payload, ensure_ascii=False)


def test_plan_and_write_reuse_same_chatgpt_conversation_by_plan_hash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    session_path = tmp_path / "editorial_chat_session.json"
    save_session(
        session_path,
        policy="auto",
        chat_url="https://chatgpt.com/c/reaction-session",
        profile_dir=tmp_path / "PROFILE_GPT_1",
        source_hash="a" * 64,
        blocks_hash="b" * 64,
        content_hash="c" * 64,
        plan_hash="d" * 64,
        title="remix_plan",
        previous=None,
        warnings=[],
    )

    url, _previous, warnings = resolve_session(
        session_path,
        "auto",
        source_hash="a" * 64,
        blocks_hash="b" * 64,
        content_hash="e" * 64,
        plan_hash="d" * 64,
    )
    changed_url, _previous, changed_warnings = resolve_session(
        session_path,
        "auto",
        source_hash="a" * 64,
        blocks_hash="b" * 64,
        content_hash="f" * 64,
        plan_hash="9" * 64,
    )

    assert url == "https://chatgpt.com/c/reaction-session"
    assert warnings == []
    assert changed_url == CHATGPT_HOME_URL
    assert changed_warnings


def test_plan_uses_ids_and_safe_cuts() -> None:
    source, transcript, blocks = reaction_fixture()
    client = FakePlanClient(planner_payload())
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, client, blocks_hash="c" * 64))

    selected = [item for item in plan.items if item.kind == "source_block"]
    assert [item.block_id for item in selected] == ["block-0001", "block-0003", "block-0004"]
    assert selected[0].start_cut_point_id == "cut-0000"
    assert plan.items[1].char_budget == round(plan.items[1].target_duration_s * 6.5)
    assert plan.items[1].target_duration_s == 2.0
    assert plan.predicted_duration_s == pytest.approx(92.0)
    assert plan.retention.unique_reaction_speech_ratio == 1.0
    assert {item.block_id for item in plan.excluded_blocks} == {"block-0002"}
    assert len(plan.semantic_annotations) == 4
    assert plan.blocks_hash == "c" * 64
    assert '"tc_start"' not in client.prompts[0]
    assert 'Eligible commentary visual IDs are exactly: ["block-0002"]' in client.prompts[0]
    assert "Create exactly 1 commentary slots" in client.prompts[0]

def test_plan_manual_drop_can_exclude_hard_non_commentary_block() -> None:
    source, transcript, blocks = reaction_fixture()
    client = FakePlanClient(planner_payload())
    plan = asyncio.run(
        build_remix_plan(
            source,
            transcript,
            blocks,
            client,
            settings=PlanSettings(manual_drop_block_ids=("block-0004",)),
            blocks_hash="c" * 64,
        )
    )

    assert "block-0004" not in {item.block_id for item in plan.items if item.kind == "source_block"}
    dropped = {item.block_id: item for item in plan.excluded_blocks}["block-0004"]
    assert dropped.category == "manual_drop"
    assert plan.predicted_duration_s == pytest.approx(85.0)
    assert plan.retention.unique_reaction_speech_ratio == 1.0
    validate_remix_plan(plan, blocks)


def test_commentary_slot_budget_uses_its_single_visual_block_capacity() -> None:
    slot = SimpleNamespace(
        preferred_visual_block_ids=["block-a"],
        role="setup",
    )
    block_by_id = {
        "block-a": SimpleNamespace(tc_start=0.0, tc_end=4.0),
    }

    assert _slot_target_duration(slot, block_by_id) == 4.0


def test_plan_repairs_multi_core_commentary_slot_into_one_slot_per_core() -> None:
    source, transcript, blocks = reaction_fixture()
    second_commentary = blocks.blocks[-1].model_copy(
        update={
            "kind": "commentary",
            "language_codes": ["ja"],
            "speaker_ids": ["narrator"],
            "sequence_group_id": None,
            "sequence_index": None,
            "preservation": ReactionPreservation(audio="replace_commentary"),
            "eligible_commentary_visual": True,
        }
    )
    blocks = blocks.model_copy(update={"blocks": [*blocks.blocks[:-1], second_commentary]})

    class RepairClient:
        def __init__(self) -> None:
            self.calls = 0

        async def ask(self, _prompt: str) -> str:
            self.calls += 1
            payload = planner_payload()
            payload["ordered_blocks"] = [
                item for item in payload["ordered_blocks"] if item["block_id"] != "block-0004"
            ]
            payload["exclusions"].append(
                {"block_id": "block-0004", "reason_code": "commentary", "reason": "Replace narrator."}
            )
            if self.calls == 1:
                payload["commentary_slots"][0]["preferred_visual_block_ids"] = [
                    "block-0002",
                    "block-0004",
                ]
            else:
                payload["commentary_slots"].append(
                    {
                        "slot_id": "commentary-slot-0002",
                        "after_block_id": "block-0003",
                        "role": "close",
                        "evidence_block_ids": ["block-0003"],
                        "preferred_visual_block_ids": ["block-0004"],
                        "reason": "Close on the second narrator core.",
                    }
                )
            return json.dumps(payload, ensure_ascii=False)

    client = RepairClient()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, client))
    slots = [item for item in plan.items if item.kind == "commentary_slot"]

    assert client.calls == 2
    assert len(slots) == 2
    assert {item.preferred_visual_block_ids[0] for item in slots} == {"block-0002", "block-0004"}
    assert all(len(item.preferred_visual_block_ids) == 1 for item in slots)


def test_validate_remix_plan_rejects_excluded_branding() -> None:
    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(planner_payload())))
    branding = blocks.blocks[-1]
    kept = [item for item in plan.items if item.block_id != branding.block_id]
    kept = [
        item.model_copy(update={"item_id": f"item-{index:04d}", "order": index})
        for index, item in enumerate(kept)
    ]
    predicted = plan.predicted_duration_s - (branding.tc_end - branding.tc_start)
    broken = plan.model_copy(
        update={
            "items": kept,
            "excluded_blocks": [
                *plan.excluded_blocks,
                RemixExcludedBlock(
                    block_id=branding.block_id,
                    reason="Incorrectly excluded branding.",
                    category="branding",
                    source_duration_s=branding.tc_end - branding.tc_start,
                ),
            ],
            "predicted_duration_s": predicted,
            "predicted_output_ratio": predicted / plan.original_duration_s,
        }
    )

    with pytest.raises(ValueError, match="all non-commentary blocks must be retained unless explicitly manual_drop"):
        validate_remix_plan(broken, blocks)


def test_plan_restores_reaction_to_meet_retention() -> None:
    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(
        build_remix_plan(
            source,
            transcript,
            blocks,
            FakePlanClient(planner_payload(omit_second_reaction=True)),
            settings=PlanSettings(min_unique_reaction_speech_ratio=0.90),
        )
    )

    assert "block-0003" in {item.block_id for item in plan.items if item.kind == "source_block"}
    assert plan.retention.unique_reaction_speech_ratio == 1.0


def test_plan_retention_uses_actual_turn_spans_instead_of_block_content_spans() -> None:
    source, transcript, _blocks = reaction_fixture()
    transcript = transcript.model_copy(
        update={
            "turns": [
                transcript.turns[0].model_copy(update={"tc_start": 0.1, "tc_end": 1.1}),
                transcript.turns[2].model_copy(
                    update={"turn_id": 1, "tc_start": 90.1, "tc_end": 99.1}
                ),
            ]
        }
    )
    cuts = [
        ReactionCutPoint(
            cut_point_id=f"cut-{index:04d}",
            tc=tc,
            kind="source_boundary" if index in {0, 2} else "turn_boundary",
            confidence=0.99,
            speech_padding_s=0.12,
        )
        for index, tc in enumerate((0.0, 90.0, 100.0))
    ]
    blocks = ReactionBlocks(
        source_hash=source.input_hash,
        transcript_hash=stable_hash(transcript.model_dump(mode="json")),
        source_duration_s=100.0,
        cut_points=cuts,
        blocks=[
            ReactionBlock(
                block_id="block-0001",
                kind="reaction",
                tc_start=0.0,
                tc_end=90.0,
                content_tc_start=0.1,
                content_tc_end=89.9,
                start_cut_point_id="cut-0000",
                end_cut_point_id="cut-0001",
                turn_ids=[0],
                language_codes=["en"],
                speaker_ids=["reactor-1"],
                preservation=ReactionPreservation(audio="source_mix"),
                eligible_commentary_visual=False,
                classification_confidence=0.99,
                language_confidence=0.99,
                speaker_confidence=0.99,
                boundary_confidence=0.99,
            ),
            ReactionBlock(
                block_id="block-0002",
                kind="reaction",
                tc_start=90.0,
                tc_end=100.0,
                content_tc_start=90.1,
                content_tc_end=99.9,
                start_cut_point_id="cut-0001",
                end_cut_point_id="cut-0002",
                turn_ids=[1],
                language_codes=["en"],
                speaker_ids=["reactor-2"],
                preservation=ReactionPreservation(audio="source_mix"),
                eligible_commentary_visual=False,
                classification_confidence=0.99,
                language_confidence=0.99,
                speaker_confidence=0.99,
                boundary_confidence=0.99,
            ),
        ],
        created_at=datetime.now(timezone.utc),
    )
    payload = {
        "semantic_annotations": [
            {"block_id": "block-0001", "summary_ja": "short speech", "novelty": 0.9},
            {"block_id": "block-0002", "summary_ja": "dense speech", "novelty": 0.8},
        ],
        "ordered_blocks": [
            {"block_id": "block-0001", "role": "hook", "reason": "Long visual span."}
        ],
        "commentary_slots": [],
        "exclusions": [
            {"block_id": "block-0002", "reason_code": "duplicate_reaction", "reason": "Omit."}
        ],
    }

    plan = asyncio.run(
        build_remix_plan(
            source,
            transcript,
            blocks,
            FakePlanClient(payload),
            settings=PlanSettings(min_unique_reaction_speech_ratio=0.90),
        )
    )

    assert {item.block_id for item in plan.items if item.kind == "source_block"} == {
        "block-0001",
        "block-0002",
    }
    assert plan.retention.unique_reaction_speech_ratio == 1.0


def test_plan_repairs_invented_id_once() -> None:
    source, transcript, blocks = reaction_fixture()

    class RepairClient:
        def __init__(self) -> None:
            self.calls = 0

        async def ask(self, _prompt: str) -> str:
            self.calls += 1
            payload = planner_payload()
            if self.calls == 1:
                payload["ordered_blocks"][0]["block_id"] = "block-9999"
            return json.dumps(payload, ensure_ascii=False)

    client = RepairClient()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, client))

    assert plan.items
    assert client.calls == 2


def test_plan_repair_restates_non_empty_unique_commentary_visual_constraints() -> None:
    source, transcript, blocks = reaction_fixture()

    class RepairClient:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        async def ask(self, prompt: str) -> str:
            self.calls += 1
            self.prompts.append(prompt)
            payload = planner_payload()
            if self.calls == 1:
                payload["commentary_slots"].append(
                    {
                        "slot_id": "commentary-slot-0002",
                        "after_block_id": "block-0003",
                        "role": "close",
                        "evidence_block_ids": ["block-0003"],
                        "preferred_visual_block_ids": [],
                        "reason": "No visual capacity.",
                    }
                )
            return json.dumps(payload, ensure_ascii=False)

    client = RepairClient()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, client))

    assert plan.items
    assert client.calls == 2
    assert 'Eligible commentary visual IDs are exactly: ["block-0002"]' in client.prompts[1]
    assert "never return an empty preferred_visual_block_ids list" in client.prompts[1]


def test_plan_repairs_missing_commentary_slot_once() -> None:
    source, transcript, blocks = reaction_fixture()

    class RepairClient:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        async def ask(self, prompt: str) -> str:
            self.calls += 1
            self.prompts.append(prompt)
            payload = planner_payload()
            if self.calls == 1:
                payload["commentary_slots"] = []
            return json.dumps(payload, ensure_ascii=False)

    client = RepairClient()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, client))

    assert len([item for item in plan.items if item.kind == "commentary_slot"]) == 1
    assert client.calls == 2
    assert "every eligible commentary block must be assigned" in client.prompts[1]


def test_plan_full_duration_replacement_does_not_round_above_hard_max() -> None:
    source, transcript, blocks = reaction_fixture()
    boundary = 48.47396875
    commentary_end_cut = blocks.cut_points[2].model_copy(update={"tc": boundary})
    commentary_block = blocks.blocks[1].model_copy(
        update={"tc_end": boundary, "content_tc_end": boundary - 0.1}
    )
    reaction_block = blocks.blocks[2].model_copy(
        update={"tc_start": boundary, "content_tc_start": boundary + 0.1}
    )
    blocks = blocks.model_copy(
        update={
            "cut_points": [*blocks.cut_points[:2], commentary_end_cut, *blocks.cut_points[3:]],
            "blocks": [blocks.blocks[0], commentary_block, reaction_block, blocks.blocks[3]],
        }
    )
    payload = planner_payload()
    payload["commentary_slots"][0]["role"] = "setup"

    plan = asyncio.run(
        build_remix_plan(
            source,
            transcript,
            blocks,
            FakePlanClient(payload),
            settings=PlanSettings(output_ratio=1.0, preferred_max_output_ratio=1.0),
        )
    )

    assert plan.predicted_duration_s == pytest.approx(100.0)
    assert plan.predicted_output_ratio <= 1.0
    slot = next(item for item in plan.items if item.kind == "commentary_slot")
    assert slot.target_duration_s == pytest.approx(8.47396875)
    assert slot.max_duration_s == pytest.approx(8.47396875)


def test_duration_repair_restores_affected_reaction_in_sequence() -> None:
    from common.schema import RemixExcludedBlock, RemixRepairRequests
    from reaction_remix.plan.__main__ import _apply_duration_repair_idempotently
    from reaction_remix.plan.core import apply_duration_restore

    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(planner_payload())))
    kept = [item for item in plan.items if item.block_id != "block-0003"]
    kept = [item.model_copy(update={"item_id": f"item-{index:04d}", "order": index}) for index, item in enumerate(kept)]
    broken = plan.model_copy(
        update={
            "items": kept,
            "excluded_blocks": [
                *plan.excluded_blocks,
                RemixExcludedBlock(
                    block_id="block-0003",
                    reason="Temporarily omitted.",
                    category="duplicate_reaction",
                    source_duration_s=40.0,
                    unique_reaction_speech_s=39.8,
                ),
            ],
            "predicted_duration_s": 55.0,
            "predicted_output_ratio": 0.55,
        }
    )
    repairs = RemixRepairRequests.model_validate(
        {
            "source_hash": plan.source_hash,
            "items": [
                {
                    "repair_id": "repair-0001",
                    "kind": "duration_restore",
                    "affected_ids": ["block-0003"],
                    "reason": "Below hard floor.",
                    "attempt": 1,
                    "requested_stage": "plan",
                }
            ],
            "created_at": datetime.now(timezone.utc),
        }
    )

    repaired = apply_duration_restore(
        broken,
        blocks,
        repairs,
        transcript=transcript,
        blocks_hash="d" * 64,
    )

    selected = [item.block_id for item in repaired.items if item.kind == "source_block"]
    assert selected == ["block-0001", "block-0003", "block-0004"]
    assert repaired.blocks_hash == "d" * 64
    assert repaired.predicted_output_ratio == pytest.approx(0.92)

    replayed = _apply_duration_repair_idempotently(
        repaired,
        blocks,
        transcript,
        repairs,
        blocks_hash="d" * 64,
    )
    assert replayed.model_dump(mode="json") == repaired.model_dump(mode="json")


def test_duration_repair_selects_only_best_blocks_needed_without_exceeding_hard_max() -> None:
    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(planner_payload())))
    plan = plan.model_copy(
        update={
            "predicted_duration_s": 80.0,
            "predicted_output_ratio": 0.80,
            "excluded_blocks": [
                RemixExcludedBlock(
                    block_id="block-oversized",
                    reason="High novelty but too long.",
                    category="duplicate_reaction",
                    source_duration_s=25.0,
                    unique_reaction_speech_s=20.0,
                ),
                RemixExcludedBlock(
                    block_id="block-best",
                    reason="Best valid restore.",
                    category="duplicate_reaction",
                    source_duration_s=5.0,
                    unique_reaction_speech_s=4.0,
                ),
                RemixExcludedBlock(
                    block_id="block-extra",
                    reason="Not needed after floor is met.",
                    category="duplicate_reaction",
                    source_duration_s=5.0,
                    unique_reaction_speech_s=4.0,
                ),
            ],
            "semantic_annotations": [
                RemixSemanticAnnotation(block_id="block-oversized", novelty=1.0, intensity=1.0),
                RemixSemanticAnnotation(block_id="block-best", novelty=0.9, intensity=0.9),
                RemixSemanticAnnotation(block_id="block-extra", novelty=0.8, intensity=0.8),
            ],
        }
    )

    selected = _duration_restore_block_ids(plan, rendered_duration_s=79.0)

    assert selected == ["block-best"]


@pytest.mark.parametrize("kind", ["reaction", "mixed", "unknown", "branding", "transition", "broll"])
def test_plan_restores_every_non_commentary_kind(kind: str) -> None:
    source, transcript, blocks = reaction_fixture()
    protected = blocks.blocks[-1].model_copy(
        update={
            "kind": kind,
            "semantic": None,
            "eligible_commentary_visual": False,
        }
    )
    blocks = blocks.model_copy(update={"blocks": [*blocks.blocks[:-1], protected]})
    payload = planner_payload()
    payload["ordered_blocks"] = [
        item for item in payload["ordered_blocks"] if item["block_id"] != "block-0004"
    ]
    payload["exclusions"].append(
        {"block_id": "block-0004", "reason_code": kind, "reason": "Planner attempted to omit it."}
    )

    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(payload)))

    assert "block-0004" in {item.block_id for item in plan.items if item.kind == "source_block"}
    assert {item.block_id for item in plan.excluded_blocks} == {"block-0002"}


def test_plan_rejects_non_commentary_visual_even_when_marked_eligible() -> None:
    source, transcript, blocks = reaction_fixture()
    broll = blocks.blocks[-1].model_copy(
        update={
            "kind": "broll",
            "eligible_commentary_visual": True,
        }
    )
    blocks = blocks.model_copy(update={"blocks": [*blocks.blocks[:-1], broll]})
    payload = planner_payload()
    payload["commentary_slots"][0]["preferred_visual_block_ids"] = ["block-0004"]

    with pytest.raises(RemixPlanError, match="eligible commentary block"):
        asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(payload)))
