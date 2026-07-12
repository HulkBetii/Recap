from __future__ import annotations

from ingest.cache import StageCache


def seed_integrity_cache(cache: StageCache) -> None:
    cache.path("audio.wav").write_bytes(b"audio")
    cache.commit_stage("audio", "audio-v1")
    for name in ("transcript_text.json", "transcript_aligned.json", "transcript_quality.json"):
        cache.write_json(name, {"ok": True})
    cache.commit_stage("transcript", "transcript-v1")
    cache.write_json("transcript_corrected.json", [{"ok": True}])
    cache.write_json("transcript_correction.meta.json", {"warnings": []})
    cache.commit_stage("correction", "correction-v1")
    cache.write_json("translated.json", [{"ok": True}])
    cache.commit_stage("translation", "translation-v1")
    cache.write_json("vision.json", [])
    cache.commit_stage("vision", "vision-v1")


def test_cache_records_hits_and_force_removes_artifacts(tmp_path) -> None:
    cache = StageCache(tmp_path)
    cache.write_json("translated.json", [{"id": 0, "value": "ok"}])

    assert cache.has("translated.json") is True
    assert cache.cache_hits == ["translated.json"]

    forced = StageCache(tmp_path, force=True)
    forced.prepare()

    assert not (tmp_path / "translated.json").exists()


def test_correction_change_preserves_aligned_transcript(tmp_path) -> None:
    cache = StageCache(tmp_path)
    cache.prepare()
    seed_integrity_cache(cache)

    assert not cache.stage_current("correction", "correction-v2", ("transcript_corrected.json", "transcript_correction.meta.json"))

    assert cache.path("transcript_aligned.json").is_file()
    assert not cache.path("transcript_corrected.json").exists()
    assert not cache.path("translated.json").exists()
    assert not cache.path("vision.json").exists()


def test_profile_change_invalidates_only_vision(tmp_path) -> None:
    cache = StageCache(tmp_path)
    cache.prepare()
    seed_integrity_cache(cache)

    assert not cache.stage_current("vision", "vision-v2", ("vision.json",))

    assert cache.path("transcript_aligned.json").is_file()
    assert cache.path("translated.json").is_file()
    assert not cache.path("vision.json").exists()


def test_legacy_or_corrupt_manifest_rebuilds_once(tmp_path) -> None:
    (tmp_path / "audio.wav").write_bytes(b"legacy")
    (tmp_path / "cache_manifest.json").write_text("{broken", encoding="utf-8")

    cache = StageCache(tmp_path)
    cache.prepare()

    assert not cache.path("audio.wav").exists()
    assert cache.manifest["keys"] == {}


def test_wrong_schema_artifact_is_not_reused(tmp_path) -> None:
    cache = StageCache(tmp_path)
    cache.prepare()
    cache.write_json("translated.json", [{"syntactically": "valid but wrong"}])
    cache.commit_stage("translation", "translation-v1")

    assert cache.stage_current("translation", "translation-v1", ("translated.json",)) is False
    assert not cache.path("translated.json").exists()
