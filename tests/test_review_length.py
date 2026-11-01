from __future__ import annotations

import asyncio
import json
import logging

import pytest

from common.schema import FilmMapSegment, ReviewBeat
from review.__main__ import ensure_narration_length, qa_rewrite_cap
from review.cache import ReviewCache
from review.llm_flow import MAX_CHARS_PER_BEAT, build_outline_prompt, min_outline_beats
from review.models import NarrationBeat, OutlineBeat

LOGGER = logging.getLogger("test")


def test_outline_prompt_scales_minimum_beats_with_budget() -> None:
    assert min_outline_beats(3373) == 5
    assert min_outline_beats(39000) == 56
    prompt = build_outline_prompt(film_map_view="[]", target_video_s=1900, char_budget=39000, min_coverage=0.85)
    assert f"at least 56 beats" in prompt
    assert f"about {MAX_CHARS_PER_BEAT} Vietnamese characters" in prompt


def test_qa_rewrite_cap_scales_with_beats() -> None:
    assert qa_rewrite_cap(6, 9) == 6
    assert qa_rewrite_cap(6, 45) == 15
    assert qa_rewrite_cap(0, 45) == 0


class ExpandClient:
    def __init__(self, *, fail_beat: int | None = None) -> None:
        self.asked: list[int] = []
        self.fail_beat = fail_beat

    async def ask(self, prompt: str) -> str:
        beat_id = json.loads(prompt.split("CURRENT_BEAT:\n", 1)[1].split("\n", 1)[0])["beat_id"]
        self.asked.append(beat_id)
        if beat_id == self.fail_beat:
            return "not json"
        return json.dumps({"beat_id": beat_id, "narration": "Câu mở rộng có nhân vật và nguyên nhân rõ ràng. " * 8}, ensure_ascii=False)


def fixtures() -> tuple[list[OutlineBeat], list[FilmMapSegment], list[NarrationBeat], list[ReviewBeat]]:
    outline = [OutlineBeat(from_seg_id=index, to_seg_id=index, summary=f"beat {index}", is_hook=index == 0) for index in range(3)]
    film_map = [
        FilmMapSegment(id=index, type="speech", tc_start=float(index), tc_end=float(index) + 1, ko="a", en="b") for index in range(3)
    ]
    narration = [
        NarrationBeat(beat_id=0, narration="Ngắn."),
        NarrationBeat(beat_id=1, narration="Đủ dài. " * 20),
        NarrationBeat(beat_id=2, narration="Cũng ngắn."),
    ]
    beats = [
        ReviewBeat(
            beat_id=item.beat_id,
            narration=item.narration,
            from_seg_id=item.beat_id,
            to_seg_id=item.beat_id,
            src_tc_start=float(item.beat_id),
            src_tc_end=float(item.beat_id) + 1,
            is_hook=item.beat_id == 0,
        )
        for item in narration
    ]
    return outline, film_map, narration, beats


def test_length_pass_expands_only_short_beats_and_survives_bad_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    outline, film_map, narration, beats = fixtures()
    cache = ReviewCache(tmp_path)
    cache.prepare()
    client = ExpandClient(fail_beat=2)

    result, _beats, expanded, warnings = asyncio.run(
        ensure_narration_length(
            cache=cache, client=client, narration=narration, beats=beats, outline=outline, film_map=film_map,
            glossary=[], char_targets=[200, 150, 200], style_guide="", logger=LOGGER,
            anime_context=None, episode_memory=None, content_type="movie",
        )
    )

    assert client.asked == [0, 2]
    assert expanded == 1
    assert len(result[0].narration) > 200 * 0.75
    assert result[2].narration == "Cũng ngắn."
    assert any("length expansion skipped for beat 2" in warning for warning in warnings)
    assert cache.path("narration_length_checked.json").is_file()

    rerun = ExpandClient()
    cached, _beats, cached_expanded, _warnings = asyncio.run(
        ensure_narration_length(
            cache=cache, client=rerun, narration=narration, beats=beats, outline=outline, film_map=film_map,
            glossary=[], char_targets=[200, 150, 200], style_guide="", logger=LOGGER,
            anime_context=None, episode_memory=None, content_type="movie",
        )
    )
    assert rerun.asked == []
    assert cached_expanded == 1
    assert [item.narration for item in cached] == [item.narration for item in result]


@pytest.mark.parametrize("target", [1586])
def test_length_threshold_is_three_quarters(target: int) -> None:
    from review.__main__ import LENGTH_EXPAND_RATIO

    assert LENGTH_EXPAND_RATIO * target == pytest.approx(1189.5)
