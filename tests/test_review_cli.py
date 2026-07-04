from __future__ import annotations

import argparse
import json

from review.__main__ import build_review_with_client


class FakeReviewClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.calls.append(prompt)
        if "keys: glossary, outline, hook" in prompt:
            return json.dumps(
                {
                    "glossary": [{"name": "Minh", "role": "nhân vật chính"}],
                    "outline": [
                        {"from_seg_id": 3, "to_seg_id": 3, "summary": "hook"},
                        {"from_seg_id": 0, "to_seg_id": 1, "summary": "mở đầu"},
                        {"from_seg_id": 2, "to_seg_id": 3, "summary": "cao trào"},
                    ],
                    "hook": [3],
                },
                ensure_ascii=False,
            )
        if "JSON array of objects" in prompt:
            return json.dumps(
                [
                    {"beat_id": 0, "narration": "Một bí mật kinh hoàng mở ra."},
                    {"beat_id": 1, "narration": "Minh bắt đầu phát hiện mọi thứ không bình thường."},
                    {"beat_id": 2, "narration": "Cuối cùng, sự thật khiến tất cả đảo lộn."},
                ],
                ensure_ascii=False,
            )
        if "Regenerate only this one" in prompt:
            return json.dumps({"beat_id": 1, "narration": "Minh lần theo dấu vết và nhận ra cả nhà đang che giấu một bí mật."}, ensure_ascii=False)
        if "Review this Vietnamese recap" in prompt and not any("Regenerate only" in call for call in self.calls):
            return json.dumps(
                {"pass": False, "issues": [{"beat_id": 1, "type": "accuracy", "suggestion": "Làm rõ theo film_map"}], "notes": "needs fix"},
                ensure_ascii=False,
            )
        return json.dumps({"pass": True, "issues": [], "notes": "ok"}, ensure_ascii=False)


def write_film_map(tmp_path):  # type: ignore[no-untyped-def]
    film_map = [
        {"id": 0, "type": "speech", "tc_start": 0.0, "tc_end": 1.0, "ko": "a", "en": "A boy enters."},
        {"id": 1, "type": "speech", "tc_start": 1.0, "tc_end": 2.0, "ko": "b", "en": "He sees a clue."},
        {"id": 2, "type": "visual", "tc_start": 2.0, "tc_end": 3.0, "scene_desc": "A dark hallway."},
        {"id": 3, "type": "speech", "tc_start": 3.0, "tc_end": 4.0, "ko": "c", "en": "The secret is revealed."},
    ]
    path = tmp_path / "film_map.json"
    path.write_text(json.dumps(film_map, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "film_map.meta.json").write_text(json.dumps({"duration_s": 4.0}), encoding="utf-8")
    return path


def make_args(tmp_path, film_map_path):  # type: ignore[no-untyped-def]
    return argparse.Namespace(
        film_map=film_map_path,
        output=tmp_path / "review_script.json",
        target_ratio=0.33,
        tts_cps=15.0,
        min_coverage=0.85,
        max_qa_iterations=2,
        style_sample=None,
        style_preset="viral-recap-vi",
        style_strength="strong",
        style_qa=True,
        target_sentence_chars=160,
        max_sentence_chars=220,
        work_dir=tmp_path / "work" / "review",
        chatgpt_profile_dir=tmp_path / "profile",
        force=False,
        headless=True,
        log_level="ERROR",
    )


def test_build_review_with_mock_client_end_to_end(tmp_path) -> None:
    film_map_path = write_film_map(tmp_path)
    client = FakeReviewClient()

    import asyncio

    beats, meta = asyncio.run(build_review_with_client(make_args(tmp_path, film_map_path), client))

    assert (tmp_path / "review_script.json").exists()
    assert (tmp_path / "review_script.meta.json").exists()
    assert beats[0].is_hook is True
    assert beats[1].narration.startswith("Minh lần theo")
    assert beats[1].src_tc_start == 0.0
    assert beats[1].src_tc_end == 2.0
    assert meta.coverage_pct == 1.0
    assert meta.n_qa_iterations == 1
    assert len(meta.qa_report) == 2
    assert (tmp_path / "work" / "review" / "outline.json").exists()
    assert (tmp_path / "work" / "review" / "narration.json").exists()
    assert (tmp_path / "work" / "review" / "qa.json").exists()
    assert (tmp_path / "work" / "review" / "style_qa.json").exists()
    assert meta.style_preset == "viral-recap-vi"


def test_build_review_falls_back_duration_without_meta(tmp_path) -> None:
    film_map_path = write_film_map(tmp_path)
    (tmp_path / "film_map.meta.json").unlink()
    client = FakeReviewClient()

    import asyncio

    _beats, meta = asyncio.run(build_review_with_client(make_args(tmp_path, film_map_path), client))

    assert any("duration fallback" in warning for warning in meta.warnings)

def test_qa_ignores_invalid_beat_ids(tmp_path) -> None:
    class InvalidQaClient(FakeReviewClient):
        async def ask(self, prompt: str) -> str:
            self.calls.append(prompt)
            if "keys: glossary, outline, hook" in prompt:
                return json.dumps({"glossary": [{"name": "Minh"}], "outline": [{"from_seg_id": 0, "to_seg_id": 3, "summary": "all", "is_hook": True}], "hook": [0]}, ensure_ascii=False)
            if "JSON array of objects" in prompt:
                return json.dumps([{"beat_id": 0, "narration": "Minh chạy qua hành lang tối và phát hiện bí mật."}], ensure_ascii=False)
            if "Review this Vietnamese recap" in prompt:
                return json.dumps({"pass": False, "issues": [{"beat_id": -1, "type": "general", "suggestion": "bad"}], "notes": "general"}, ensure_ascii=False)
            return json.dumps({"pass": True, "issues": [], "notes": "ok"}, ensure_ascii=False)

    film_map_path = write_film_map(tmp_path)
    import asyncio
    beats, meta = asyncio.run(build_review_with_client(make_args(tmp_path, film_map_path), InvalidQaClient()))
    assert beats
    assert meta.qa_report[0]["issues"] == []
