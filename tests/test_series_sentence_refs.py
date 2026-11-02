from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from common.schema import SeriesSentenceRef
from series_composer.sentence_refs import run


def write_episode(run_dir: Path, offset: float) -> None:
    run_dir.mkdir(parents=True)
    film_map = [
        {"id": index, "type": "speech", "tc_start": offset + index * 4.0, "tc_end": offset + index * 4.0 + 3.0, "ko": "k", "en": f"line {index}"}
        for index in range(10)
    ]
    (run_dir / "film_map.json").write_text(json.dumps(film_map), encoding="utf-8")


def ref(episode: str) -> dict[str, object]:
    return {
        "event_id": f"{episode}:section:0", "episode_key": episode, "src": f"{episode}/{episode}.mp4",
        "source_path": f"C:/m/{episode}.mp4", "from_seg_id": 2, "to_seg_id": 8, "src_tc_start": 8.0, "src_tc_end": 35.0,
    }


class FakeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps([{"beat_id": 0, "sentences": [
            {"idx": 0, "ref": 1, "seg_ids": [3, 4]},
            {"idx": 1, "ref": 0, "seg_ids": [0, 9]},
            {"idx": 2, "ref": 5, "seg_ids": [3, 3]},
        ]}])


def test_season_sentences_cite_the_right_episode_and_stay_inside_the_ref(tmp_path: Path) -> None:
    write_episode(tmp_path / "e01", 0.0)
    write_episode(tmp_path / "e02", 1000.0)
    script = [{"beat_id": 0, "narration": "Câu một. Câu hai. Câu ba.", "source_refs": [ref("e01"), ref("e02")]}]
    (tmp_path / "series_review_script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    args = argparse.Namespace(
        series_review_script=tmp_path / "series_review_script.json",
        episode_run_dir=[f"e01={tmp_path / 'e01'}", f"e02={tmp_path / 'e02'}"],
        output=tmp_path / "series_review_script.sentences.json",
        llm_model="fast", llm_fallback_model=None, llm_base_url=None, reply_timeout_s=60, concurrency=2,
        work_dir=tmp_path / "work", force=False, log_level="ERROR",
    )
    client = FakeClient()
    refs = asyncio.run(run(args, client))

    assert '"episode": "e02"' in client.prompts[0] and "#2 line 2" in client.prompts[0]
    first, second, third = refs
    assert (first.episode_key, first.from_seg_id, first.src_tc_start, first.src_tc_end) == ("e02", 3, 1012.0, 1019.0)
    assert (second.episode_key, second.from_seg_id, second.to_seg_id) == ("e01", 2, 8)  # clamped to the ref span
    assert third.grounded is False  # unknown ref index
    saved = [SeriesSentenceRef.model_validate(item) for item in json.loads(args.output.read_text(encoding="utf-8"))]
    assert saved == refs

    again = FakeClient()
    asyncio.run(run(args, again))
    assert again.prompts == []
