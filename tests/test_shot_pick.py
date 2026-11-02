from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from common.schema import SentenceShotScores, ShotPickMeta
from shot_pick.__main__ import run_shot_pick, spread


def write_inputs(tmp_path: Path) -> argparse.Namespace:
    shots_dir = tmp_path / "shots"
    shots_dir.mkdir()
    shots = []
    for index in range(10):
        thumb = shots_dir / f"film-{index:03d}.jpg"
        thumb.write_bytes(f"jpeg{index}".encode())
        shots.append({
            "src": "film.mp4", "index": index, "tc_start": index * 5.0, "tc_end": index * 5.0 + 5.0, "duration": 5.0,
            "thumb": f"shots/film-{index:03d}.jpg", "motion_score": 0.5, "face_count": 1, "face_area": 0.1,
            "brightness": 0.5, "is_usable": index != 3,
        })
    (tmp_path / "shots.json").write_text(json.dumps(shots), encoding="utf-8")
    beats = [{"beat_id": 0, "narration": "Anh bị bắt. Cô ấy khóc. Thật buồn.", "from_seg_id": 0, "to_seg_id": 9, "src_tc_start": 0.0, "src_tc_end": 50.0}]
    (tmp_path / "review_script.json").write_text(json.dumps(beats, ensure_ascii=False), encoding="utf-8")
    refs = [
        {"beat_id": 0, "idx": 0, "text": "Anh bị bắt.", "from_seg_id": 1, "to_seg_id": 2, "src_tc_start": 10.0, "src_tc_end": 25.0, "grounded": True},
        {"beat_id": 0, "idx": 1, "text": "Cô ấy khóc.", "from_seg_id": 6, "to_seg_id": 6, "src_tc_start": 30.0, "src_tc_end": 33.0, "grounded": True},
        {"beat_id": 0, "idx": 2, "text": "Thật buồn."},
    ]
    (tmp_path / "review_script.sentences.json").write_text(json.dumps(refs, ensure_ascii=False), encoding="utf-8")
    stamps = [
        {"beat_id": 0, "idx": 0, "tl_start": 0.0, "tl_end": 3.0, "method": "silence"},
        {"beat_id": 0, "idx": 1, "tl_start": 3.0, "tl_end": 5.0, "method": "silence"},
        {"beat_id": 0, "idx": 2, "tl_start": 5.0, "tl_end": 6.0, "method": "silence"},
    ]
    (tmp_path / "sentence_timing.json").write_text(json.dumps(stamps), encoding="utf-8")
    return argparse.Namespace(
        review_script=tmp_path / "review_script.json",
        review_sentences=tmp_path / "review_script.sentences.json",
        sentence_timing=tmp_path / "sentence_timing.json",
        shots=tmp_path / "shots.json",
        output=tmp_path / "sentence_shot_scores.json",
        model="vlm",
        fallback_model=None,
        base_url=None,
        timeout_s=60,
        concurrency=2,
        max_candidates=6,
        sentences_per_request=6,
        work_dir=tmp_path / "work" / "shot_pick",
        force=False,
        log_level="ERROR",
    )


class FakeVlm:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def ask(self, prompt: str, *, image_data_urls: list[str] | None = None) -> str:
        self.calls.append((prompt, len(image_data_urls or [])))
        items = [json.loads(line) for line in prompt.split("ITEMS:\n", 1)[1].strip().splitlines()]
        return json.dumps([{"id": item["id"], "scores": {label: (3 if label == "B" else 1) for label in item["candidates"]}} for item in items])


def test_spread_keeps_ends_and_limit() -> None:
    assert spread(list(range(10)), 4) == [0, 3, 6, 9]  # type: ignore[arg-type]
    assert spread(list(range(3)), 6) == [0, 1, 2]  # type: ignore[arg-type]


def test_shot_pick_scores_grounded_sentences_and_reuses_its_cache(tmp_path: Path) -> None:
    args = write_inputs(tmp_path)
    client = FakeVlm()
    output, meta = asyncio.run(run_shot_pick(args, client))

    assert [(item.beat_id, item.idx) for item in output] == [(0, 0), (0, 1)]  # the commentary sentence is skipped
    first = output[0].scores
    assert 3 not in first  # unusable shot never offered
    assert sorted(first.values(), reverse=True)[0] == 1.0 and set(first.values()) == {1.0, round(1 / 3, 4)}
    assert meta.n_scored_sentences == 2 and meta.n_requests == 1
    assert len(client.calls) == 1 and client.calls[0][1] == sum(len(item.scores) for item in output)
    saved = [SentenceShotScores.model_validate(item) for item in json.loads(args.output.read_text(encoding="utf-8"))]
    assert saved == output
    ShotPickMeta.model_validate_json((tmp_path / "sentence_shot_scores.meta.json").read_text(encoding="utf-8"))

    again = FakeVlm()
    output_again, meta_again = asyncio.run(run_shot_pick(args, again))
    assert again.calls == [] and output_again == output and meta_again.n_cached_sentences == 2


def test_failed_batches_leave_sentences_unscored(tmp_path: Path) -> None:
    args = write_inputs(tmp_path)

    class Broken:
        async def ask(self, prompt: str, *, image_data_urls: list[str] | None = None) -> str:
            return "no json here"

    output, meta = asyncio.run(run_shot_pick(args, Broken()))
    assert output == [] and meta.n_requests == 2 and "failed" in meta.warnings[0]
