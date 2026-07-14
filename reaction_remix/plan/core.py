from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import ValidationError

from common.integrity import stable_hash
from common.schema import (
    ReactionBlocks,
    ReactionLlmInfo,
    ReactionSource,
    ReactionTranscript,
    RemixDurationPolicy,
    RemixExcludedBlock,
    RemixPlan,
    RemixPlanItem,
    RemixRetention,
    RemixRepairRequests,
    RemixSemanticAnnotation,
    validate_remix_plan,
)
from reaction_remix.plan.models import PlannerBlockChoice, PlannerDraft, PlannerExclusion
from reaction_remix.plan.prompts import build_plan_prompt, build_plan_repair_prompt
from review.json_utils import extract_json

SCHEMA_VERSION = "reaction-remix.v1"
PLAN_PROMPT_VERSION = "reaction-plan-v8"
COMMENTARY_CHARS_PER_SECOND = 6.5
MIN_COMMENTARY_SLOT_S = 2.0
ROLE_TARGET_S = {"setup": 10.0, "bridge": 7.0, "punchline": 5.0, "close": 8.0}
SOURCE_ITEM_KINDS = {"reaction", "mixed", "unknown", "branding", "transition", "broll"}
PRESERVE_KINDS = SOURCE_ITEM_KINDS


class ChatClient(Protocol):
    async def ask(self, prompt: str) -> str:
        ...


class RemixPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanSettings:
    output_ratio: float = 0.875
    hard_min_output_ratio: float = 0.80
    preferred_min_output_ratio: float = 0.85
    preferred_max_output_ratio: float = 0.90
    hard_max_output_ratio: float = 1.00
    min_unique_reaction_speech_ratio: float = 0.90
    max_semantic_repairs: int = 1
    manual_drop_block_ids: tuple[str, ...] = ()

    def validate(self) -> None:
        ratios = (
            self.hard_min_output_ratio,
            self.preferred_min_output_ratio,
            self.output_ratio,
            self.preferred_max_output_ratio,
            self.hard_max_output_ratio,
        )
        if tuple(sorted(ratios)) != ratios or ratios[0] < 0.80 or ratios[-1] > 1.0:
            raise RemixPlanError("duration ratios must be ordered inside 0.80-1.00")
        if self.min_unique_reaction_speech_ratio < 0.90:
            raise RemixPlanError("reaction speech retention must be at least 0.90")
        if self.max_semantic_repairs < 0:
            raise RemixPlanError("max_semantic_repairs must be non-negative")
        if len(set(self.manual_drop_block_ids)) != len(self.manual_drop_block_ids):
            raise RemixPlanError("manual_drop_block_ids must not contain duplicates")


