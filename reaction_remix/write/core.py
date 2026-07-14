from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from common.integrity import stable_hash
from common.schema import (
    CommentaryFitRequests,
    CommentaryScript,
    CommentaryScriptSlot,
    CommentarySlotQa,
    ReactionBlocks,
    ReactionLlmInfo,
    ReactionTranscript,
    RemixPlan,
    validate_commentary_script,
)
from reaction_remix.write.japanese import STYLE_MARKERS, detected_tone_tags, validate_japanese_text
from reaction_remix.write.models import ScriptQaResponse, WrittenSlots
from reaction_remix.write.prompts import (
    build_fit_repair_prompt,
    build_script_qa_prompt,
    build_slot_repair_prompt,
    build_write_prompt,
)
from review.json_utils import extract_json

SCHEMA_VERSION = "reaction-remix.v1"


class ChatClient(Protocol):
    async def ask(self, prompt: str) -> str:
        ...


class CommentaryWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class WriteSettings:
    style_id: str = "reaction-internet-ja-v1"
    max_qa_iterations: int = 2
    min_distinct_style_markers: int = 2

    def validate(self) -> None:
        if self.max_qa_iterations < 0:
            raise CommentaryWriteError("max_qa_iterations must be non-negative")
        if self.min_distinct_style_markers < 0:
            raise CommentaryWriteError("min_distinct_style_markers must be non-negative")


def _block_context(block: Any, transcript: ReactionTranscript) -> dict[str, Any]:
    turns_by_id = {turn.turn_id: turn for turn in transcript.turns}
    return {
        "block_id": block.block_id,
        "kind": block.kind,
        "semantic": block.semantic.model_dump(mode="json") if block.semantic else None,
        "reaction_turns": [
            {
                "turn_id": turns_by_id[turn_id].turn_id,
                "text": turns_by_id[turn_id].text,
                "language": turns_by_id[turn_id].language,
                "speaker_id": turns_by_id[turn_id].speaker_id,
            }
            for turn_id in block.turn_ids
            if turn_id in turns_by_id
        ],
    }


def build_slot_contexts(plan: RemixPlan, blocks: ReactionBlocks, transcript: ReactionTranscript) -> list[dict[str, Any]]:
    by_id = {block.block_id: block for block in blocks.blocks}
    items = sorted(plan.items, key=lambda item: item.order)
    contexts: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if item.kind != "commentary_slot":
            continue
        previous_item = items[index - 1] if index > 0 else None
        next_item = items[index + 1] if index + 1 < len(items) else None
        previous_source = next((candidate for candidate in reversed(items[:index]) if candidate.kind == "source_block"), None)
        next_source = next((candidate for candidate in items[index + 1 :] if candidate.kind == "source_block"), None)
        contexts.append(
            {
                "slot_id": item.slot_id,
                "role": item.role,
                "target_duration_s": item.target_duration_s,
                "max_duration_s": item.max_duration_s,
                "char_budget": item.char_budget,
                "previous_item_id": previous_item.item_id if previous_item else None,
                "next_item_id": next_item.item_id if next_item else None,
                "previous_reaction": _block_context(by_id[previous_source.block_id], transcript) if previous_source else None,
                "next_reaction": _block_context(by_id[next_source.block_id], transcript) if next_source else None,
                "evidence": [_block_context(by_id[block_id], transcript) for block_id in item.evidence_block_ids],
            }
        )
    return contexts


def _local_slot_errors(text: str, *, role: str, char_budget: int) -> list[str]:
    errors = validate_japanese_text(text, char_budget=char_budget)
    if role == "punchline" and not detected_tone_tags(text):
        errors.append("punchline slot has no approved Japanese internet-commentary marker")
    return errors


