from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.integrity import file_hash
from common.schema import AudioAssets, CommentaryAudio, ReactionBlocks, ReactionSource, RemixEdl, RemixPlan, RemixRepairRequests


class ComposeError(RuntimeError):
    pass


def _resolved_artifact_path(base_dir: Path, value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ComposeError(f"audio artifact does not exist or is empty: {resolved}")
    return resolved.as_posix()


def _repair_requests(source_hash: str, slot_id: str, reason: str) -> RemixRepairRequests:
    return RemixRepairRequests.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source_hash,
            "items": [
                {
                    "repair_id": "repair-0000",
                    "kind": "tts_fit",
                    "affected_ids": [slot_id],
                    "reason": reason,
                    "attempt": 1,
                    "requested_stage": "write",
                }
            ],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": [],
        }
    )


def compose_remix(
    *,
    film_path: Path,
    source: ReactionSource,
    blocks: ReactionBlocks,
    plan: RemixPlan,
    commentary_audio: CommentaryAudio,
    commentary_audio_base: Path,
    plan_hash: str,
    commentary_audio_hash: str,
    audio_assets: AudioAssets | None = None,
    audio_assets_base: Path | None = None,
    tts_gain_db: float = 1.0,
    bed_gain_db: float = -14.0,
    boundary_fade_ms: int = 50,
    bed_fade_ms: int = 180,
    force_tts_slots: set[str] | None = None,
) -> tuple[RemixEdl, RemixRepairRequests | None]:
    force_tts_slots = force_tts_slots or set()
    if source.input_hash != blocks.source_hash or source.input_hash != plan.source_hash:
        raise ComposeError("source hashes do not match across reaction-remix inputs")
    if commentary_audio.source_hash != source.input_hash:
        raise ComposeError("commentary audio source hash does not match source")

    cuts = {item.cut_point_id: item for item in blocks.cut_points}
    blocks_by_id = {item.block_id: item for item in blocks.blocks}
    audio_by_slot = {item.slot_id: item for item in commentary_audio.items}
    approved_bed = None
    if audio_assets is not None:
        if audio_assets.source_hash != source.input_hash:
            raise ComposeError("audio assets source hash does not match source")
        approved_bed = next(
            (
                item
                for item in audio_assets.items
                if item.kind == "no_vocals"
                and not item.leakage_detected
                and item.src_tc_start is not None
                and item.src_tc_end is not None
            ),
            None,
        )

    placements: list[dict[str, Any]] = []
    # A block selected as source footage is never eligible for commentary reuse,
    # regardless of whether it appears before or after the commentary slot.
    used_commentary_visuals: set[str] = {
        item.block_id for item in plan.items if item.kind == "source_block" and item.block_id is not None
    }
    selected_source_block_ids = {
        item.block_id
        for item in plan.items
        if item.kind == "source_block" and item.block_id is not None
    }
    timeline_cursor = 0.0
    for plan_item in sorted(plan.items, key=lambda item: item.order):
        if plan_item.kind == "source_block":
            if not plan_item.block_id or not plan_item.start_cut_point_id or not plan_item.end_cut_point_id:
                raise ComposeError(f"source item {plan_item.item_id} is incomplete")
            block = blocks_by_id.get(plan_item.block_id)
            start_cut = cuts.get(plan_item.start_cut_point_id)
            end_cut = cuts.get(plan_item.end_cut_point_id)
            if block is None or start_cut is None or end_cut is None:
                raise ComposeError(f"source item {plan_item.item_id} references missing block/cut point")
            src_in = start_cut.tc
            src_out = end_cut.tc
            if src_in < block.tc_start - 1e-3 or src_out > block.tc_end + 1e-3 or src_out <= src_in:
                raise ComposeError(f"source item {plan_item.item_id} uses cuts outside its block")
            duration = src_out - src_in
            placements.append(
                _source_placement(
                    placement_id=f"placement-{len(placements):04d}",
                    item_id=plan_item.item_id,
                    block_id=block.block_id,
                    kind=block.kind,
                    film_path=film_path,
                    src_in=src_in,
                    src_out=src_out,
                    tl_start=timeline_cursor,
                )
            )
            timeline_cursor += duration
            continue

        if not plan_item.slot_id:
            raise ComposeError(f"commentary item {plan_item.item_id} has no slot_id")
        audio_item = audio_by_slot.get(plan_item.slot_id)
        if audio_item is None:
            raise ComposeError(f"commentary slot {plan_item.slot_id} has no synthesized audio")
        fit_tolerance_s = max(0.1, source.video.fps_den / source.video.fps_num)
        candidate = _select_commentary_visual(
            plan_item.preferred_visual_block_ids,
            blocks_by_id,
            selected_source_block_ids | used_commentary_visuals,
            audio_item.duration_s,
            fit_tolerance_s,
        )
        if candidate is None:
            repair = _repair_requests(
                source.input_hash,
                plan_item.slot_id,
                f"No unused commentary block has {audio_item.duration_s:.3f}s capacity",
            )
            return _empty_edl(source, plan_hash, commentary_audio_hash), repair
        used_commentary_visuals.add(candidate.block_id)
        src_in = candidate.tc_start
        placement_duration_s = min(audio_item.duration_s, candidate.tc_end - candidate.tc_start)
        src_out = src_in + placement_duration_s
        tts_path = _resolved_artifact_path(commentary_audio_base, audio_item.audio_path)
        bed_path: str | None = None
        mode = "tts"
        bed_in = None
        bed_out = None
        filters: list[str] = [f"boundary_fade_{boundary_fade_ms}ms", "commentary_limiter_-1.5db"]
        if approved_bed is not None and audio_assets_base is not None and plan_item.slot_id not in force_tts_slots:
            resolved_bed = Path(_resolved_artifact_path(audio_assets_base, approved_bed.path))
            if (
                file_hash(resolved_bed) == approved_bed.content_hash
                and approved_bed.src_tc_start <= src_in + 1e-6
                and approved_bed.src_tc_end >= src_out - 1e-6
            ):
                mode = "tts_bed"
                bed_path = resolved_bed.as_posix()
                bed_in = src_in
                bed_out = src_out
                filters.insert(0, f"bed_fade_{bed_fade_ms}ms")
        placements.append(
            {
                "placement_id": f"placement-{len(placements):04d}",
                "item_id": plan_item.item_id,
                "kind": "commentary",
                "origin_block_id": candidate.block_id,
                "tl_start": timeline_cursor,
                "tl_end": timeline_cursor + placement_duration_s,
                "video": {
                    "src": film_path.resolve().as_posix(),
                    "src_in": src_in,
                    "src_out": src_out,
                    "speed": 1.0,
                    "filters": [],
                },
                "audio": {
                    "mode": mode,
                    "source_src": None,
                    "source_in": None,
                    "source_out": None,
                    "source_gain_db": None,
                    "tts_audio_path": tts_path,
                    "tts_gain_db": tts_gain_db,
                    "bed_audio_path": bed_path,
                    "bed_in": bed_in,
                    "bed_out": bed_out,
                    "bed_gain_db": bed_gain_db if bed_path else None,
                    "filters": filters,
                },
                "warnings": (
                    [f"TTS exceeds visual capacity by {audio_item.duration_s - placement_duration_s:.3f}s within fit tolerance"]
                    if placement_duration_s < audio_item.duration_s
                    else []
                ),
            }
        )
        timeline_cursor += placement_duration_s

    ratio = timeline_cursor / source.duration_s
    if ratio < plan.duration_policy.hard_min_output_ratio - 1e-6 or ratio > plan.duration_policy.hard_max_output_ratio + 1e-6:
        raise ComposeError(
            f"composed duration ratio {ratio:.4f} is outside "
            f"{plan.duration_policy.hard_min_output_ratio:.2f}-{plan.duration_policy.hard_max_output_ratio:.2f}"
        )
    payload = {
        "schema_version": "reaction-remix.v1",
        "source_hash": source.input_hash,
        "plan_hash": plan_hash,
        "commentary_audio_hash": commentary_audio_hash,
        "output": {
            "width": source.video.width,
            "height": source.video.height,
            "fps_num": source.video.fps_num,
            "fps_den": source.video.fps_den,
            "audio_sample_rate": source.audio.sample_rate,
            "audio_channels": source.audio.channels,
        },
        "visual_policy": {
            "mask_subtitles": False,
            "add_subtitles": False,
            "add_text": False,
            "blur": False,
            "overlay": False,
            "preserve_burned_in_pixels": True,
        },
        "placements": placements,
        "total_duration_s": timeline_cursor,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "warnings": [],
    }
    return RemixEdl.model_validate(payload), None


