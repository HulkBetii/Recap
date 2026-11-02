from __future__ import annotations

import asyncio
import json

from common.schema import FilmMapSegment, ReviewBeat, validate_sentence_refs
from review.sentence_refs import request_sentence_refs, resolve_sentence_refs


def film_map() -> list[FilmMapSegment]:
    return [
        FilmMapSegment(id=index, type="speech", tc_start=index * 10.0, tc_end=index * 10.0 + 8.0, ko="k", en=f"line {index}")
        for index in range(10)
    ]


def beats() -> list[ReviewBeat]:
    return [
        ReviewBeat(beat_id=0, narration="Anh đến nhà. Cô ấy bỏ chạy! Thật đáng sợ.", from_seg_id=0, to_seg_id=4, src_tc_start=0.0, src_tc_end=48.0),
        ReviewBeat(beat_id=1, narration="Cảnh sát tới.", from_seg_id=5, to_seg_id=9, src_tc_start=50.0, src_tc_end=98.0),
    ]


def test_resolve_clamps_to_beat_span_and_derives_timecodes() -> None:
    refs, warnings = resolve_sentence_refs(beats(), film_map(), {0: {0: [1, 2], 1: [3, 7], 2: None}, 1: {0: 8}})
    validate_sentence_refs(refs, beats())
    assert [(ref.beat_id, ref.idx, ref.from_seg_id, ref.to_seg_id, ref.grounded) for ref in refs] == [
        (0, 0, 1, 2, True),
        (0, 1, 3, 4, True),
        (0, 2, None, None, False),
        (1, 0, 8, 8, True),
    ]
    assert refs[0].src_tc_start == 10.0 and refs[0].src_tc_end == 28.0
    assert warnings == []


def test_resolve_drops_refs_entirely_outside_the_span() -> None:
    refs, warnings = resolve_sentence_refs(beats(), film_map(), {0: {0: [6, 7]}})
    assert refs[0].grounded is False and "outside the beat span" in warnings[0]


def test_request_marks_failed_batches_ungrounded_and_incomplete() -> None:
    class Client:
        async def ask(self, prompt: str) -> str:
            assert "Link every narration sentence" in prompt and "#0 line 0" in prompt
            return "not json"

    refs, warnings, complete = asyncio.run(request_sentence_refs(Client(), beats=beats(), film_map=film_map()))
    assert complete is False and all(not ref.grounded for ref in refs) and len(refs) == 4
    assert any("failed" in warning for warning in warnings)


def test_request_parses_answers() -> None:
    class Client:
        async def ask(self, prompt: str) -> str:
            return json.dumps([
                {"beat_id": 0, "sentences": [{"idx": 0, "seg_ids": [0, 1]}, {"idx": 1, "seg_ids": [2, 2]}, {"idx": 2, "seg_ids": None}]},
                {"beat_id": 1, "sentences": [{"idx": 0, "seg_ids": [5, 6]}]},
            ])

    refs, warnings, complete = asyncio.run(request_sentence_refs(Client(), beats=beats(), film_map=film_map()))
    assert complete and warnings == []
    assert [ref.grounded for ref in refs] == [True, True, False, True]
