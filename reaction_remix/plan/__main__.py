from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from common.integrity import atomic_write_json, file_hash, stable_hash
from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from common.schema import ReactionBlocks, ReactionSource, ReactionTranscript, RemixPlan, RemixRepairRequests, validate_remix_plan
from reaction_remix.plan.core import (
    PLAN_PROMPT_VERSION,
    PlanSettings,
    RemixPlanError,
    apply_duration_restore,
    apply_manual_drops_to_plan,
    build_remix_plan,
)
from reaction_remix.plan.session import resolve_session, save_session
from review.playwright_chat import PlaywrightChatClient, PlaywrightChatError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction Remix R3: plan reaction order and commentary slots")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--blocks", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--qa-output", type=Path, default=None)
    parser.add_argument("--repair-request", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=Path("work/reaction-remix/plan"))
    parser.add_argument("--output-ratio", type=float, default=0.875)
    parser.add_argument("--hard-min-output-ratio", type=float, default=0.80)
    parser.add_argument("--preferred-min-output-ratio", type=float, default=0.85)
    parser.add_argument("--preferred-max-output-ratio", type=float, default=0.90)
    parser.add_argument("--hard-max-output-ratio", type=float, default=1.00)
    parser.add_argument("--min-unique-reaction-speech-ratio", type=float, default=0.90)
    parser.add_argument("--manual-drop-block-id", action="append", default=[])
    parser.add_argument("--chatgpt-profile-dir", type=Path, default=CHATGPT_PLAYWRIGHT_PROFILE_DIR)
    parser.add_argument("--chat-session-policy", choices=["auto", "new", "resume"], default="auto")
    parser.add_argument("--chat-session-meta", type=Path, default=None)
    parser.add_argument("--reply-timeout-s", type=int, default=1200)
    parser.add_argument("--playwright-max-attempts", type=int, default=2)
    parser.add_argument("--playwright-recovery-timeout-s", type=int, default=60)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return parser


