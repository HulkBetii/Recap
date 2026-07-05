from __future__ import annotations

from common.schema import ReviewBeat, ReviewIntent, StorySection

ACTION_WORDS = ("chase", "fight", "run", "attack", "kill", "escape", "shoot", "hit", "battle")
REVEAL_WORDS = ("reveal", "truth", "secret", "discover", "realize", "twist", "found")
REACTION_WORDS = ("cry", "shock", "fear", "angry", "panic", "sad", "surprise")


def find_section(beat: ReviewBeat, sections: list[StorySection]) -> StorySection | None:
    best: tuple[float, StorySection] | None = None
    for section in sections:
        if section.type == "non_story":
            continue
        overlap = max(0.0, min(beat.src_tc_end, section.tc_end) - max(beat.src_tc_start, section.tc_start))
        if overlap <= 0:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, section)
    return best[1] if best else None


def infer_visual_intent(beat: ReviewBeat, section: StorySection | None) -> str:
    text = beat.narration.lower()
    if section and section.type == "setup":
        return "character_intro" if beat.is_hook else "location"
    if any(word in text for word in ACTION_WORDS):
        return "action"
    if any(word in text for word in REVEAL_WORDS) or (section and section.type == "reveal"):
        return "reveal"
    if any(word in text for word in REACTION_WORDS):
        return "reaction"
    if section and section.type == "ending":
        return "ending"
    return "dialogue"


def build_review_intents(beats: list[ReviewBeat], sections: list[StorySection]) -> list[ReviewIntent]:
    intents: list[ReviewIntent] = []
    for beat in beats:
        section = find_section(beat, sections)
        warnings: list[str] = []
        if section is None:
            warnings.append("no overlapping story section")
        chronology_mode = "ordered" if beat.is_hook or beat.src_tc_start < 300 else "flexible"
        intents.append(
            ReviewIntent(
                beat_id=beat.beat_id,
                story_section_id=section.section_id if section else None,
                story_section_type=section.type if section else None,
                visual_intent=infer_visual_intent(beat, section),
                chronology_mode=chronology_mode,
                warnings=warnings,
            )
        )
    return intents


def story_map_prompt_context(sections: list[StorySection]) -> str:
    rows = []
    for section in sections:
        if section.type == "non_story":
            rows.append(f"- section {section.section_id}: NON_STORY {section.tc_start:.1f}-{section.tc_end:.1f}s {section.summary}")
        else:
            rows.append(f"- section {section.section_id}: {section.type} {section.tc_start:.1f}-{section.tc_end:.1f}s {section.summary}")
    return "\nSTORY_MAP (follow this order for movie mode):\n" + "\n".join(rows)