def _duration(block: Any) -> float:
    return float(block.tc_end - block.tc_start)


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _interval_duration(intervals: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _reaction_speech_intervals(block: Any, turns_by_id: dict[int, Any]) -> list[tuple[float, float]]:
    if block.kind != "reaction":
        return []
    intervals: list[tuple[float, float]] = []
    for turn_id in block.turn_ids:
        turn = turns_by_id.get(turn_id)
        if turn is None:
            continue
        source_intervals = (
            [(word.tc_start, word.tc_end) for word in turn.words]
            if turn.words
            else [(turn.tc_start, turn.tc_end)]
        )
        for start, end in source_intervals:
            clipped_start = max(float(start), float(block.content_tc_start))
            clipped_end = min(float(end), float(block.content_tc_end))
            if clipped_end > clipped_start:
                intervals.append((clipped_start, clipped_end))
    return _merge_intervals(intervals)


def _reaction_speech_stats(
    blocks: ReactionBlocks,
    transcript: ReactionTranscript,
    selected_block_ids: set[str],
) -> tuple[float, float, dict[str, float]]:
    turns_by_id = {turn.turn_id: turn for turn in transcript.turns}
    all_intervals: list[tuple[float, float]] = []
    selected_intervals: list[tuple[float, float]] = []
    by_block: dict[str, float] = {}
    for block in blocks.blocks:
        intervals = _reaction_speech_intervals(block, turns_by_id)
        by_block[block.block_id] = _interval_duration(intervals)
        all_intervals.extend(intervals)
        if block.block_id in selected_block_ids:
            selected_intervals.extend(intervals)
    return _interval_duration(all_intervals), _interval_duration(selected_intervals), by_block


def _semantic_value(block: Any, name: str) -> Any:
    semantic = getattr(block, "semantic", None)
    return getattr(semantic, name, None) if semantic is not None else None


def build_block_catalog(blocks: ReactionBlocks, transcript: ReactionTranscript) -> list[dict[str, Any]]:
    turns_by_id = {turn.turn_id: turn for turn in transcript.turns}
    return [
        {
            "block_id": block.block_id,
            "kind": block.kind,
            "duration_s": round(_duration(block), 3),
            "content_duration_s": round(float(block.content_tc_end - block.content_tc_start), 3),
            "languages": block.language_codes,
            "sequence_group_id": block.sequence_group_id,
            "sequence_index": block.sequence_index,
            "eligible_commentary_visual": block.eligible_commentary_visual,
            "summary_ja": _semantic_value(block, "summary_ja"),
            "country": _semantic_value(block, "country"),
            "topic": _semantic_value(block, "topic"),
            "sentiment": _semantic_value(block, "sentiment"),
            "intensity": _semantic_value(block, "intensity"),
            "novelty": _semantic_value(block, "novelty"),
            "turns": [
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
        for block in blocks.blocks
    ]


def _restore_required_blocks(
    draft: PlannerDraft,
    blocks: ReactionBlocks,
    transcript: ReactionTranscript,
    settings: PlanSettings,
) -> PlannerDraft:
    by_id = {block.block_id: block for block in blocks.blocks}
    annotations = {item.block_id: item for item in draft.semantic_annotations}
    selected = {item.block_id for item in draft.ordered_blocks}
    restored: list[Any] = []
    for block in blocks.blocks:
        if block.kind in PRESERVE_KINDS and block.block_id not in selected:
            restored.append(block)
            selected.add(block.block_id)

    total_speech, selected_speech, _speech_by_block = _reaction_speech_stats(blocks, transcript, selected)
    omitted_reactions = [block for block in blocks.blocks if block.kind == "reaction" and block.block_id not in selected]
    omitted_reactions.sort(
        key=lambda block: (
            -float(getattr(annotations.get(block.block_id), "novelty", None) or 0.0),
            -float(getattr(annotations.get(block.block_id), "intensity", None) or 0.0),
            block.tc_start,
        )
    )
    while omitted_reactions and total_speech > 0 and selected_speech / total_speech < settings.min_unique_reaction_speech_ratio:
        block = omitted_reactions.pop(0)
        restored.append(block)
        selected.add(block.block_id)
        _total_speech, selected_speech, _speech_by_block = _reaction_speech_stats(blocks, transcript, selected)

    current_duration = sum(_duration(by_id[item.block_id]) for item in draft.ordered_blocks if item.block_id in by_id)
    current_duration += sum(_duration(block) for block in restored)
    hard_min_s = blocks.source_duration_s * settings.hard_min_output_ratio
    while omitted_reactions and current_duration < hard_min_s:
        block = omitted_reactions.pop(0)
        restored.append(block)
        selected.add(block.block_id)
        current_duration += _duration(block)

    if not restored:
        return draft
    ordered = list(draft.ordered_blocks)
    for block in sorted(restored, key=lambda item: item.tc_start):
        insertion = next((index for index, item in enumerate(ordered) if item.role == "close"), len(ordered))
        if block.sequence_group_id is not None and block.sequence_index is not None:
            group_positions = [
                (index, by_id[item.block_id])
                for index, item in enumerate(ordered)
                if item.block_id in by_id and by_id[item.block_id].sequence_group_id == block.sequence_group_id
            ]
            higher = [index for index, member in group_positions if (member.sequence_index or 0) > block.sequence_index]
            lower = [index for index, member in group_positions if (member.sequence_index or 0) < block.sequence_index]
            if higher:
                insertion = min(higher)
            elif lower:
                insertion = max(lower) + 1
        ordered.insert(
            insertion,
            PlannerBlockChoice(block_id=block.block_id, role="body", reason="Restored by retention/duration policy."),
        )
    exclusions = [item for item in draft.exclusions if item.block_id not in selected]
    return draft.model_copy(update={"ordered_blocks": ordered, "exclusions": exclusions})

def _manual_drop_reason(block_id: str) -> str:
    return (
        f"Manually dropped {block_id} after review because original commentary remains "
        "or the segment is too hard to preserve cleanly."
    )

def _apply_manual_drops_to_draft(
    draft: PlannerDraft,
    blocks: ReactionBlocks,
    settings: PlanSettings,
) -> PlannerDraft:
    manual_ids = set(settings.manual_drop_block_ids)
    if not manual_ids:
        return draft
    block_by_id = {block.block_id: block for block in blocks.blocks}
    unknown = sorted(manual_ids - set(block_by_id))
    if unknown:
        raise RemixPlanError(f"manual_drop_block_ids contain unknown block IDs: {unknown}")
    commentary = sorted(block_id for block_id in manual_ids if block_by_id[block_id].kind == "commentary")
    if commentary:
        raise RemixPlanError(f"manual_drop_block_ids cannot target commentary blocks: {commentary}")

    ordered: list[PlannerBlockChoice] = [
        choice for choice in draft.ordered_blocks if choice.block_id not in manual_ids
    ]
    if not ordered:
        raise RemixPlanError("manual_drop_block_ids removed every ordered source block")
    remaining_ids = {choice.block_id for choice in ordered}
    for slot in draft.commentary_slots:
        if slot.after_block_id in manual_ids:
            raise RemixPlanError(
                f"manual_drop_block_ids cannot remove after_block_id for commentary slot {slot.slot_id}"
            )
        if any(block_id in manual_ids for block_id in slot.evidence_block_ids):
            raise RemixPlanError(
                f"manual_drop_block_ids cannot remove evidence for commentary slot {slot.slot_id}"
            )
        if any(block_id in manual_ids for block_id in slot.preferred_visual_block_ids):
            raise RemixPlanError(
                f"manual_drop_block_ids cannot remove visual block for commentary slot {slot.slot_id}"
            )
        if slot.after_block_id is not None and slot.after_block_id not in remaining_ids:
            raise RemixPlanError(f"commentary slot {slot.slot_id} is anchored after a removed block")

    exclusions = [item for item in draft.exclusions if item.block_id not in manual_ids]
    exclusions.extend(
        PlannerExclusion(
            block_id=block_id,
            reason_code="manual_drop",
            reason=_manual_drop_reason(block_id),
        )
        for block_id in sorted(manual_ids)
    )
    return draft.model_copy(update={"ordered_blocks": ordered, "exclusions": exclusions})


def _slot_target_duration(slot: Any, block_by_id: dict[str, Any]) -> float:
    capacity = max((_duration(block_by_id[block_id]) for block_id in slot.preferred_visual_block_ids), default=0.0)
    return min(capacity, ROLE_TARGET_S.get(slot.role, 7.0))


def _allocate_slot_targets(
    slots: list[Any],
    block_by_id: dict[str, Any],
    *,
    commentary_budget_s: float,
) -> dict[str, float]:
    maximums = {slot.slot_id: _slot_target_duration(slot, block_by_id) for slot in slots}
    minimums = {
        slot.slot_id: min(maximums[slot.slot_id], MIN_COMMENTARY_SLOT_S)
        for slot in slots
    }
    minimum_total = sum(minimums.values())
    maximum_total = sum(maximums.values())
    target_total = min(maximum_total, max(minimum_total, commentary_budget_s))
    remaining = max(0.0, target_total - minimum_total)
    targets = dict(minimums)
    for slot in slots:
        if remaining <= 1e-9:
            break
        slot_id = slot.slot_id
        increment = min(maximums[slot_id] - targets[slot_id], remaining)
        targets[slot_id] += increment
        remaining -= increment
    return targets


def _build_plan(
    source: ReactionSource,
    transcript: ReactionTranscript,
    blocks: ReactionBlocks,
    draft: PlannerDraft,
    settings: PlanSettings,
    *,
    session_url: str | None,
    attempts: int,
    blocks_hash: str,
) -> RemixPlan:
    by_id = {block.block_id: block for block in blocks.blocks}
    selected_ids = [choice.block_id for choice in draft.ordered_blocks]
    selected_set = set(selected_ids)
    manual_drop_ids = set(settings.manual_drop_block_ids)
    errors: list[str] = []
    for block_id in selected_ids:
        block = by_id.get(block_id)
        if block is None:
            errors.append(f"unknown selected block: {block_id}")
        elif block.kind not in SOURCE_ITEM_KINDS:
            errors.append(f"block {block_id} kind={block.kind} cannot be a source-audio plan item")
    for block in blocks.blocks:
        if block.kind in PRESERVE_KINDS and block.block_id not in selected_set:
            if block.block_id not in manual_drop_ids:
                errors.append(f"preserve-only block omitted: {block.block_id}")
    selected_sequence: dict[str, list[int]] = {}
    for block_id in selected_ids:
        block = by_id.get(block_id)
        if block is not None and block.sequence_group_id is not None and block.sequence_index is not None:
            selected_sequence.setdefault(block.sequence_group_id, []).append(block.sequence_index)
    for group_id, indexes in selected_sequence.items():
        if indexes != sorted(indexes):
            errors.append(f"sequence group {group_id} is reordered internally: {indexes}")

    exclusion_by_id = {item.block_id: item for item in draft.exclusions}
    unknown_exclusion_ids = sorted(set(exclusion_by_id) - set(by_id))
    if unknown_exclusion_ids:
        errors.append(f"exclusions reference unknown blocks: {unknown_exclusion_ids}")
    selected_exclusion_ids = sorted(selected_set & set(exclusion_by_id))
    if selected_exclusion_ids:
        errors.append(f"selected blocks cannot also be excluded: {selected_exclusion_ids}")
    slot_ids = {slot.slot_id for slot in draft.commentary_slots}
    if len(slot_ids) != len(draft.commentary_slots):
        errors.append("commentary slot IDs must be unique")
    for block in blocks.blocks:
        if block.kind == "reaction" and block.block_id not in selected_set and block.block_id not in exclusion_by_id:
            errors.append(f"omitted reaction lacks exclusion reason: {block.block_id}")
    annotation_by_id = {annotation.block_id: annotation for annotation in draft.semantic_annotations}
    unknown_annotation_ids = sorted(set(annotation_by_id) - set(by_id))
    if unknown_annotation_ids:
        errors.append(f"semantic annotations reference unknown blocks: {unknown_annotation_ids}")
    missing_annotations = sorted(set(by_id) - set(annotation_by_id))
    if missing_annotations:
        errors.append(f"semantic annotations are missing blocks: {missing_annotations}")

    position_by_block = {block_id: index for index, block_id in enumerate(selected_ids)}
    slots_after: dict[str | None, list[Any]] = {}
    used_visual_ids: set[str] = set()
    for slot in draft.commentary_slots:
        if slot.after_block_id is not None and slot.after_block_id not in selected_set:
            errors.append(f"slot {slot.slot_id} has unknown after_block_id={slot.after_block_id}")
        if len(slot.preferred_visual_block_ids) != 1:
            errors.append(f"slot {slot.slot_id} must reference exactly one commentary visual block")
        for evidence_id in slot.evidence_block_ids:
            if evidence_id not in selected_set or by_id.get(evidence_id) is None or by_id[evidence_id].kind != "reaction":
                errors.append(f"slot {slot.slot_id} evidence must reference a retained reaction: {evidence_id}")
        for visual_id in slot.preferred_visual_block_ids:
            visual = by_id.get(visual_id)
            if visual is None or visual.kind != "commentary" or not visual.eligible_commentary_visual:
                errors.append(f"slot {slot.slot_id} must use an eligible commentary block as visual: {visual_id}")
            if visual_id in selected_set or visual_id in used_visual_ids:
                errors.append(f"commentary visual is selected or reused: {visual_id}")
            used_visual_ids.add(visual_id)
        slots_after.setdefault(slot.after_block_id, []).append(slot)

    eligible_visual_ids = {
        block.block_id
        for block in blocks.blocks
        if block.kind == "commentary" and block.eligible_commentary_visual
    }
    missing_visual_ids = sorted(eligible_visual_ids - used_visual_ids)
    unexpected_visual_ids = sorted(used_visual_ids - eligible_visual_ids)
    if missing_visual_ids or unexpected_visual_ids:
        errors.append(
            "every eligible commentary block must be assigned to exactly one commentary slot: "
            f"missing={missing_visual_ids}, unexpected={unexpected_visual_ids}"
        )

    if errors:
        raise RemixPlanError("; ".join(errors))

    source_base_duration = sum(_duration(by_id[block_id]) for block_id in selected_ids)
    requested_commentary_budget = max(
        0.0,
        float(source.duration_s) * settings.output_ratio - source_base_duration,
    )
    hard_max_commentary_budget = max(
        0.0,
        float(source.duration_s) * settings.hard_max_output_ratio - source_base_duration,
    )
    slot_targets = _allocate_slot_targets(
        draft.commentary_slots,
        by_id,
        commentary_budget_s=min(requested_commentary_budget, hard_max_commentary_budget),
    )

    raw_items: list[dict[str, Any]] = []

    def append_slot(slot: Any) -> None:
        capacity = max((_duration(by_id[block_id]) for block_id in slot.preferred_visual_block_ids), default=0.0)
        target = slot_targets[slot.slot_id]
        if target <= 0:
            raise RemixPlanError(f"slot {slot.slot_id} has no visual duration capacity")
        raw_items.append(
            {
                "kind": "commentary_slot",
                "role": slot.role,
                "slot_id": slot.slot_id,
                "evidence_block_ids": slot.evidence_block_ids,
                "preferred_visual_block_ids": slot.preferred_visual_block_ids,
                "target_duration_s": target,
                "max_duration_s": capacity,
                "char_budget": max(1, round(target * COMMENTARY_CHARS_PER_SECOND)),
                "reason": slot.reason,
            }
        )

    for slot in slots_after.get(None, []):
        append_slot(slot)
    for choice in draft.ordered_blocks:
        block = by_id[choice.block_id]
        raw_items.append(
            {
                "kind": "source_block",
                "role": choice.role,
                "block_id": block.block_id,
                "start_cut_point_id": block.start_cut_point_id,
                "end_cut_point_id": block.end_cut_point_id,
                "dependency_group_id": block.sequence_group_id,
                "reason": choice.reason,
            }
        )
        for slot in slots_after.get(choice.block_id, []):
            append_slot(slot)

    items: list[RemixPlanItem] = []
    for index, payload in enumerate(raw_items):
        values = dict(payload)
        values.setdefault("evidence_block_ids", [])
        values.setdefault("preferred_visual_block_ids", [])
        items.append(RemixPlanItem(item_id=f"item-{index:04d}", order=index, **values))
    item_by_block = {item.block_id: item for item in items if item.kind == "source_block"}
    # Add commentary list fields after defaults without duplicating kwargs above.
    for index, payload in enumerate(raw_items):
        if payload["kind"] == "commentary_slot":
            items[index] = items[index].model_copy(
                update={
                    "evidence_block_ids": payload["evidence_block_ids"],
                    "preferred_visual_block_ids": payload["preferred_visual_block_ids"],
                }
            )

    source_duration = float(source.duration_s)
    predicted = sum(
        _duration(by_id[item.block_id]) if item.kind == "source_block" else float(item.target_duration_s or 0.0)
        for item in items
    )
    reaction_blocks = [block for block in blocks.blocks if block.kind == "reaction"]
    selected_reactions = [block for block in reaction_blocks if block.block_id in item_by_block]
    annotation_by_id = {item.block_id: item for item in draft.semantic_annotations}
    total_speech, selected_speech, speech_by_block = _reaction_speech_stats(
        blocks,
        transcript,
        {block.block_id for block in selected_reactions},
    )

    def coverage(field: str) -> float:
        all_values = {
            value
            for block in reaction_blocks
            if (value := getattr(annotation_by_id.get(block.block_id), field, None))
        }
        selected_values = {
            value
            for block in selected_reactions
            if (value := getattr(annotation_by_id.get(block.block_id), field, None))
        }
        return len(selected_values) / len(all_values) if all_values else 1.0

    excluded: list[RemixExcludedBlock] = []
    for block in blocks.blocks:
        if block.block_id in selected_set:
            continue
        draft_exclusion = exclusion_by_id.get(block.block_id)
        category = draft_exclusion.reason_code if draft_exclusion else (
            "commentary" if block.block_id in used_visual_ids or block.kind == "commentary" else
            block.kind if block.kind in {"transition", "branding", "broll"} else "other"
        )
        reason = draft_exclusion.reason if draft_exclusion else (
            "Visual retained for replacement commentary." if block.block_id in used_visual_ids else "Not selected for the remix timeline."
        )
        excluded.append(
            RemixExcludedBlock(
                block_id=block.block_id,
                reason=reason,
                category=category,
                source_duration_s=_duration(block),
                unique_reaction_speech_s=speech_by_block.get(block.block_id, 0.0),
            )
        )

    semantic_annotations = [
        RemixSemanticAnnotation(
            **annotation.model_dump()
        )
        for annotation in draft.semantic_annotations
    ]
    plan = RemixPlan(
        schema_version=SCHEMA_VERSION,
        source_hash=blocks.source_hash,
        blocks_hash=blocks_hash,
        original_duration_s=source_duration,
        duration_policy=RemixDurationPolicy(
            hard_min_output_ratio=settings.hard_min_output_ratio,
            preferred_min_output_ratio=settings.preferred_min_output_ratio,
            preferred_max_output_ratio=settings.preferred_max_output_ratio,
            hard_max_output_ratio=settings.hard_max_output_ratio,
            target_duration_s=round(source_duration * settings.output_ratio, 3),
        ),
        items=items,
        excluded_blocks=excluded,
        semantic_annotations=semantic_annotations,
        predicted_duration_s=predicted,
        predicted_output_ratio=predicted / source_duration,
        retention=RemixRetention(
            unique_reaction_speech_ratio=selected_speech / total_speech if total_speech else 1.0,
            reaction_block_ratio=len(selected_reactions) / len(reaction_blocks) if reaction_blocks else 1.0,
            country_coverage_ratio=coverage("country"),
            topic_coverage_ratio=coverage("topic"),
        ),
        llm=ReactionLlmInfo(backend="chatgpt_playwright", session_url=session_url, attempts=attempts),
        created_at=datetime.now(timezone.utc),
        warnings=[],
    )
    if not settings.preferred_min_output_ratio <= plan.predicted_output_ratio <= settings.preferred_max_output_ratio:
        plan.warnings.append("predicted output duration is outside the preferred 0.85-0.90 range")
    return validate_remix_plan(plan, blocks)


def apply_manual_drops_to_plan(
    plan: RemixPlan,
    blocks: ReactionBlocks,
    transcript: ReactionTranscript,
    settings: PlanSettings,
    *,
    blocks_hash: str | None = None,
) -> RemixPlan:
    settings.validate()
    manual_ids = set(settings.manual_drop_block_ids)
    if not manual_ids:
        return validate_remix_plan(plan, blocks)
    if plan.source_hash != blocks.source_hash or transcript.source_hash != plan.source_hash:
        raise RemixPlanError("manual drop source hash does not match plan/blocks/transcript")
    blocks_hash = blocks_hash or plan.blocks_hash
    block_by_id = {block.block_id: block for block in blocks.blocks}
    unknown = sorted(manual_ids - set(block_by_id))
    if unknown:
        raise RemixPlanError(f"manual_drop_block_ids contain unknown block IDs: {unknown}")
    commentary = sorted(block_id for block_id in manual_ids if block_by_id[block_id].kind == "commentary")
    if commentary:
        raise RemixPlanError(f"manual_drop_block_ids cannot target commentary blocks: {commentary}")

    selected_ids = {item.block_id for item in plan.items if item.kind == "source_block"}
    already_excluded = {item.block_id for item in plan.excluded_blocks if item.category == "manual_drop"}
    if manual_ids.issubset(already_excluded) and not (manual_ids & selected_ids):
        return validate_remix_plan(plan, blocks)

    items = [
        item
        for item in plan.items
        if item.kind != "source_block" or item.block_id not in manual_ids
    ]
    if not any(item.kind == "source_block" for item in items):
        raise RemixPlanError("manual_drop_block_ids removed every source block")
    reindexed = [
        item.model_copy(update={"item_id": f"item-{index:04d}", "order": index})
        for index, item in enumerate(items)
    ]
    for item in reindexed:
        if item.kind == "commentary_slot":
            if any(block_id in manual_ids for block_id in item.evidence_block_ids):
                raise RemixPlanError(f"manual_drop_block_ids cannot remove evidence for {item.slot_id}")
            if any(block_id in manual_ids for block_id in item.preferred_visual_block_ids):
                raise RemixPlanError(f"manual_drop_block_ids cannot remove visual block for {item.slot_id}")

    selected_after = {
        item.block_id
        for item in reindexed
        if item.kind == "source_block" and item.block_id is not None
    }
    total_speech, selected_speech, speech_by_block = _reaction_speech_stats(
        blocks,
        transcript,
        {block_id for block_id in selected_after if block_by_id[block_id].kind == "reaction"},
    )
    reaction_blocks = [block for block in blocks.blocks if block.kind == "reaction"]
    selected_reactions = [block for block in reaction_blocks if block.block_id in selected_after]
    annotation_by_id = {item.block_id: item for item in plan.semantic_annotations}

    def coverage(field: str) -> float:
        all_values = {
            value
            for block in reaction_blocks
            if (value := getattr(annotation_by_id.get(block.block_id), field, None))
        }
        selected_values = {
            value
            for block in selected_reactions
            if (value := getattr(annotation_by_id.get(block.block_id), field, None))
        }
        return len(selected_values) / len(all_values) if all_values else 1.0

    excluded = [item for item in plan.excluded_blocks if item.block_id not in manual_ids]
    excluded.extend(
        RemixExcludedBlock(
            block_id=block_id,
            reason=_manual_drop_reason(block_id),
            category="manual_drop",
            source_duration_s=_duration(block_by_id[block_id]),
            unique_reaction_speech_s=speech_by_block.get(block_id, 0.0),
        )
        for block_id in sorted(manual_ids)
    )
    predicted = sum(
        _duration(block_by_id[item.block_id])
        if item.kind == "source_block"
        else float(item.target_duration_s or 0.0)
        for item in reindexed
    )
    warnings = list(dict.fromkeys([
        *plan.warnings,
        *(f"manual_drop applied to {block_id}" for block_id in sorted(manual_ids)),
    ]))
    if not settings.preferred_min_output_ratio <= predicted / plan.original_duration_s <= settings.preferred_max_output_ratio:
        warnings.append("predicted output duration is outside the preferred 0.85-0.90 range")
        warnings = list(dict.fromkeys(warnings))
    updated = plan.model_copy(
        update={
            "blocks_hash": blocks_hash,
            "items": reindexed,
            "excluded_blocks": excluded,
            "predicted_duration_s": predicted,
            "predicted_output_ratio": predicted / plan.original_duration_s,
            "retention": RemixRetention(
                unique_reaction_speech_ratio=selected_speech / total_speech if total_speech else 1.0,
                reaction_block_ratio=len(selected_reactions) / len(reaction_blocks) if reaction_blocks else 1.0,
                country_coverage_ratio=coverage("country"),
                topic_coverage_ratio=coverage("topic"),
            ),
            "warnings": warnings,
        }
    )
    return validate_remix_plan(updated, blocks)


async def build_remix_plan(
    source: ReactionSource,
    transcript: ReactionTranscript,
    blocks: ReactionBlocks,
    client: ChatClient,
    *,
    settings: PlanSettings | None = None,
    session_url: str | None = None,
    blocks_hash: str | None = None,
) -> RemixPlan:
    settings = settings or PlanSettings()
    settings.validate()
    if source.input_hash != blocks.source_hash or source.input_hash != transcript.source_hash:
        raise RemixPlanError("reaction source, transcript, and block source hashes do not match")
    blocks_hash = blocks_hash or stable_hash(blocks.model_dump(mode="json"))
    block_catalog = build_block_catalog(blocks, transcript)
    eligible_commentary_visual_ids = [
        str(item["block_id"])
        for item in block_catalog
        if item["kind"] == "commentary" and item["eligible_commentary_visual"]
    ]
    prompt = build_plan_prompt(
        source_duration_s=source.duration_s,
        block_catalog=block_catalog,
        target_duration_s=source.duration_s * settings.output_ratio,
        hard_min_duration_s=source.duration_s * settings.hard_min_output_ratio,
        hard_max_duration_s=source.duration_s * settings.hard_max_output_ratio,
        min_reaction_retention=settings.min_unique_reaction_speech_ratio,
    )
    previous_payload: object | None = None
    last_errors: list[str] = []
    for attempt in range(1, settings.max_semantic_repairs + 2):
        response = await client.ask(prompt)
        try:
            previous_payload = extract_json(response)
            draft = PlannerDraft.model_validate(previous_payload)
            draft = _restore_required_blocks(draft, blocks, transcript, settings)
            draft = _apply_manual_drops_to_draft(draft, blocks, settings)
            return _build_plan(
                source,
                transcript,
                blocks,
                draft,
                settings,
                session_url=session_url,
                attempts=attempt,
                blocks_hash=blocks_hash,
            )
        except (ValidationError, ValueError, RemixPlanError) as exc:
            last_errors = [str(exc)]
            if attempt > settings.max_semantic_repairs:
                break
            prompt = build_plan_repair_prompt(
                previous=previous_payload or response,
                errors=last_errors,
                eligible_commentary_visual_ids=eligible_commentary_visual_ids,
            )
    raise RemixPlanError("plan failed deterministic validation after repair: " + "; ".join(last_errors))


def apply_duration_restore(
    plan: RemixPlan,
    blocks: ReactionBlocks,
    repairs: RemixRepairRequests,
    *,
    transcript: ReactionTranscript,
    blocks_hash: str | None = None,
) -> RemixPlan:
    if repairs.source_hash != plan.source_hash or plan.source_hash != blocks.source_hash:
        raise RemixPlanError("duration repair source hash does not match plan/blocks")
    if transcript.source_hash != plan.source_hash:
        raise RemixPlanError("duration repair transcript source hash does not match plan")
    requested = {
        block_id
        for request in repairs.items
        if request.kind == "duration_restore" and request.requested_stage == "plan"
        for block_id in request.affected_ids
    }
    if not requested:
        raise RemixPlanError("repair request contains no duration_restore block IDs")
    block_by_id = {block.block_id: block for block in blocks.blocks}
    selected_ids = {item.block_id for item in plan.items if item.kind == "source_block"}
    excluded_ids = {item.block_id for item in plan.excluded_blocks}
    invalid = sorted(
        block_id
        for block_id in requested
        if block_id not in block_by_id
        or block_id not in excluded_ids
        or block_by_id[block_id].kind != "reaction"
    )
    if invalid:
        raise RemixPlanError(f"duration repair IDs must be omitted reaction blocks: {invalid}")
    if requested & selected_ids:
        raise RemixPlanError("duration repair cannot reuse selected blocks")

    items = list(sorted(plan.items, key=lambda item: item.order))
    for block in sorted((block_by_id[block_id] for block_id in requested), key=lambda value: value.tc_start):
        insertion = next((index for index, item in enumerate(items) if item.role == "close"), len(items))
        if block.sequence_group_id is not None and block.sequence_index is not None:
            group_positions = [
                (index, block_by_id[item.block_id])
                for index, item in enumerate(items)
                if item.kind == "source_block"
                and item.block_id in block_by_id
                and block_by_id[item.block_id].sequence_group_id == block.sequence_group_id
            ]
            higher = [index for index, member in group_positions if member.sequence_index is not None and member.sequence_index > block.sequence_index]
            lower = [index for index, member in group_positions if member.sequence_index is not None and member.sequence_index < block.sequence_index]
            if higher:
                insertion = min(higher)
            elif lower:
                insertion = max(lower) + 1
        items.insert(
            insertion,
            RemixPlanItem(
                item_id="repair-placeholder",
                order=0,
                kind="source_block",
                role="body",
                block_id=block.block_id,
                start_cut_point_id=block.start_cut_point_id,
                end_cut_point_id=block.end_cut_point_id,
                dependency_group_id=block.sequence_group_id,
                reason="Restored by deterministic duration repair.",
            ),
        )
    items = [item.model_copy(update={"item_id": f"item-{index:04d}", "order": index}) for index, item in enumerate(items)]
    selected_reactions = {
        item.block_id for item in items if item.kind == "source_block" and block_by_id[item.block_id].kind == "reaction"
    }
    reaction_blocks = [block for block in blocks.blocks if block.kind == "reaction"]
    total_speech, selected_speech, _speech_by_block = _reaction_speech_stats(
        blocks,
        transcript,
        selected_reactions,
    )
    annotations = {item.block_id: item for item in plan.semantic_annotations}

    def coverage(field: str) -> float:
        all_values = {getattr(annotations.get(block.block_id), field, None) for block in reaction_blocks}
        selected_values = {
            getattr(annotations.get(block.block_id), field, None)
            for block in reaction_blocks
            if block.block_id in selected_reactions
        }
        all_values.discard(None)
        selected_values.discard(None)
        return len(selected_values) / len(all_values) if all_values else 1.0

    predicted = sum(
        _duration(block_by_id[item.block_id]) if item.kind == "source_block" else float(item.target_duration_s or 0.0)
        for item in items
    )
    hard_max_s = plan.original_duration_s * plan.duration_policy.hard_max_output_ratio
    if predicted > hard_max_s + 1e-6:
        raise RemixPlanError("duration repair would exceed the hard maximum output duration")
    updated = plan.model_copy(
        update={
            "blocks_hash": blocks_hash or plan.blocks_hash,
            "items": items,
            "excluded_blocks": [item for item in plan.excluded_blocks if item.block_id not in requested],
            "predicted_duration_s": predicted,
            "predicted_output_ratio": predicted / plan.original_duration_s,
            "retention": RemixRetention(
                unique_reaction_speech_ratio=selected_speech / total_speech if total_speech else 1.0,
                reaction_block_ratio=len(selected_reactions) / len(reaction_blocks) if reaction_blocks else 1.0,
                country_coverage_ratio=coverage("country"),
                topic_coverage_ratio=coverage("topic"),
            ),
            "created_at": datetime.now(timezone.utc),
            "warnings": [*plan.warnings, f"restored {len(requested)} reaction block(s) from QA duration repair"],
        }
    )
    return validate_remix_plan(updated, blocks)
