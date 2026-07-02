from __future__ import annotations

from shots.cache import ShotsCache, stable_hash


def test_shots_cache_hits_and_invalidates(tmp_path) -> None:
    cache = ShotsCache(tmp_path)
    cache.prepare()
    key = stable_hash({"a": 1})
    cache.write_cached("features.json", key, {"0": {"brightness": 1}})

    assert cache.read_cached("features.json", key) == {"0": {"brightness": 1}}
    assert cache.cache_hits == ["features.json"]
    assert cache.read_cached("features.json", stable_hash({"a": 2})) is None


def test_shots_cache_force_clears_artifacts(tmp_path) -> None:
    cache = ShotsCache(tmp_path)
    cache.prepare()
    cache.write_cached("detection.json", "k", [])
    (tmp_path / "thumbs" / "x.jpg").write_bytes(b"jpg")

    forced = ShotsCache(tmp_path, force=True)
    forced.prepare()

    assert not (tmp_path / "detection.json").exists()
    assert not (tmp_path / "thumbs" / "x.jpg").exists()
