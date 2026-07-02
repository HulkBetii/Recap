from __future__ import annotations

from ingest.cache import StageCache


def test_cache_records_hits_and_force_removes_artifacts(tmp_path) -> None:
    cache = StageCache(tmp_path)
    cache.write_json("translated.json", [{"id": 0, "value": "ok"}])

    assert cache.has("translated.json") is True
    assert cache.cache_hits == ["translated.json"]

    forced = StageCache(tmp_path, force=True)
    forced.prepare()

    assert not (tmp_path / "translated.json").exists()
