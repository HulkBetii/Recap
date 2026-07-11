from __future__ import annotations

from common.schema import ReviewBeat, StorySection
from review.intent import build_review_intents, infer_visual_intent


def test_review_intent_ids_match_beats_and_setup_is_ordered() -> None:
    beats = [ReviewBeat(beat_id=0, narration="Alice arrives at a strange house.", from_seg_id=0, to_seg_id=0, src_tc_start=10, src_tc_end=20, is_hook=True)]
    sections = [StorySection(section_id=0, type="setup", tc_start=10, tc_end=30, segment_ids=[0], summary="setup", characters=["Alice"], locations=[], events=[], confidence=0.8)]
    intents = build_review_intents(beats, sections)
    assert [intent.beat_id for intent in intents] == [0]
    assert intents[0].story_section_id == 0
    assert intents[0].visual_intent == "character_intro"
    assert intents[0].chronology_mode == "ordered"


def test_vietnamese_trung_quoc_does_not_trigger_run_action_substring() -> None:
    beat = ReviewBeat(beat_id=0, narration="Một nhóm người từ Trung Quốc tới khu phố.", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=2, is_hook=False)
    assert infer_visual_intent(beat, None) == "dialogue"


def test_review_visual_queries_are_compact_and_bilingual() -> None:
    narration = "Thanh tra phát hiện bí mật trong căn phòng rồi lập tức gọi đồng đội tới kiểm tra toàn bộ hiện trường."
    beat = ReviewBeat(beat_id=0, narration=narration, from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=2, is_hook=False)
    section = StorySection(section_id=0, type="reveal", tc_start=0, tc_end=2, segment_ids=[0], summary="reveal", characters=["Thanh tra Ma"], locations=["căn phòng"], events=[narration], confidence=0.9)
    intent = build_review_intents([beat], [section])[0]
    assert intent.visual_query_vi is not None and len(intent.visual_query_vi.split()) <= 24
    assert intent.visual_query_en is not None and len(intent.visual_query_en.split()) <= 24


def test_explicit_action_overrides_setup_location_default() -> None:
    beat = ReviewBeat(
        beat_id=0,
        narration="Một vụ ẩu đả nổ ra, Thanh tra Ma lao vào rượt nghi phạm.",
        from_seg_id=0,
        to_seg_id=0,
        src_tc_start=10,
        src_tc_end=20,
        is_hook=False,
    )
    section = StorySection(
        section_id=0,
        type="setup",
        tc_start=0,
        tc_end=30,
        segment_ids=[0],
        summary="setup",
        characters=["Tên không liên quan", "Thanh tra Ma"],
        locations=["Garibong"],
        events=[],
        confidence=1.0,
    )
    intent = build_review_intents([beat], [section])[0]
    assert intent.visual_intent == "action"
    assert intent.visual_query_vi.startswith("Thanh tra Ma")
    assert "Tên không liên quan" not in intent.visual_query_vi


def test_reveal_story_section_does_not_override_plain_dialogue() -> None:
    beat = ReviewBeat(
        beat_id=0,
        narration="Hai vợ chồng nói chuyện trong xe về khoản nợ và kế hoạch làm ăn.",
        from_seg_id=0,
        to_seg_id=0,
        src_tc_start=10,
        src_tc_end=20,
        is_hook=False,
    )
    section = StorySection(
        section_id=0,
        type="reveal",
        tc_start=0,
        tc_end=30,
        segment_ids=[0],
        summary="The investment creates tension between them.",
        characters=[],
        locations=[],
        events=[],
        confidence=1.0,
    )

    intent = build_review_intents([beat], [section])[0]

    assert intent.visual_intent == "dialogue"
    assert intent.visual_query_en is not None
    assert intent.visual_query_en.startswith("people talking face to face")


def test_secret_used_as_sales_pitch_does_not_imply_visible_reveal() -> None:
    beat = ReviewBeat(
        beat_id=0,
        narration="Hai người nói về lợi nhuận và một bí mật thị trường trong lúc lái xe về nhà.",
        from_seg_id=0,
        to_seg_id=0,
        src_tc_start=10,
        src_tc_end=20,
        is_hook=False,
    )

    assert infer_visual_intent(beat, None) == "dialogue"


def test_setup_section_uses_dialogue_when_location_is_not_mentioned() -> None:
    beat = ReviewBeat(
        beat_id=0,
        narration="Hai vợ chồng tranh luận về khoản nợ trong lúc lái xe về nhà.",
        from_seg_id=0,
        to_seg_id=0,
        src_tc_start=10,
        src_tc_end=20,
        is_hook=False,
    )
    section = StorySection(
        section_id=0,
        type="setup",
        tc_start=0,
        tc_end=30,
        segment_ids=[0],
        summary="The couple argues after work.",
        characters=[],
        locations=["chợ cá"],
        events=[],
        confidence=1.0,
    )

    assert infer_visual_intent(beat, section) == "dialogue"


def test_explicit_fight_overrides_ending_default() -> None:
    beat = ReviewBeat(
        beat_id=0,
        narration="Tên trùm chống trả dữ dội nhưng cuối cùng bị thanh tra khống chế.",
        from_seg_id=0,
        to_seg_id=0,
        src_tc_start=10,
        src_tc_end=20,
        is_hook=False,
    )
    section = StorySection(
        section_id=0,
        type="ending",
        tc_start=0,
        tc_end=30,
        segment_ids=[0],
        summary="The villain is defeated.",
        characters=[],
        locations=[],
        events=[],
        confidence=1.0,
    )

    assert infer_visual_intent(beat, section) == "action"
