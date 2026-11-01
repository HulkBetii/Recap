"""CapCut TTS Engine for Recap stage 3.

Ported from the user's trans-zh project (src/zhsub/dub/capcut/__init__.py).
"""

from __future__ import annotations

from .client import DEFAULT_VOICE, CapCutClient, CapCutVoice, load_voice_catalog
from .exceptions import CapCutAPIError, CapCutError, CapCutSignError, CapCutTaskError

__all__ = [
    "CapCutClient",
    "CapCutVoice",
    "CapCutError",
    "CapCutAPIError",
    "CapCutSignError",
    "CapCutTaskError",
    "DEFAULT_VOICE",
    "load_voice_catalog",
]
