from __future__ import annotations

import asyncio
import json

import pytest

from review.llm_flow import build_narration_prompt, plan_narration_batches, request_narration
from review.models import NarrationBeat, OutlineBeat


def outline(n: int) -> list[OutlineBeat]:
    return [OutlineBeat(from_seg_id=index, to_seg_id=index, summary=f"beat {index}") for index in range(n)]


class BatchClient:
    def __init__(self, *, drop_id: int | None = None) -> None:
        self.prompts: list[str] = []
        self.drop_id = drop_id

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        beats = json.loads(prompt.split("BEATS:\n", 1)[1])
        return json.dumps(
            [{"beat_id": beat["beat_id"], "narration": f"Lời kể cho beat {beat['beat_id']}."} for beat in beats if beat["beat_id"] != self.drop_id],
            ensure_ascii=False,
        )


def test_plan_groups_consecutive_beats_under_char_budget() -> None:
    assert plan_narration_batches([3000, 3000, 3000, 3000, 9000, 100], 8000) == [[0, 1], [2, 3], [4], [5]]
    assert plan_narration_batches([500, 500], 8000) == [[0, 1]]


def test_short_script_keeps_single_unchanged_prompt() -> None:
    beats = outline(3)
    client = BatchClient()
    narration = asyncio.run(request_narration(client, outline=beats, glossary=[], char_targets=[400, 400, 400]))

    assert [beat.beat_id for beat in narration] == [0, 1, 2]
    assert client.prompts == [build_narration_prompt(outline=beats, glossary=[], char_targets=[400, 400, 400])]
    assert "FULL_OUTLINE" not in client.prompts[0]


def test_long_script_is_batched_with_continuity_and_cache() -> None:
    beats = outline(4)
    targets = [5000, 5000, 5000, 5000]
    saved: dict[tuple[int, ...], list[NarrationBeat]] = {}
    client = BatchClient()

    narration = asyncio.run(
        request_narration(
            client,
            outline=beats,
            glossary=[],
            char_targets=targets,
            load_batch=lambda batch: saved.get(tuple(batch)),
            save_batch=lambda batch, items: saved.__setitem__(tuple(batch), items),
        )
    )

    assert [beat.beat_id for beat in narration] == [0, 1, 2, 3]
    assert len(client.prompts) == 4
    assert "FULL_OUTLINE" in client.prompts[1]
    assert "PREVIOUS_BEAT_NARRATION" in client.prompts[1] and "Lời kể cho beat 0." in client.prompts[1]
    assert set(saved) == {(0,), (1,), (2,), (3,)}

    rerun = BatchClient()
    again = asyncio.run(
        request_narration(rerun, outline=beats, glossary=[], char_targets=targets, load_batch=lambda batch: saved.get(tuple(batch)))
    )
    assert rerun.prompts == []
    assert [beat.narration for beat in again] == [beat.narration for beat in narration]


def test_batch_missing_beat_fails_clearly() -> None:
    with pytest.raises(ValueError, match="missing beat_ids \\[1\\]"):
        asyncio.run(request_narration(BatchClient(drop_id=1), outline=outline(2), glossary=[], char_targets=[6000, 6000]))


def test_source_view_tags_neighbour_segments_and_feeds_prompts() -> None:
    from common.schema import FilmMapSegment, ReviewBeat
    from review.llm_flow import OPENING_REWRITE_RULE, build_narration_prompt, build_regenerate_prompt, build_source_view
    from review.models import OutlineBeat

    film_map = [
        FilmMapSegment(id=index, type="speech", tc_start=float(index), tc_end=index + 1.0, ko="k", en=f"line {index}")
        for index in range(6)
    ]
    assert build_source_view(film_map, [(2, 3)], context=1) == "#1 [other beat] line 1\n#2 line 2\n#3 line 3\n#4 [other beat] line 4"

    outline = [OutlineBeat(from_seg_id=0, to_seg_id=2, summary="a"), OutlineBeat(from_seg_id=3, to_seg_id=5, summary="b")]
    prompt = build_narration_prompt(outline=outline, glossary=[], char_targets=[100, 100], film_map=film_map)
    assert "Narrate only events present in that beat's source" in prompt
    assert '"source": ' + json.dumps("\n".join(["#3 line 3", "#4 line 4", "#5 line 5"])) in prompt
    assert "SOURCE" not in build_narration_prompt(outline=outline, glossary=[], char_targets=[100, 100])

    beat = ReviewBeat(beat_id=0, narration="x", from_seg_id=0, to_seg_id=2, src_tc_start=0.0, src_tc_end=3.0)
    regenerate = build_regenerate_prompt(beat=beat, issue="i", glossary=[], char_target=100, source="#0 line 0", opening_rule=OPENING_REWRITE_RULE)
    assert "SOURCE:\n#0 line 0\n\nCURRENT_BEAT:" in regenerate
    assert "opening beat of a movie recap" in regenerate
