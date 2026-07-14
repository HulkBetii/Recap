from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from common.integrity import stable_hash
from common.schema import CommentaryFitRequest, CommentaryFitRequests
from reaction_remix.plan.core import build_remix_plan
from reaction_remix.write.core import (
    _global_style_marker_issues,
    _required_style_markers,
    build_commentary_script,
    repair_commentary_for_fit,
)
from reaction_remix.write.japanese import STYLE_MARKERS
from tests.test_remix_plan import FakePlanClient, planner_payload, reaction_fixture


class FakeWriteClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.calls.append(prompt)
        if "Audit this Japanese" in prompt:
            return json.dumps({"pass": True, "issues": [], "notes": "ok"}, ensure_ascii=False)
        return json.dumps(
            {"slots": [{"slot_id": "commentary-slot-0001", "text_ja": "ニキの反応、さすがに草だぜ。"}]},
            ensure_ascii=False,
        )


def test_write_builds_evidence_bound_japanese_script() -> None:
    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(planner_payload())))

    script, qa = asyncio.run(
        build_commentary_script(plan, blocks, transcript, FakeWriteClient(), plan_hash="d" * 64)
    )

    assert qa["passed"] is True
    assert script.language == "ja"
    assert script.slots[0].evidence_block_ids == ["block-0001"]
    assert set(script.slots[0].tone_tags) >= {"ニキ", "草", "だぜ"}
    assert script.slots[0].before_item_id == "item-0000"
    assert script.slots[0].after_item_id == "item-0002"
    assert script.plan_hash == "d" * 64


def test_write_repairs_only_failed_slot() -> None:
    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(planner_payload())))

    class RepairClient:
        def __init__(self) -> None:
            self.write_calls = 0
            self.qa_calls = 0

        async def ask(self, prompt: str) -> str:
            if "Audit this Japanese" in prompt:
                self.qa_calls += 1
                if self.qa_calls == 1:
                    return json.dumps(
                        {
                            "pass": False,
                            "issues": [
                                {
                                    "slot_id": "commentary-slot-0001",
                                    "issue_type": "tone",
                                    "suggestion": "Use the channel register.",
                                }
                            ],
                            "notes": "repair",
                        }
                    )
                return json.dumps({"pass": True, "issues": [], "notes": "ok"})
            self.write_calls += 1
            text = "普通の説明です。" if self.write_calls == 1 else "ニキの反応、これは草だぜ。"
            return json.dumps({"slots": [{"slot_id": "commentary-slot-0001", "text_ja": text}]}, ensure_ascii=False)

    client = RepairClient()
    script, qa = asyncio.run(build_commentary_script(plan, blocks, transcript, client))

    assert script.slots[0].text_ja == "ニキの反応、これは草だぜ。"
    assert len(qa["iterations"]) == 2


def test_short_script_does_not_require_global_style_markers() -> None:
    source, transcript, blocks = reaction_fixture()
    payload = planner_payload()
    payload["commentary_slots"][0]["role"] = "setup"
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(payload)))

    class PlainShortClient:
        async def ask(self, prompt: str) -> str:
            if "Audit this Japanese" in prompt:
                return json.dumps({"pass": True, "issues": [], "notes": "ok"}, ensure_ascii=False)
            return json.dumps(
                {"slots": [{"slot_id": "commentary-slot-0001", "text_ja": "驚きの反応だ。"}]},
                ensure_ascii=False,
            )

    script, qa = asyncio.run(build_commentary_script(plan, blocks, transcript, PlainShortClient()))

    assert qa["passed"] is True
    assert script.slots[0].tone_tags == []


def test_long_script_keeps_configured_global_style_marker_gate() -> None:
    slots = [
        type("Slot", (), {"char_budget": 40})(),
        type("Slot", (), {"char_budget": 40})(),
    ]

    assert _required_style_markers(slots, 2) == 2


def test_global_style_marker_gap_becomes_actionable_slot_issue() -> None:
    contexts = [
        {"slot_id": "commentary-slot-0001", "char_budget": 40},
        {"slot_id": "commentary-slot-0002", "char_budget": 40},
    ]
    texts = {
        "commentary-slot-0001": f"ãƒãƒƒãƒ—å·®ãŒè¦‹ãˆã‚‹{STYLE_MARKERS[0]}ã€‚",
        "commentary-slot-0002": "æ—…ã®ä½™éŸ»ãŒæ®‹ã‚‹ã€‚",
    }

    issues = _global_style_marker_issues(contexts, texts, configured_minimum=2)

    assert [issue["slot_id"] for issue in issues] == ["commentary-slot-0002"]
    assert "distinct approved Japanese internet-commentary marker" in issues[0]["suggestion"]


def test_fit_rewrite_runs_separate_evidence_qa() -> None:
    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(planner_payload())))
    script, _qa = asyncio.run(build_commentary_script(plan, blocks, transcript, FakeWriteClient()))
    script_hash = stable_hash(script.model_dump(mode="json"))
    requests = CommentaryFitRequests(
        source_hash=script.source_hash,
        script_hash=script_hash,
        requests=[
            CommentaryFitRequest(
                slot_id="commentary-slot-0001",
                actual_duration_s=5.0,
                target_duration_s=2.0,
                max_duration_s=2.5,
                tolerance_s=0.1,
                direction="shorten",
                attempt=1,
                reason="too long",
            )
        ],
        created_at=datetime.now(timezone.utc),
    )
    client = FakeWriteClient()

    repaired, qa = asyncio.run(
        repair_commentary_for_fit(
            script,
            requests,
            client,
            plan=plan,
            blocks=blocks,
            transcript=transcript,
            script_hash=script_hash,
        )
    )

    assert qa["passed"] is True
    assert any("Audit this Japanese" in prompt for prompt in client.calls)
    assert repaired.llm.attempts == script.llm.attempts + 2
