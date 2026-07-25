from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from recap_ui.schemas import FilesystemEntry, FilesystemListing, FilesystemLocation, FilesystemRoot


class PathAccessError(ValueError):
    pass


def load_or_create_secret(path: Path, *, length: int = 32) -> bytes:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        value = path.read_bytes()
        if len(value) < 16:
            raise RuntimeError(f"security token is invalid: {path}")
        return value
    value = secrets.token_bytes(length)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)
    return value


def startup_token(state_dir: Path) -> str:
    raw = load_or_create_secret(state_dir / "startup_token")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def token_matches(expected: str, provided: str | None) -> bool:
    return bool(provided) and hmac.compare_digest(expected, provided)


def same_origin_allowed(*, origin: str | None, host: str, allowed_host: str) -> bool:
    if host.casefold() != allowed_host.casefold():
        return False
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == allowed_host.casefold()


class PathRegistry:
    def __init__(self, roots: dict[str, tuple[str, Path] | Path], secret: bytes) -> None:
        if not roots:
            raise ValueError("at least one filesystem root is required")
        self._secret = secret
        normalized: dict[str, tuple[str, Path]] = {}
        for root_id, value in roots.items():
            label, path = value if isinstance(value, tuple) else (root_id, value)
            if not root_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in root_id.casefold()):
                raise ValueError(f"invalid root id: {root_id}")
            normalized[root_id] = (label, path.expanduser().resolve())
        self._roots = normalized

    @classmethod
    def default(cls, repo_root: Path, state_dir: Path) -> PathRegistry:
        repo = repo_root.expanduser().resolve()
        home = Path.home().resolve()
        roots: dict[str, tuple[str, Path]] = {
            "repo": ("Recap repository", repo),
            "runs": ("Recap runs", repo / "runs"),
        }
        downloads = home / "Downloads"
        videos = home / "Videos"
        if downloads.exists():
            roots["downloads"] = ("Downloads", downloads)
        if videos.exists():
            roots["videos"] = ("Videos", videos)
        return cls(roots, load_or_create_secret(state_dir / "path_token_key"))

    def roots(self) -> list[FilesystemRoot]:
        return [
            FilesystemRoot(id=root_id, label=label, token=self.token_for(path))
            for root_id, (label, path) in self._roots.items()
        ]

    def token_for(self, path: Path | str) -> str:
        resolved = Path(path).expanduser().resolve()
        candidates: list[tuple[int, str, Path]] = []
        for root_id, (_, root) in self._roots.items():
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            candidates.append((len(root.parts), root_id, relative))
        if not candidates:
            raise PathAccessError("path is outside configured filesystem roots")
        _, root_id, relative = max(candidates)
        payload = json.dumps(
            {"root": root_id, "path": relative.as_posix()},
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def resolve(
        self,
        token: str,
        expect: Literal["file", "directory"] | None = None,
    ) -> Path:
        try:
            encoded, signature = token.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            expected_signature = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_signature, signature):
                raise PathAccessError("invalid path token signature")
            decoded = json.loads(payload)
            root_id = str(decoded["root"])
            relative_text = str(decoded["path"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc, PathAccessError):
                raise
            raise PathAccessError("invalid path token") from exc
        if root_id not in self._roots:
            raise PathAccessError("path token references an unknown root")
        root = self._roots[root_id][1]
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise PathAccessError("path token contains an unsafe relative path")
        resolved = (root / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PathAccessError("path escapes configured filesystem root") from exc
        if expect == "file" and not resolved.is_file():
            raise PathAccessError("path is not a file")
        if expect == "directory" and not resolved.is_dir():
            raise PathAccessError("path is not a directory")
        return resolved

    def list_entries(
        self,
        token: str | None = None,
        *,
        root_id: str | None = None,
        limit: int = 500,
    ) -> list[FilesystemEntry]:
        return self.listing(token, root_id=root_id, limit=limit).entries

    def listing(
        self,
        token: str | None = None,
        *,
        root_id: str | None = None,
        limit: int = 500,
    ) -> FilesystemListing:
        if token is not None:
            directory = self.resolve(token, expect="directory")
        elif root_id is not None and root_id in self._roots:
            directory = self._roots[root_id][1]
        else:
            raise PathAccessError("directory token or root id is required")
        current_root_id, root_label, root = self._root_for(directory)
        entries: list[FilesystemEntry] = []
        try:
            children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        except OSError as exc:
            raise PathAccessError(f"cannot list directory: {exc}") from exc
        for child in children[: max(1, min(limit, 2000))]:
            try:
                resolved = child.resolve()
                self.token_for(resolved)
                stat = resolved.stat()
            except (OSError, PathAccessError):
                continue
            entries.append(
                FilesystemEntry(
                    token=self.token_for(resolved),
                    name=child.name,
                    kind="directory" if resolved.is_dir() else "file",
                    suffix=child.suffix.lower(),
                    size=None if resolved.is_dir() else stat.st_size,
                    modified_at=stat.st_mtime,
                )
            )
        at_root = directory == root
        return FilesystemListing(
            current=FilesystemLocation(
                root_id=current_root_id,
                token=self.token_for(directory),
                name=root_label if at_root else directory.name,
                parent_token=None if at_root else self.token_for(directory.parent),
                at_root=at_root,
            ),
            entries=entries,
        )

    def _root_for(self, path: Path) -> tuple[str, str, Path]:
        candidates: list[tuple[int, str, str, Path]] = []
        for root_id, (label, root) in self._roots.items():
            try:
                path.relative_to(root)
            except ValueError:
                continue
            candidates.append((len(root.parts), root_id, label, root))
        if not candidates:
            raise PathAccessError("path is outside configured filesystem roots")
        _, root_id, label, root = max(candidates)
        return root_id, label, root
