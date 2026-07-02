from __future__ import annotations

from pathlib import Path

from review.session import (
    CHATGPT_HOME_URL,
    ChatSessionMeta,
    build_chat_session_meta,
    resolve_initial_chat_url,
    save_chat_session,
)


def test_resolve_initial_chat_url_starts_new_when_no_meta(tmp_path: Path) -> None:
    url, previous, warnings = resolve_initial_chat_url(tmp_path / "chat_session_meta.json", "auto")

    assert url == CHATGPT_HOME_URL
    assert previous is None
    assert warnings == []


def test_resolve_initial_chat_url_resumes_existing_session(tmp_path: Path) -> None:
    path = tmp_path / "chat_session_meta.json"
    meta = build_chat_session_meta(
        policy="auto",
        chat_url="https://chatgpt.com/c/abc",
        profile_dir=tmp_path / "profile",
        film_map_path=tmp_path / "film_map.json",
        title="ep01",
        previous=None,
    )
    save_chat_session(path, meta)

    url, previous, warnings = resolve_initial_chat_url(path, "resume")

    assert url == "https://chatgpt.com/c/abc"
    assert isinstance(previous, ChatSessionMeta)
    assert warnings == []


def test_resolve_initial_chat_url_new_ignores_existing_session(tmp_path: Path) -> None:
    path = tmp_path / "chat_session_meta.json"
    save_chat_session(path, build_chat_session_meta(
        policy="auto",
        chat_url="https://chatgpt.com/c/old",
        profile_dir=tmp_path / "profile",
        film_map_path=tmp_path / "film_map.json",
        title=None,
        previous=None,
    ))

    url, previous, warnings = resolve_initial_chat_url(path, "new")

    assert url == CHATGPT_HOME_URL
    assert previous is not None
    assert warnings == []


def test_build_chat_session_meta_preserves_created_at_on_update(tmp_path: Path) -> None:
    first = build_chat_session_meta(
        policy="auto",
        chat_url="https://chatgpt.com/c/old",
        profile_dir=tmp_path / "profile",
        film_map_path=tmp_path / "film_map.json",
        title="old",
        previous=None,
    )
    updated = build_chat_session_meta(
        policy="resume",
        chat_url="https://chatgpt.com/c/new",
        profile_dir=tmp_path / "profile",
        film_map_path=tmp_path / "film_map.json",
        title="new",
        previous=first,
        warnings=["warn"],
    )

    assert updated.created_at == first.created_at
    assert updated.updated_at >= first.updated_at
    assert updated.warnings == ["warn"]