def _required_style_markers(slots: list[CommentaryScriptSlot], configured_minimum: int) -> int:
    total_char_budget = sum(slot.char_budget for slot in slots)
    return min(configured_minimum, total_char_budget // 30)

def _global_style_marker_issues(
    contexts: list[dict[str, Any]],
    texts: dict[str, str],
    *,
    configured_minimum: int,
) -> list[dict[str, str]]:
    total_char_budget = sum(int(context["char_budget"] or 0) for context in contexts)
    required = min(configured_minimum, total_char_budget // 30)
    if required <= 0:
        return []
    used_markers = {
        marker
        for text in texts.values()
        for marker in detected_tone_tags(text)
    }
    missing = required - len(used_markers)
    if missing <= 0:
        return []
    remaining_markers = [marker for marker in STYLE_MARKERS if marker not in used_markers]
    if not remaining_markers:
        remaining_markers = list(STYLE_MARKERS)
    candidates = sorted(
        contexts,
        key=lambda context: (
            bool(detected_tone_tags(texts.get(context["slot_id"], ""))),
            -int(context["char_budget"] or 0),
            str(context["slot_id"]),
        ),
    )
    issues: list[dict[str, str]] = []
    for context, marker in zip(candidates, remaining_markers, strict=False):
        if len(issues) >= missing:
            break
        slot_id = context["slot_id"]
        issues.append(
            {
                "slot_id": slot_id,
                "issue_type": "deterministic",
                "suggestion": (
                    "Add one distinct approved Japanese internet-commentary marker "
                    f"not already used. Prefer '{marker}' if it fits naturally. "
                    "Keep the same evidence, transition purpose, and character budget."
                ),
            }
        )
    return issues


def _build_script(
    plan: RemixPlan,
    contexts: list[dict[str, Any]],
    texts: dict[str, str],
    *,
    settings: WriteSettings,
    session_url: str | None,
    attempts: int,
    plan_hash: str,
) -> CommentaryScript:
    plan_items = {item.slot_id: item for item in plan.items if item.kind == "commentary_slot"}
    expected = set(plan_items)
    if set(texts) != expected:
        missing = sorted(expected - set(texts))
        extra = sorted(set(texts) - expected)
        raise CommentaryWriteError(f"commentary slot mismatch; missing={missing}, extra={extra}")
    context_by_id = {context["slot_id"]: context for context in contexts}
    slots: list[CommentaryScriptSlot] = []
    all_tone_tags: set[str] = set()
    failures: list[str] = []
    for slot_id, item in plan_items.items():
        text = texts[slot_id].strip()
        errors = _local_slot_errors(text, role=item.role, char_budget=int(item.char_budget or 1))
        if errors:
            failures.extend(f"{slot_id}: {error}" for error in errors)
        tags = detected_tone_tags(text)
        all_tone_tags.update(tags)
        context = context_by_id[slot_id]
        slots.append(
            CommentaryScriptSlot(
                slot_id=slot_id,
                before_item_id=context["previous_item_id"],
                after_item_id=context["next_item_id"],
                role=item.role,
                text_ja=text,
                evidence_block_ids=item.evidence_block_ids,
                target_duration_s=item.target_duration_s,
                max_duration_s=item.max_duration_s,
                char_budget=item.char_budget,
                tone_tags=tags,
                qa=CommentarySlotQa(
                    language_ok=not any("Japanese" in error for error in errors),
                    evidence_ok=True,
                    style_ok=not any("marker" in error for error in errors),
                    length_ok=not any("budget" in error for error in errors),
                ),
                warnings=[],
            )
        )
    required_style_markers = _required_style_markers(slots, settings.min_distinct_style_markers)
    if len(all_tone_tags) < required_style_markers:
        failures.append(
            f"whole script uses {len(all_tone_tags)} distinct style markers; "
            f"minimum is {required_style_markers} for the available character budget"
        )
    if failures:
        raise CommentaryWriteError("; ".join(failures))
    script = CommentaryScript(
        schema_version=SCHEMA_VERSION,
        source_hash=plan.source_hash,
        plan_hash=plan_hash,
        language="ja",
        style_id=settings.style_id,
        slots=slots,
        llm=ReactionLlmInfo(backend="chatgpt_playwright", session_url=session_url, attempts=attempts),
        created_at=datetime.now(timezone.utc),
        warnings=[],
    )
    return validate_commentary_script(script, plan)


def _qa_payload(contexts: list[dict[str, Any]], texts: dict[str, str]) -> list[dict[str, Any]]:
    return [{**context, "text_ja": texts.get(context["slot_id"], "")} for context in contexts]


async def build_commentary_script(
    plan: RemixPlan,
    blocks: ReactionBlocks,
    transcript: ReactionTranscript,
    client: ChatClient,
    *,
    settings: WriteSettings | None = None,
    session_url: str | None = None,
    plan_hash: str | None = None,
) -> tuple[CommentaryScript, dict[str, Any]]:
    settings = settings or WriteSettings()
    settings.validate()
    if plan.source_hash != blocks.source_hash or plan.source_hash != transcript.source_hash:
        raise CommentaryWriteError("plan, blocks, and transcript source hashes do not match")
    contexts = build_slot_contexts(plan, blocks, transcript)
    plan_hash = plan_hash or stable_hash(plan.model_dump(mode="json"))
    response = await client.ask(build_write_prompt(slots=contexts))
    parsed = WrittenSlots.model_validate(extract_json(response))
    texts = {slot.slot_id: slot.text_ja for slot in parsed.slots}
    attempts = 1
    qa_history: list[dict[str, Any]] = []
    for iteration in range(settings.max_qa_iterations + 1):
        local_issues: list[dict[str, str]] = []
        for context in contexts:
            slot_id = context["slot_id"]
            if slot_id not in texts:
                local_issues.append({"slot_id": slot_id, "issue_type": "missing", "suggestion": "Return this slot."})
                continue
            for error in _local_slot_errors(texts[slot_id], role=context["role"], char_budget=context["char_budget"]):
                local_issues.append({"slot_id": slot_id, "issue_type": "deterministic", "suggestion": error})
        local_issues.extend(
            _global_style_marker_issues(
                contexts,
                texts,
                configured_minimum=settings.min_distinct_style_markers,
            )
        )
        qa = ScriptQaResponse.model_validate(extract_json(await client.ask(build_script_qa_prompt(slots=_qa_payload(contexts, texts)))))
        attempts += 1
        llm_issues = [issue.model_dump() for issue in qa.issues if issue.slot_id in {item["slot_id"] for item in contexts}]
        issues = local_issues + llm_issues
        if not qa.passed and not issues:
            raise CommentaryWriteError("LLM QA failed without actionable slot issues")
        qa_history.append({"iteration": iteration, "local_issues": local_issues, "llm": qa.model_dump(by_alias=True)})
        if not issues and qa.passed:
            script = _build_script(
                plan,
                contexts,
                texts,
                settings=settings,
                session_url=session_url,
                attempts=attempts,
                plan_hash=plan_hash,
            )
            return script, {"passed": True, "iterations": qa_history}
        if iteration >= settings.max_qa_iterations:
            break
        affected = {issue["slot_id"] for issue in issues}
        repair_contexts = [context for context in contexts if context["slot_id"] in affected]
        repaired = WrittenSlots.model_validate(
            extract_json(await client.ask(build_slot_repair_prompt(slots=_qa_payload(repair_contexts, texts), issues=issues)))
        )
        attempts += 1
        for slot in repaired.slots:
            if slot.slot_id not in affected:
                raise CommentaryWriteError(f"repair returned unrequested slot: {slot.slot_id}")
            texts[slot.slot_id] = slot.text_ja
    raise CommentaryWriteError("commentary script failed QA after bounded repairs")


async def repair_commentary_for_fit(
    script: CommentaryScript,
    fit_requests: CommentaryFitRequests,
    client: ChatClient,
    *,
    plan: RemixPlan,
    blocks: ReactionBlocks | None = None,
    transcript: ReactionTranscript | None = None,
    script_hash: str | None = None,
) -> tuple[CommentaryScript, dict[str, Any]]:
    script_hash = script_hash or stable_hash(script.model_dump(mode="json"))
    if fit_requests.source_hash != script.source_hash or fit_requests.script_hash != script_hash:
        raise CommentaryWriteError("fit request does not match the commentary script")
    by_id = {slot.slot_id: slot for slot in script.slots}
    evidence_contexts: dict[str, dict[str, Any]] = {}
    if blocks is not None and transcript is not None:
        evidence_contexts = {
            context["slot_id"]: context
            for context in build_slot_contexts(plan, blocks, transcript)
        }
    payload: list[dict[str, Any]] = []
    requested_ids: set[str] = set()
    for request in fit_requests.requests:
        if request.slot_id not in by_id:
            raise CommentaryWriteError(f"fit request references unknown slot: {request.slot_id}")
        if request.attempt > 2:
            raise CommentaryWriteError(f"fit repair limit exceeded for slot {request.slot_id}")
        requested_ids.add(request.slot_id)
        payload.append(
            {
                **request.model_dump(mode="json"),
                "text_ja": by_id[request.slot_id].text_ja,
                "evidence_context": evidence_contexts.get(request.slot_id),
            }
        )
    if not payload:
        return script
    repaired = WrittenSlots.model_validate(extract_json(await client.ask(build_fit_repair_prompt(slots=payload))))
    if {slot.slot_id for slot in repaired.slots} != requested_ids:
        raise CommentaryWriteError("fit repair must return exactly the requested slots")
    replacements = {slot.slot_id: slot.text_ja for slot in repaired.slots}
    requested_contexts = [evidence_contexts[slot_id] for slot_id in requested_ids if slot_id in evidence_contexts]
    if len(requested_contexts) != len(requested_ids):
        raise CommentaryWriteError("fit repair requires evidence context for every requested slot")
    qa_history: list[dict[str, Any]] = []
    llm_attempts = 1
    for iteration in range(2):
        issues: list[dict[str, str]] = []
        for context in requested_contexts:
            slot_id = context["slot_id"]
            for error in _local_slot_errors(
                replacements[slot_id],
                role=context["role"],
                char_budget=context["char_budget"],
            ):
                issues.append({"slot_id": slot_id, "issue_type": "deterministic", "suggestion": error})
        qa = ScriptQaResponse.model_validate(
            extract_json(
                await client.ask(
                    build_script_qa_prompt(slots=_qa_payload(requested_contexts, replacements))
                )
            )
        )
        llm_attempts += 1
        issues.extend(
            issue.model_dump()
            for issue in qa.issues
            if issue.slot_id in requested_ids
        )
        qa_history.append({"iteration": iteration, "issues": issues, "llm": qa.model_dump(by_alias=True)})
        if qa.passed and not issues:
            break
        if not qa.passed and not issues:
            raise CommentaryWriteError("fit-repair LLM QA failed without actionable slot issues")
        if iteration >= 1:
            raise CommentaryWriteError("fit-repaired commentary failed evidence/style QA")
        affected = {issue["slot_id"] for issue in issues}
        repair_contexts = [context for context in requested_contexts if context["slot_id"] in affected]
        second_pass = WrittenSlots.model_validate(
            extract_json(
                await client.ask(
                    build_slot_repair_prompt(
                        slots=_qa_payload(repair_contexts, replacements),
                        issues=issues,
                    )
                )
            )
        )
        llm_attempts += 1
        if {slot.slot_id for slot in second_pass.slots} != affected:
            raise CommentaryWriteError("fit QA repair must return exactly the affected slots")
        replacements.update({slot.slot_id: slot.text_ja for slot in second_pass.slots})
    updated_slots = []
    for slot in script.slots:
        if slot.slot_id not in replacements:
            updated_slots.append(slot)
            continue
        updated_slots.append(
            slot.model_copy(
                update={
                    "text_ja": replacements[slot.slot_id],
                    "tone_tags": detected_tone_tags(replacements[slot.slot_id]),
                }
            )
        )
    updated = script.model_copy(
        update={
            "slots": updated_slots,
            "llm": script.llm.model_copy(update={"attempts": script.llm.attempts + llm_attempts}),
            "created_at": datetime.now(timezone.utc),
        }
    )
    return validate_commentary_script(updated, plan), {"passed": True, "fit_repair_iterations": qa_history}
