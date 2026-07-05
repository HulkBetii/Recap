from __future__ import annotations

from common.schema import ReviewBeat, StorySection
from review.intent import build_review_intents


def test_review_intent_ids_match_beats_and_setup_is_ordered() -> None:
    beats = [ReviewBeat(beat_id=0, narration="Alice arrives at a strange house.", from_seg_id=0, to_seg_id=0, src_tc_start=10, src_tc_end=20, is_hook=True)]
    sections = [StorySection(section_id=0, type="setup", tc_start=10, tc_end=30, segment_ids=[0], summary="setup", characters=["Alice"], locations=[], events=[], confidence=0.8)]
    intents = build_review_intents(beats, sections)
    assert [intent.beat_id for intent in intents] == [0]
    assert intents[0].story_section_id == 0
    assert intents[0].visual_intent == "character_intro"
    assert intents[0].chronology_mode == "ordered"
