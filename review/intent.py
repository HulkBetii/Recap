from __future__ import annotations

from common.schema import ReviewBeat, ReviewIntent, StorySection

ACTION_WORDS = ("chase", "fight", "run", "attack", "kill", "escape", "shoot", "hit", "battle")
REVEAL_WORDS = ("reveal", "truth", "secret", "discover", "realize", "twist", "found")
REACTION_WORDS = ("cry", "shock", "fear", "angry", "panic", "sad", "surprise")
ACTION_CUES = {
    "action": ["movement", "conflict", "physical action"],
    "reaction": ["reaction", "close-up", "emotion"],
    "reveal": ["realization", "evidence", "tense reaction"],
    "character_intro": ["person", "face", "entrance"],
    "location": ["establishing shot", "place"],
    "ending": ["resolution", "aftermath"],
    "dialogue": ["conversation", "faces"],
}
ABSTRACTION_BY_INTENT = {
    "reveal": "mental_state_reveal",
    "reaction": "emotion_reaction",
    "dialogue": "dialogue_context",
    "character_intro": "character_presence",
    "location": "setting_context",
    "action": "visible_action",
    "ending": "story_resolution",
}


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
        visual_intent = infer_visual_intent(beat, section)
        action_cues = list(ACTION_CUES.get(visual_intent, []))
        if section:
            action_cues.extend(section.events[:3])
        visual_query_parts = [beat.narration]
        if section:
            visual_query_parts.extend(section.characters[:3])
            visual_query_parts.extend(section.locations[:2])
            visual_query_parts.extend(section.events[:3])
        intents.append(
            ReviewIntent(
                beat_id=beat.beat_id,
                story_section_id=section.section_id if section else None,
                story_section_type=section.type if section else None,
                visual_intent=visual_intent,
                chronology_mode=chronology_mode,
                visual_query_vi="; ".join(part for part in visual_query_parts if part),
                abstraction_class=ABSTRACTION_BY_INTENT.get(visual_intent, "general_story"),
                visual_explicitness=0.7 if visual_intent == "action" else 0.45 if visual_intent in {"reveal", "reaction"} else 0.55,
                characters=[{"name": name, "entity_type": "person", "salience": 0.8} for name in (section.characters[:5] if section else [])],
                action_cues=dedupe(action_cues),
                emotion_cues=[cue for cue in ACTION_CUES.get(visual_intent, []) if cue in {"emotion", "tense reaction", "reaction"}],
                location_cues=section.locations[:5] if section else [],
                object_cues=[],
                negative_visual_cues=["title card", "credits", "logo"],
                preferred_shot_traits=["close_up"] if visual_intent in {"reaction", "reveal", "character_intro"} else [],
                warnings=warnings,
            )
        )
    return intents

def dedupe(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def story_map_prompt_context(sections: list[StorySection]) -> str:
    rows = []
    for section in sections:
        if section.type == "non_story":
            rows.append(f"- section {section.section_id}: NON_STORY {section.tc_start:.1f}-{section.tc_end:.1f}s {section.summary}")
        else:
            rows.append(f"- section {section.section_id}: {section.type} {section.tc_start:.1f}-{section.tc_end:.1f}s {section.summary}")
    return "\nSTORY_MAP (follow this order for movie mode):\n" + "\n".join(rows)