def _read_model(path: Path, model):  # type: ignore[no-untyped-def]
    if not path.is_file():
        raise RemixPlanError(f"required input does not exist: {path}")
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _profile(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    canonical = CHATGPT_PLAYWRIGHT_PROFILE_DIR.resolve()
    if resolved != canonical:
        raise RemixPlanError(f"reaction ChatGPT profile is locked to {canonical}")
    return canonical


def _apply_duration_repair_idempotently(
    plan: RemixPlan,
    blocks: ReactionBlocks,
    transcript: ReactionTranscript,
    repairs: RemixRepairRequests,
    *,
    blocks_hash: str,
) -> RemixPlan:
    requested = {
        block_id
        for item in repairs.items
        if item.kind == "duration_restore" and item.requested_stage == "plan"
        for block_id in item.affected_ids
    }
    if not requested:
        raise RemixPlanError("repair request contains no duration_restore block IDs")
    selected = {
        item.block_id
        for item in plan.items
        if item.kind == "source_block" and item.block_id is not None
    }
    remaining = requested - selected
    if not remaining:
        return validate_remix_plan(plan, blocks)
    filtered_items = []
    for item in repairs.items:
        if item.kind != "duration_restore" or item.requested_stage != "plan":
            continue
        affected_ids = [block_id for block_id in item.affected_ids if block_id in remaining]
        if affected_ids:
            filtered_items.append(item.model_copy(update={"affected_ids": affected_ids}))
    filtered = repairs.model_copy(update={"items": filtered_items})
    return apply_duration_restore(
        plan,
        blocks,
        filtered,
        transcript=transcript,
        blocks_hash=blocks_hash,
    )


async def run(args: argparse.Namespace) -> int:
    source = _read_model(args.source, ReactionSource)
    transcript = _read_model(args.transcript, ReactionTranscript)
    blocks = _read_model(args.blocks, ReactionBlocks)
    if blocks.transcript_hash != file_hash(args.transcript.expanduser().resolve()):
        raise RemixPlanError("reaction blocks transcript hash does not match reaction_transcript.json")
    output = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    settings = PlanSettings(
        output_ratio=args.output_ratio,
        hard_min_output_ratio=args.hard_min_output_ratio,
        preferred_min_output_ratio=args.preferred_min_output_ratio,
        preferred_max_output_ratio=args.preferred_max_output_ratio,
        hard_max_output_ratio=args.hard_max_output_ratio,
        min_unique_reaction_speech_ratio=args.min_unique_reaction_speech_ratio,
        manual_drop_block_ids=tuple(args.manual_drop_block_id or ()),
    )
    settings.validate()
    blocks_file_hash = file_hash(args.blocks.expanduser().resolve())
    if blocks_file_hash is None:
        raise RemixPlanError(f"could not hash reaction blocks: {args.blocks}")
    identity = stable_hash(
        {
            "source": source.model_dump(mode="json"),
            "transcript": transcript.model_dump(mode="json"),
            "blocks": blocks.model_dump(mode="json"),
            "blocks_file_hash": blocks_file_hash,
            "settings": settings.__dict__,
            "prompt_version": PLAN_PROMPT_VERSION,
            "repair_request_hash": file_hash(args.repair_request) if args.repair_request else None,
        }
    )
    manifest_path = work_dir / "cache_manifest.json"
    if not args.force and manifest_path.is_file() and output.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("cache_key") == identity:
                validate_remix_plan(RemixPlan.model_validate_json(output.read_text(encoding="utf-8")), blocks)
                return 0
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    repairs = None
    if settings.manual_drop_block_ids and output.is_file():
        try:
            existing = _read_model(output, RemixPlan)
            plan = apply_manual_drops_to_plan(
                existing,
                blocks,
                transcript,
                settings,
                blocks_hash=blocks_file_hash,
            )
        except (RemixPlanError, ValueError):
            plan = None
        if plan is not None:
            atomic_write_json(output, plan.model_dump(mode="json"))
            qa_output = (args.qa_output or output.with_name("plan.qa.json")).expanduser().resolve()
            atomic_write_json(
                qa_output,
                {
                    "passed": True,
                    "manual_drop_block_ids": list(settings.manual_drop_block_ids),
                    "predicted_duration_s": plan.predicted_duration_s,
                    "predicted_output_ratio": plan.predicted_output_ratio,
                    "retention": plan.retention.model_dump(mode="json"),
                    "warnings": plan.warnings,
                },
            )
            atomic_write_json(manifest_path, {"cache_key": identity, "output_hash": file_hash(output)})
            return 0
    if args.repair_request is not None:
        if not args.repair_request.is_file():
            raise RemixPlanError(f"repair request does not exist: {args.repair_request}")
        repairs = _read_model(args.repair_request, RemixRepairRequests)
        if output.is_file():
            try:
                existing = _read_model(output, RemixPlan)
                plan = _apply_duration_repair_idempotently(
                    existing,
                    blocks,
                    transcript,
                    repairs,
                    blocks_hash=blocks_file_hash,
                )
            except (RemixPlanError, ValueError):
                plan = None
            if plan is not None:
                atomic_write_json(output, plan.model_dump(mode="json"))
                qa_output = (args.qa_output or output.with_name("plan.qa.json")).expanduser().resolve()
                atomic_write_json(
                    qa_output,
                    {
                        "passed": True,
                        "repair_request": str(args.repair_request),
                        "predicted_duration_s": plan.predicted_duration_s,
                        "predicted_output_ratio": plan.predicted_output_ratio,
                        "retention": plan.retention.model_dump(mode="json"),
                        "warnings": plan.warnings,
                    },
                )
                atomic_write_json(manifest_path, {"cache_key": identity, "output_hash": file_hash(output)})
                return 0

    profile = _profile(args.chatgpt_profile_dir)
    session_path = (args.chat_session_meta or work_dir.parent / "editorial_chat_session.json").expanduser().resolve()
    initial_url, previous, session_warnings = resolve_session(
        session_path,
        args.chat_session_policy,
        source_hash=source.input_hash,
        blocks_hash=blocks_file_hash,
        content_hash=identity,
    )
    async with PlaywrightChatClient(
        profile,
        headless=args.headless,
        initial_url=initial_url,
        timeout_s=args.reply_timeout_s,
        max_attempts=args.playwright_max_attempts,
        recovery_timeout_s=args.playwright_recovery_timeout_s,
        resume_matching_prompts=True,
    ) as client:
        plan = await build_remix_plan(
            source,
            transcript,
            blocks,
            client,
            settings=settings,
            session_url=client.current_url,
            blocks_hash=blocks_file_hash,
        )
        if repairs is not None:
            plan = _apply_duration_repair_idempotently(
                plan,
                blocks,
                transcript,
                repairs,
                blocks_hash=blocks_file_hash,
            )
        plan = plan.model_copy(update={"llm": plan.llm.model_copy(update={"session_url": client.current_url})})
        atomic_write_json(output, plan.model_dump(mode="json"))
        output_hash = file_hash(output)
        if output_hash is None:
            raise RemixPlanError(f"could not hash remix plan output: {output}")
        save_session(
            session_path,
            policy=args.chat_session_policy,
            chat_url=client.current_url,
            profile_dir=profile,
            source_hash=source.input_hash,
            blocks_hash=plan.blocks_hash,
            content_hash=identity,
            plan_hash=output_hash,
            title=output.stem,
            previous=previous,
            warnings=session_warnings,
        )
    qa_output = (args.qa_output or output.with_name("plan.qa.json")).expanduser().resolve()
    atomic_write_json(
        qa_output,
        {
            "passed": True,
            **({"repair_request": str(args.repair_request)} if repairs is not None else {}),
            "predicted_duration_s": plan.predicted_duration_s,
            "predicted_output_ratio": plan.predicted_output_ratio,
            "retention": plan.retention.model_dump(mode="json"),
            "warnings": plan.warnings,
        },
    )
    atomic_write_json(manifest_path, {"cache_key": identity, "output_hash": file_hash(output)})
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run(args))
    except (RemixPlanError, PlaywrightChatError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"reaction-remix plan: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
