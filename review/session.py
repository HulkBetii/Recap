from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChatSessionPolicy = Literal["auto", "new", "resume"]
CHATGPT_HOME_URL = "https://chatgpt.com/"


class ChatSessionMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: ChatSessionPolicy
    chat_url: str
    profile_dir: str
    film_map_path: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    warnings: list[str] = Field(default_factory=list)


def load_chat_session(path: Path) -> ChatSessionMeta | None:
    if not path.is_file():
        return None
    return ChatSessionMeta.model_validate_json(path.read_text(encoding="utf-8"))


def save_chat_session(path: Path, meta: ChatSessionMeta) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")


def resolve_initial_chat_url(path: Path, policy: ChatSessionPolicy) -> tuple[str, ChatSessionMeta | None, list[str]]:
    existing = load_chat_session(path)
    warnings: list[str] = []
    if policy == "new":
        return CHATGPT_HOME_URL, existing, warnings
    if existing is not None and existing.chat_url:
        return existing.chat_url, existing, warnings
    if policy == "resume":
        warnings.append(f"chat session meta not found for resume: {path}; starting a new chat")
    return CHATGPT_HOME_URL, existing, warnings


def build_chat_session_meta(
    *,
    policy: ChatSessionPolicy,
    chat_url: str,
    profile_dir: Path,
    film_map_path: Path,
    title: str | None,
    previous: ChatSessionMeta | None,
    warnings: list[str] | None = None,
) -> ChatSessionMeta:
    now = datetime.now(timezone.utc)
    return ChatSessionMeta(
        policy=policy,
        chat_url=chat_url,
        profile_dir=str(profile_dir),
        film_map_path=str(film_map_path),
        title=title,
        created_at=previous.created_at if previous else now,
        updated_at=now,
        warnings=warnings or [],
    )
