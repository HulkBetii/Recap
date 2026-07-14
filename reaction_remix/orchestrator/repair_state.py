from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from common.integrity import atomic_write_json, file_hash
from common.schema import RemixQa, RemixRepairRequests
from reaction_remix.orchestrator.paths import ReactionRunPaths


ACCEPTED_LEDGER_VERSION = "reaction-remix.accepted-repairs.v1"
DURABLE_REPAIR_KINDS = {"duration_restore", "bed_leakage"}


def load_accepted_repair_request(paths: ReactionRunPaths) -> Path | None:
    ledger_path = paths.accepted_repair_ledger
    if not ledger_path.is_file() or not paths.remix_qa.is_file():
        return None
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger.get("version") != ACCEPTED_LEDGER_VERSION:
            return None
        request_hash = str(ledger["request_hash"])
        request_name = str(ledger["request_file"])
        qa_hash = str(ledger["qa_hash"])
        source_hash = str(ledger["source_hash"])
        if len(request_hash) != 64 or len(qa_hash) != 64:
            return None
        if request_name != f"{request_hash}.json" or Path(request_name).name != request_name:
            return None
        if file_hash(paths.remix_qa) != qa_hash:
            return None
        qa = RemixQa.model_validate_json(paths.remix_qa.read_text(encoding="utf-8"))
        if qa.status != "pass" or qa.source_hash != source_hash:
            return None
        request_path = paths.accepted_repair_dir / request_name
        if file_hash(request_path) != request_hash:
            return None
        repairs = RemixRepairRequests.model_validate_json(request_path.read_text(encoding="utf-8"))
        if repairs.source_hash != source_hash or not repairs.items:
            return None
        if any(item.kind not in DURABLE_REPAIR_KINDS for item in repairs.items):
            return None
        return request_path
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def request_applies_to_stage(request_path: Path | None, stage: str) -> bool:
    if request_path is None or stage not in {"plan", "compose"}:
        return False
    try:
        repairs = RemixRepairRequests.model_validate_json(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    kind = "duration_restore" if stage == "plan" else "bed_leakage"
    return any(item.kind == kind for item in repairs.items)


def accepted_request_matches_source(request_path: Path | None, source_hash: str | None) -> bool:
    if request_path is None or source_hash is None:
        return False
    try:
        repairs = RemixRepairRequests.model_validate_json(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return repairs.source_hash == source_hash


def merge_active_with_accepted(
    active: RemixRepairRequests,
    accepted_request: Path | None,
) -> RemixRepairRequests:
    if accepted_request is None:
        return active
    accepted = RemixRepairRequests.model_validate_json(accepted_request.read_text(encoding="utf-8"))
    if accepted.source_hash != active.source_hash:
        raise ValueError("accepted and active repair requests have different sources")
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for item in [*accepted.items, *active.items]:
        key = (item.kind, item.requested_stage)
        entry = grouped.setdefault(
            key,
            {"affected_ids": [], "reasons": [], "attempt": 1},
        )
        affected_ids = entry["affected_ids"]
        reasons = entry["reasons"]
        assert isinstance(affected_ids, list)
        assert isinstance(reasons, list)
        for affected_id in item.affected_ids:
            if affected_id not in affected_ids:
                affected_ids.append(affected_id)
        if item.reason not in reasons:
            reasons.append(item.reason)
        entry["attempt"] = max(int(entry["attempt"]), item.attempt)
    payload = active.model_dump(mode="json")
    payload["items"] = [
        {
            "repair_id": f"repair-{index:04d}",
            "kind": kind,
            "affected_ids": values["affected_ids"],
            "reason": " | ".join(str(value) for value in values["reasons"]),
            "attempt": values["attempt"],
            "requested_stage": requested_stage,
        }
        for index, ((kind, requested_stage), values) in enumerate(sorted(grouped.items()))
    ]
    return RemixRepairRequests.model_validate(payload)


def persist_accepted_repairs(
    paths: ReactionRunPaths,
    *,
    qa_path: Path,
    current_repairs: list[RemixRepairRequests],
    previous_request: Path | None,
) -> Path | None:
    qa = RemixQa.model_validate_json(qa_path.read_text(encoding="utf-8"))
    if qa.status != "pass":
        raise ValueError("only a passing QA result can accept repair overrides")

    accepted_items = []
    request_created_at = None
    if previous_request is not None:
        previous = RemixRepairRequests.model_validate_json(previous_request.read_text(encoding="utf-8"))
        if previous.source_hash != qa.source_hash:
            raise ValueError("previous accepted repair source does not match passing QA")
        accepted_items.extend(item for item in previous.items if item.kind in DURABLE_REPAIR_KINDS)
        request_created_at = previous.created_at
    for current in current_repairs:
        if current.source_hash != qa.source_hash:
            raise ValueError("accepted repair source does not match passing QA")
        accepted_items.extend(item for item in current.items if item.kind in DURABLE_REPAIR_KINDS)
        if request_created_at is None:
            request_created_at = current.created_at
    if not accepted_items:
        return None

    merged: dict[tuple[str, str], dict[str, object]] = {}
    for item in accepted_items:
        key = (item.kind, item.requested_stage)
        entry = merged.setdefault(
            key,
            {
                "affected_ids": [],
                "reasons": [],
                "attempt": 1,
            },
        )
        affected_ids = entry["affected_ids"]
        reasons = entry["reasons"]
        assert isinstance(affected_ids, list)
        assert isinstance(reasons, list)
        for affected_id in item.affected_ids:
            if affected_id not in affected_ids:
                affected_ids.append(affected_id)
        if item.reason not in reasons:
            reasons.append(item.reason)
        entry["attempt"] = max(int(entry["attempt"]), item.attempt)

    payload = {
        "schema_version": "reaction-remix.v1",
        "source_hash": qa.source_hash,
        "items": [
            {
                "repair_id": f"accepted-{index:04d}",
                "kind": kind,
                "affected_ids": values["affected_ids"],
                "reason": " | ".join(str(value) for value in values["reasons"]),
                "attempt": values["attempt"],
                "requested_stage": requested_stage,
            }
            for index, ((kind, requested_stage), values) in enumerate(sorted(merged.items()))
        ],
        "created_at": request_created_at or datetime.now(timezone.utc),
        "warnings": ["Accepted only after a repaired run passed all QA hard gates."],
    }
    accepted = RemixRepairRequests.model_validate(payload)
    paths.accepted_repair_dir.mkdir(parents=True, exist_ok=True)
    candidate = paths.accepted_repair_dir / f".accepted-{os.getpid()}.json"
    atomic_write_json(candidate, accepted.model_dump(mode="json"))
    request_hash = file_hash(candidate)
    if request_hash is None:
        raise OSError("could not hash accepted repair request")
    request_path = paths.accepted_repair_dir / f"{request_hash}.json"
    if request_path.is_file():
        if file_hash(request_path) != request_hash:
            raise OSError("accepted repair content-addressed artifact is corrupt")
        candidate.unlink(missing_ok=True)
    else:
        candidate.replace(request_path)
    qa_hash = file_hash(qa_path)
    if qa_hash is None:
        raise OSError("could not hash accepted QA result")
    atomic_write_json(
        paths.accepted_repair_ledger,
        {
            "version": ACCEPTED_LEDGER_VERSION,
            "source_hash": qa.source_hash,
            "request_file": request_path.name,
            "request_hash": request_hash,
            "qa_hash": qa_hash,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return request_path
