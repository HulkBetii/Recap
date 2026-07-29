from __future__ import annotations

import re


_SEASON_EPISODE_RE = re.compile(r"\bs\d{1,3}[\s._-]*e(?P<episode>\d{1,4})\b", re.IGNORECASE)
_CROSS_EPISODE_RE = re.compile(r"\b\d{1,3}x(?P<episode>\d{1,4})\b", re.IGNORECASE)
_LABELED_EPISODE_RE = re.compile(
    r"\b(?:episode|ep|e)[\s._-]*(?P<episode>\d{1,4})\b",
    re.IGNORECASE,
)


def numeric_episode(value: int | str | None) -> int | None:
    """Extract an episode number without mistaking a season prefix for it."""

    if value is None:
        return None
    if isinstance(value, int):
        return value

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    for pattern in (_SEASON_EPISODE_RE, _CROSS_EPISODE_RE, _LABELED_EPISODE_RE):
        match = pattern.search(text)
        if match:
            return int(match.group("episode"))

    numeric_groups = re.findall(r"\d+", text)
    return int(numeric_groups[0]) if len(numeric_groups) == 1 else None
