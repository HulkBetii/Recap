from __future__ import annotations

from common.schema import ReactionBlockKind, ReactionTurn


def classify_turn(
    turn: ReactionTurn,
    narrator_speaker_id: str | None,
    *,
    commentary_min_confidence: float,
) -> ReactionBlockKind:
    if narrator_speaker_id is not None and turn.speaker_id == narrator_speaker_id:
        if (
            turn.language == "ja"
            and turn.language_confidence >= commentary_min_confidence
            and turn.speaker_confidence >= commentary_min_confidence
        ):
            return "commentary"
        return "mixed"
    if (
        turn.language == "und"
        or turn.speaker_id == "speaker-unknown"
        or turn.language_confidence < 0.55
        or turn.speaker_confidence < 0.55
        or turn.asr_confidence < 0.55
    ):
        return "unknown"
    return "reaction"
