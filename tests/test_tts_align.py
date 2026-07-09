from __future__ import annotations

import json
from pathlib import Path

from tts_align.__main__ import main as tts_align_main


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_tts_align_builds_micro_with_proportional_fallback(tmp_path: Path, monkeypatch) -> None:
    film_map = [
        {"id": 0, "type": "speech", "tc_start": 0, "tc_end": 50, "ko": "a", "en": "a", "scene_desc": None},
        {"id": 1, "type": "speech", "tc_start": 50, "tc_end": 100, "ko": "b", "en": "b", "scene_desc": None},
        {"id": 2, "type": "speech", "tc_start": 100, "tc_end": 150, "ko": "c", "en": "c", "scene_desc": None},
        {"id": 3, "type": "speech", "tc_start": 150, "tc_end": 210, "ko": "d", "en": "d", "scene_desc": None},
    ]
    review = [
        {
            "beat_id": 0,
            "narration": "Mở đầu có chuyện lớn xảy ra. Cảnh sát lập tức vào cuộc. Kẻ xấu bỏ chạy rất nhanh.",
            "from_seg_id": 0,
            "to_seg_id": 3,
            "src_tc_start": 0,
            "src_tc_end": 210,
            "is_hook": True,
        }
    ]
    timing = [{"beat_id": 0, "audio_path": "audio/0.mp3", "tl_start": 0, "tl_end": 18, "duration": 18}]
    write_json(tmp_path / "film_map.json", film_map)
    write_json(tmp_path / "review_script.json", review)
    write_json(tmp_path / "beats_timing.json", timing)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tts_align",
            "--review-script", str(tmp_path / "review_script.json"),
            "--beats-timing", str(tmp_path / "beats_timing.json"),
            "--film-map", str(tmp_path / "film_map.json"),
            "--audio-dir", str(tmp_path / "audio"),
            "--output-micro", str(tmp_path / "review_script.micro.json"),
            "--output-policy", str(tmp_path / "micro_policy.json"),
            "--output-align", str(tmp_path / "tts_align.json"),
            "--output-meta", str(tmp_path / "review_script.micro.meta.json"),
            "--mode", "auto",
            "--aligner", "none",
            "--max-source-span-s", "60",
        ],
    )
    assert tts_align_main() == 0
    micro = json.loads((tmp_path / "review_script.micro.json").read_text(encoding="utf-8"))
    meta = json.loads((tmp_path / "review_script.micro.meta.json").read_text(encoding="utf-8"))
    assert len(micro) == 3
    assert meta["enabled"] is True
    assert meta["alignment_methods"] == {"proportional": 3}
    assert micro[0]["src_tc_start"] == 0
    assert micro[-1]["src_tc_end"] == 210
    assert all(micro[index]["src_tc_end"] <= micro[index + 1]["src_tc_start"] + 0.001 for index in range(len(micro) - 1))
