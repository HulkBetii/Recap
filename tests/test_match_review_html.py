from __future__ import annotations

import json
from pathlib import Path

from common.schema import EdlPlacement, ReviewBeat, Shot
from match.review_html import write_review_html


def test_write_review_html_contains_summary_and_escapes_text(tmp_path: Path) -> None:
    thumb_dir = tmp_path / "shots"
    thumb_dir.mkdir()
    (thumb_dir / "film-000.jpg").write_bytes(b"jpg")
    shots_path = tmp_path / "shots.json"
    shots_path.write_text("[]", encoding="utf-8")
    beat = ReviewBeat(beat_id=0, narration="Cô ấy <ngạc nhiên> & bỏ chạy.", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=2, is_hook=True)
    shot = Shot(src="film.mp4", index=0, tc_start=0, tc_end=2, duration=2, thumb="shots/film-000.jpg", motion_score=0.5, face_count=1, face_area=0.2, brightness=0.6, is_usable=True)
    placement = EdlPlacement(tl_start=0, tl_end=2, src="film.mp4", src_in=0, src_out=2, beat_id=0, shot_index=0, reused=False, speed=1)
    qa = {"n_intro_excluded": 0, "selected_from_non_story": False, "beats": [{"beat_id": 0, "avg_semantic_score": 0.5, "candidate_capacity_s": 2.0, "required_duration_s": 2.0, "dark_selected_ids": [0], "overlapping_repeat_count": 0, "warnings": ["low semantic"], "selected": [{"semantic_score": 0.5, "semantic_rank": 1, "dark_fallback": True}]}]}

    output = tmp_path / "edl.review.html"
    write_review_html(output_path=output, asset_dir=tmp_path / "edl.review", shots_path=shots_path, beats=[beat], placements=[placement], shots=[shot], qa=qa, thumbs_per_beat=8)

    html = output.read_text(encoding="utf-8")
    assert "Total beats" in html
    assert "Cô ấy &lt;ngạc nhiên&gt; &amp; bỏ chạy." in html
    assert "low semantic" in html
    assert "Dark selected: 1" in html
    assert "dark fallback=True" in html
    assert (tmp_path / "edl.review" / "beat-000-00-shot-0000.jpg").exists()


def test_write_review_html_handles_missing_thumbnail(tmp_path: Path) -> None:
    shots_path = tmp_path / "shots.json"
    shots_path.write_text(json.dumps([]), encoding="utf-8")
    beat = ReviewBeat(beat_id=0, narration="Beat", from_seg_id=0, to_seg_id=0, src_tc_start=0, src_tc_end=2, is_hook=True)
    shot = Shot(src="film.mp4", index=0, tc_start=0, tc_end=2, duration=2, thumb="shots/missing.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.6, is_usable=True)
    placement = EdlPlacement(tl_start=0, tl_end=2, src="film.mp4", src_in=0, src_out=2, beat_id=0, shot_index=0, reused=False, speed=1)

    output = tmp_path / "edl.review.html"
    write_review_html(output_path=output, asset_dir=tmp_path / "edl.review", shots_path=shots_path, beats=[beat], placements=[placement], shots=[shot], qa={"beats": []}, thumbs_per_beat=8)

    assert "missing thumbnail" in output.read_text(encoding="utf-8")
