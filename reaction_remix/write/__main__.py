from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from common.integrity import atomic_write_json, file_hash, stable_hash
from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from common.schema import (
    CommentaryFitRequests,
    CommentaryScript,
    ReactionBlocks,
    ReactionTranscript,
    RemixPlan,
    validate_commentary_script,
)
from reaction_remix.plan.session import resolve_session, save_session
from reaction_remix.write.core import (
    CommentaryWriteError,
    WriteSettings,
    build_commentary_script,
    repair_commentary_for_fit,
)
from review.playwright_chat import PlaywrightChatClient, PlaywrightChatError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction Remix R4: write Japanese commentary")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--blocks", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--qa-output", type=Path, default=None)
    parser.add_argument("--fit-request", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=Path("work/reaction-remix/write"))
    parser.add_argument("--style-id", default="reaction-internet-ja-v1")
    parser.add_argument("--max-qa-iterations", type=int, default=2)
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


def _read(path: Path, model):  # type: ignore[no-untyped-def]
    if not path.is_file():
        raise CommentaryWriteError(f"required input does not exist: {path}")
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _profile(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    canonical = CHATGPT_PLAYWRIGHT_PROFILE_DIR.resolve()
    if resolved != canonical:
        raise CommentaryWriteError(f"reaction ChatGPT profile is locked to {canonical}")
    return canonical


def _refresh_existing_script_for_plan(script: CommentaryScript, plan: RemixPlan, *, plan_hash: str) -> CommentaryScript | None:
    if script.source_hash != plan.source_hash:
        return None
    plan_slots = {item.slot_id: item for item in plan.items if item.kind == "commentary_slot"}
    script_slots = {slot.slot_id: slot for slot in script.slots}
    if set(script_slots) != set(plan_slots):
        return None
    ordered_plan = sorted(plan.items, key=lambda item: item.order)
    refreshed_slots = []
    for slot in script.slots:
        item = plan_slots[slot.slot_id]
        if item is None:
            return None
        if not slot.evidence_block_ids or not set(slot.evidence_block_ids).issubset(set(item.evidence_block_ids)):
            return None
        if abs(slot.target_duration_s - float(item.target_duration_s or 0.0)) > 1e-6:
            return None
        if abs(slot.max_duration_s - float(item.max_duration_s or 0.0)) > 1e-6 or slot.char_budget != item.char_budget:
            return None
        if not all((slot.qa.language_ok, slot.qa.evidence_ok, slot.qa.style_ok, slot.qa.length_ok)):
            return None
        plan_index = ordered_plan.index(item)
        expected_before = ordered_plan[plan_index - 1].item_id if plan_index > 0 else None
        expected_after = ordered_plan[plan_index + 1].item_id if plan_index + 1 < len(ordered_plan) else None
        refreshed_slots.append(
            slot.model_copy(
                update={
                    "before_item_id": expected_before,
                    "after_item_id": expected_after,
                }
            )
        )
    warnings = list(dict.fromkeys([*script.warnings, "script metadata refreshed after manual plan drop"]))
    refreshed = script.model_copy(update={"plan_hash": plan_hash, "slots": refreshed_slots, "warnings": warnings})
    return validate_commentary_script(refreshed, plan)


async def run(args: argparse.Namespace) -> int:
    plan = _read(args.plan, RemixPlan)
    blocks = _read(args.blocks, ReactionBlocks)
    transcript = _read(args.transcript, ReactionTranscript)
    output = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    settings = WriteSettings(style_id=args.style_id, max_qa_iterations=args.max_qa_iterations)
    settings.validate()
    plan_hash = file_hash(args.plan.expanduser().resolve())
    if plan_hash is None:
        raise CommentaryWriteError(f"could not hash remix plan: {args.plan}")
    identity = stable_hash(
        {
            "plan_hash": plan_hash,
            "blocks_hash": stable_hash(blocks.model_dump(mode="json")),
            "transcript_hash": stable_hash(transcript.model_dump(mode="json")),
            "settings": settings.__dict__,
            "prompt_version": "reaction-write-v2",
        }
    )
    fit_requests = _read(args.fit_request, CommentaryFitRequests) if args.fit_request and args.fit_request.is_file() else None
    if fit_requests is not None and not fit_requests.requests and output.is_file() and not args.force:
        CommentaryScript.model_validate_json(output.read_text(encoding="utf-8"))
        return 0
    if fit_requests is None and output.is_file() and not args.force:
        existing_script = CommentaryScript.model_validate_json(output.read_text(encoding="utf-8"))
        refreshed_script = _refresh_existing_script_for_plan(existing_script, plan, plan_hash=plan_hash)
        if refreshed_script is not None:
            atomic_write_json(output, refreshed_script.model_dump(mode="json"))
            atomic_write_json(
                (args.qa_output or output.with_name("script.qa.json")).expanduser().resolve(),
                {
                    "passed": True,
                    "refreshed_from_existing_script": True,
                    "slot_count": len(refreshed_script.slots),
                    "warnings": refreshed_script.warnings,
                },
            )
            atomic_write_json(work_dir / "cache_manifest.json", {"cache_key": identity, "script_hash": file_hash(output)})
            return 0
    profile = _profile(args.chatgpt_profile_dir)
    session_path = (args.chat_session_meta or work_dir.parent / "editorial_chat_session.json").expanduser().resolve()
    initial_url, previous, session_warnings = resolve_session(
        session_path,
        args.chat_session_policy,
        source_hash=plan.source_hash,
        blocks_hash=plan.blocks_hash,
        plan_hash=plan_hash,
        content_hash=identity,
    )
    if not args.force and fit_requests is None and output.is_file() and (work_dir / "cache_manifest.json").is_file():
        manifest = json.loads((work_dir / "cache_manifest.json").read_text(encoding="utf-8"))
        if manifest.get("cache_key") == identity:
            CommentaryScript.model_validate_json(output.read_text(encoding="utf-8"))
            return 0
    async with PlaywrightChatClient(
        profile,
        headless=args.headless,
        initial_url=initial_url,
        timeout_s=args.reply_timeout_s,
        max_attempts=args.playwright_max_attempts,
        recovery_timeout_s=args.playwright_recovery_timeout_s,
        resume_matching_prompts=True,
    ) as client:
        if fit_requests is not None and fit_requests.requests:
            if not output.is_file():
                raise CommentaryWriteError("fit repair requires an existing commentary_script.json")
            script = _read(output, CommentaryScript)
            current_script_hash = file_hash(output)
            if current_script_hash is None:
                raise CommentaryWriteError(f"could not hash commentary script: {output}")
            script, qa_report = await repair_commentary_for_fit(
                script,
                fit_requests,
                client,
                plan=plan,
                blocks=blocks,
                transcript=transcript,
                script_hash=current_script_hash,
            )
            qa_report["fit_repair_slots"] = [item.slot_id for item in fit_requests.requests]
        else:
            script, qa_report = await build_commentary_script(
                plan,
                blocks,
                transcript,
                client,
                settings=settings,
                session_url=client.current_url,
                plan_hash=plan_hash,
            )
        script = script.model_copy(update={"llm": script.llm.model_copy(update={"session_url": client.current_url})})
        atomic_write_json(output, script.model_dump(mode="json"))
        save_session(
            session_path,
            policy=args.chat_session_policy,
            chat_url=client.current_url,
            profile_dir=profile,
            source_hash=plan.source_hash,
            blocks_hash=plan.blocks_hash,
            plan_hash=plan_hash,
            content_hash=identity,
            title=output.stem,
            previous=previous,
            warnings=session_warnings,
        )
    atomic_write_json((args.qa_output or output.with_name("script.qa.json")).expanduser().resolve(), qa_report)
    atomic_write_json(work_dir / "cache_manifest.json", {"cache_key": identity, "script_hash": file_hash(output)})
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run(args))
    except (CommentaryWriteError, PlaywrightChatError, ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"reaction-remix write: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
