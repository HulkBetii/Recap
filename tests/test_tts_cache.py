from __future__ import annotations

from common.schema import TtsManifestEntry
from tts.cache import TtsCache, build_cache_key


def test_tts_cache_hits_and_invalidates_changed_narration(tmp_path) -> None:
    cache = TtsCache(tmp_path)
    cache.prepare()
    key = build_cache_key(provider="ai33", voice_id="v1", model="m", speed=1.0, narration="hello", normalized=True)
    audio = cache.audio_path(0)
    audio.write_bytes(b"mp3")
    manifest = {
        "0": TtsManifestEntry(
            beat_id=0,
            cache_key=key,
            narration_hash="hash",
            provider="ai33",
            voice_id="v1",
            model="m",
            speed=1.0,
            normalized=True,
            audio_path="audio/0.mp3",
        )
    }

    assert cache.get_cached(manifest, 0, key) == audio
    changed_key = build_cache_key(provider="ai33", voice_id="v1", model="m", speed=1.0, narration="changed", normalized=True)
    assert cache.get_cached(manifest, 0, changed_key) is None


def test_tts_cache_force_clears_manifest_and_audio(tmp_path) -> None:
    cache = TtsCache(tmp_path)
    cache.prepare()
    cache.audio_path(0).write_bytes(b"mp3")
    cache.save_manifest({})

    forced = TtsCache(tmp_path, force=True)
    forced.prepare()

    assert not (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "audio" / "0.mp3").exists()
