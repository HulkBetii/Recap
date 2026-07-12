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


def test_review_identity_change_clears_every_generated_artifact(tmp_path) -> None:
    cache = ReviewCache(tmp_path)
    cache.prepare()
    assert cache.reconcile("key-v1") is False
    for name in ("outline.json", "opening_coherence.json", "micro_beats.json", "non_story_beats.json"):
        cache.write_json(name, {"ok": True})
    cache.write_json("revisions/qa-0.json", {"ok": True})
    cache.write_json("style_revisions/0.json", {"ok": True})

    assert cache.reconcile("key-v2") is False

    assert not (tmp_path / "outline.json").exists()
    assert not (tmp_path / "opening_coherence.json").exists()
    assert not any((tmp_path / "revisions").iterdir())
    assert not any((tmp_path / "style_revisions").iterdir())


def test_review_manifest_reuses_matching_identity(tmp_path) -> None:
    cache = ReviewCache(tmp_path)
    cache.prepare()
    assert cache.reconcile("same") is False
    cache.write_json("outline.json", {"ok": True})

    resumed = ReviewCache(tmp_path)
    resumed.prepare()

    assert resumed.reconcile("same") is True
    assert resumed.path("outline.json").is_file()


def test_review_corrupt_artifact_invalidates_matching_manifest(tmp_path) -> None:
    cache = ReviewCache(tmp_path)
    cache.prepare()
    cache.reconcile("same")
    cache.path("outline.json").write_text("{broken", encoding="utf-8")

    resumed = ReviewCache(tmp_path)
    resumed.prepare()

    assert resumed.reconcile("same") is False
    assert not resumed.path("outline.json").exists()
