from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from common.integrity import atomic_write_json

SessionPolicy = Literal["auto", "new", "resume"]
CHATGPT_HOME_URL = "https://chatgpt.com/"


class ReactionChatSessionMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: SessionPolicy
    chat_url: str
    profile_dir: str
    source_hash: str
    blocks_hash: str
    plan_hash: str | None = None
    content_hash: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    warnings: list[str] = Field(default_factory=list)


def load_session(path: Path) -> ReactionChatSessionMeta | None:
    if not path.is_file():
        return None
    return ReactionChatSessionMeta.model_validate_json(path.read_text(encoding="utf-8"))


def resolve_session(
    path: Path,
    policy: SessionPolicy,
    *,
    source_hash: str,
    blocks_hash: str,
    content_hash: str,
    plan_hash: str | None = None,
) -> tuple[str, ReactionChatSessionMeta | None, list[str]]:
    previous = load_session(path)
    warnings: list[str] = []
    if policy == "new":
        return CHATGPT_HOME_URL, previous, warnings
    content_matches = bool(
        previous
        and (
            previous.content_hash == content_hash
            or (plan_hash is not None and previous.plan_hash == plan_hash)
        )
    )
    matches = bool(
        previous
        and previous.source_hash == source_hash
        and previous.blocks_hash == blocks_hash
        and content_matches
        and (plan_hash is None or previous.plan_hash == plan_hash)
    )
    if previous and not matches:
        if policy == "auto":
            warnings.append("reaction editorial input changed; starting a new ChatGPT conversation")
            return CHATGPT_HOME_URL, previous, warnings
        warnings.append("reaction editorial input changed but session policy is resume")
    if previous and previous.chat_url:
        return previous.chat_url, previous, warnings
    if policy == "resume":
        warnings.append("reaction ChatGPT session metadata is missing; starting a new conversation")
    return CHATGPT_HOME_URL, previous, warnings


def save_session(
    path: Path,
    *,
    policy: SessionPolicy,
    chat_url: str,
    profile_dir: Path,
    source_hash: str,
    blocks_hash: str,
    content_hash: str,
    plan_hash: str | None,
    title: str | None,
    previous: ReactionChatSessionMeta | None,
    warnings: list[str],
) -> ReactionChatSessionMeta:
    now = datetime.now(timezone.utc)
    meta = ReactionChatSessionMeta(
        policy=policy,
        chat_url=chat_url,
        profile_dir=str(profile_dir),
        source_hash=source_hash,
        blocks_hash=blocks_hash,
        plan_hash=plan_hash,
        content_hash=content_hash,
        title=title,
        created_at=previous.created_at if previous else now,
        updated_at=now,
        warnings=warnings,
    )
    atomic_write_json(path, meta.model_dump(mode="json"))
    return meta
