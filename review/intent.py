from __future__ import annotations

import re

from common.schema import ReviewBeat, ReviewIntent, StorySection

ACTION_WORDS = (
    "rượt đuổi", "đánh nhau", "chiến đấu", "tấn công", "bỏ chạy", "chạy trốn", "nổ súng",
    "ẩu đả", "rượt", "hỗn chiến", "trả đũa", "dao gậy", "lao vào", "ập tới", "bắt quỳ",
    "tra hỏi", "đánh đập", "chống trả", "khống chế", "vật lộn", "đánh gục", "đánh ngã",
    "đập phá", "đập tan", "chặt", "bắn", "đâm", "giết", "bắt giữ", "phục kích", "chase",
    "fight", "attack", "escape", "shoot",
)
REVEAL_WORDS = (
    "phát hiện", "sự thật", "lộ ra", "nhận ra", "bằng chứng", "hé lộ",
    "reveal", "truth", "discover", "realize", "twist",
)
REACTION_WORDS = (
    "khóc", "hoảng", "sợ hãi", "tức giận", "bất ngờ", "đau buồn", "sững sờ",
    "cry", "shock", "fear", "angry", "panic", "sad", "surprise",
)
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
VI_QUERY_BY_INTENT = {
    "action": "cảnh hành động và xung đột",
    "reaction": "cận cảnh phản ứng cảm xúc",
    "reveal": "cảnh phát hiện bí mật hoặc bằng chứng",
    "character_intro": "nhân vật xuất hiện rõ khuôn mặt",
    "location": "toàn cảnh địa điểm",
    "ending": "cảnh kết thúc và hậu quả",
    "dialogue": "nhân vật trò chuyện trực diện",
}
EN_QUERY_BY_INTENT = {
    "action": "visible action and physical conflict",
    "reaction": "close-up emotional reaction",
    "reveal": "discovery of a secret or evidence",
    "character_intro": "character entrance with a visible face",
    "location": "establishing view of the location",
    "ending": "aftermath and story resolution",
    "dialogue": "people talking face to face",
}
MAX_QUERY_WORDS = 24


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


def contains_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, flags=re.IGNORECASE) for phrase in phrases)


def contains_exact_case_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def infer_visual_intent(beat: ReviewBeat, section: StorySection | None) -> str:
    text = beat.narration.lower()
    if section and section.type == "setup" and beat.is_hook:
        return "character_intro"
    if contains_phrase(text, ACTION_WORDS):
        return "action"
    if contains_phrase(text, REVEAL_WORDS):
        return "reveal"
    if contains_phrase(text, REACTION_WORDS):
        return "reaction"
    if section and section.type == "setup" and any(
        contains_phrase(beat.narration, (location,)) for location in section.locations
    ):
        return "location"
    if section and section.type == "ending":
        return "ending"
    return "dialogue"


def compact_words(parts: list[str], *, limit: int = MAX_QUERY_WORDS) -> str:
    words: list[str] = []
    for part in parts:
        for word in " ".join(str(part).split()).split(" "):
            if word:
                words.append(word)
            if len(words) >= limit:
                return " ".join(words)
    return " ".join(words)


def narration_visual_clause(text: str, *, max_words: int = 12) -> str:
    first_sentence = re.split(r"[.!?;]", text, maxsplit=1)[0]
    return compact_words([first_sentence], limit=max_words)


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
        character_names = [
            name
            for name in (section.characters if section else [])
            if len(name.split()) >= 2 and contains_exact_case_phrase(beat.narration, name)
        ][:2]
        mentioned_locations = [name for name in (section.locations if section else []) if contains_phrase(beat.narration, (name,))]
        locations = mentioned_locations[:1]
        visual_clause = narration_visual_clause(beat.narration)
        visual_query_vi = compact_words(character_names + locations + [VI_QUERY_BY_INTENT[visual_intent], visual_clause])
        visual_query_en = compact_words(character_names + locations + [EN_QUERY_BY_INTENT[visual_intent]])
        intents.append(
            ReviewIntent(
                beat_id=beat.beat_id,
                story_section_id=section.section_id if section else None,
                story_section_type=section.type if section else None,
                visual_intent=visual_intent,
                chronology_mode=chronology_mode,
                visual_query_vi=visual_query_vi,
                visual_query_en=visual_query_en,
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