def _source_placement(
    *,
    placement_id: str,
    item_id: str,
    block_id: str,
    kind: str,
    film_path: Path,
    src_in: float,
    src_out: float,
    tl_start: float,
) -> dict[str, Any]:
    duration = src_out - src_in
    return {
        "placement_id": placement_id,
        "item_id": item_id,
        "kind": kind,
        "origin_block_id": block_id,
        "tl_start": tl_start,
        "tl_end": tl_start + duration,
        "video": {
            "src": film_path.resolve().as_posix(),
            "src_in": src_in,
            "src_out": src_out,
            "speed": 1.0,
            "filters": [],
        },
        "audio": {
            "mode": "source",
            "source_src": film_path.resolve().as_posix(),
            "source_in": src_in,
            "source_out": src_out,
            "source_gain_db": 0.0,
            "tts_audio_path": None,
            "tts_gain_db": None,
            "bed_audio_path": None,
            "bed_in": None,
            "bed_out": None,
            "bed_gain_db": None,
            "filters": [],
        },
        "warnings": [],
    }


def _select_commentary_visual(
    preferred_ids: list[str],
    blocks_by_id: dict[str, Any],
    unavailable: set[str],
    duration_s: float,
    tolerance_s: float,
) -> Any | None:
    ranked: list[Any] = []
    preferred = [blocks_by_id[block_id] for block_id in preferred_ids if block_id in blocks_by_id]
    ranked.extend(block for block in preferred if block.kind == "commentary")
    ranked.extend(
        block for block in blocks_by_id.values() if block.kind == "commentary" and block.block_id not in preferred_ids
    )
    for block in ranked:
        if block.block_id in unavailable:
            continue
        if block.tc_end - block.tc_start + tolerance_s >= duration_s:
            return block
    return None


def _empty_edl(source: ReactionSource, plan_hash: str, commentary_audio_hash: str) -> RemixEdl:
    return RemixEdl.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source.input_hash,
            "plan_hash": plan_hash,
            "commentary_audio_hash": commentary_audio_hash,
            "output": {
                "width": source.video.width,
                "height": source.video.height,
                "fps_num": source.video.fps_num,
                "fps_den": source.video.fps_den,
                "audio_sample_rate": source.audio.sample_rate,
                "audio_channels": source.audio.channels,
            },
            "visual_policy": {
                "mask_subtitles": False,
                "add_subtitles": False,
                "add_text": False,
                "blur": False,
                "overlay": False,
                "preserve_burned_in_pixels": True,
            },
            "placements": [],
            "total_duration_s": 0.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": ["compose requires repair before rendering"],
        }
    )
