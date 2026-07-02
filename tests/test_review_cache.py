from __future__ import annotations

from review.cache import ReviewCache


def test_review_cache_hits_and_force(tmp_path) -> None:
    cache = ReviewCache(tmp_path)
    cache.write_json("outline.json", {"ok": True})

    assert cache.has("outline.json") is True
    assert cache.cache_hits == ["outline.json"]

    forced = ReviewCache(tmp_path, force=True)
    forced.prepare()

    assert not (tmp_path / "outline.json").exists()
    assert (tmp_path / "revisions").is_dir()
