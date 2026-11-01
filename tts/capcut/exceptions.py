"""Exceptions for CapCut TTS API.

Ported from the user's trans-zh project (src/zhsub/dub/capcut/exceptions.py); Recap adds `transient`.
"""

from __future__ import annotations

from typing import Any


class CapCutError(Exception):
    """Base exception for all CapCut client errors.

    `transient=True` marks conditions worth retrying later (server busy, rate limit, 5xx, network).
    """

    def __init__(self, message: str = "", *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class CapCutSignError(CapCutError):
    """Raised when cryptography or signing fails."""


class CapCutAPIError(CapCutError):
    """Raised when the API returns an error response or non-200 HTTP code."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_data: dict[str, Any] | None = None,
        *,
        transient: bool = False,
    ) -> None:
        super().__init__(message, transient=transient)
        self.status_code = status_code
        self.response_data = response_data or {}


class CapCutTaskError(CapCutError):
    """Raised when a background TTS task fails or times out."""
