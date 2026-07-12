from __future__ import annotations

import pytest

from common.schema import BeatTiming, TtsMeta, validate_beats_timing


def test_beat_timing_rejects_mismatched_duration() -> None:
    with pytest.raises(ValueError, match="tl_end must equal"):
        BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0.0, tl_end=2.0, duration=1.0)


def test_validate_beats_timing_requires_continuity_with_pause() -> None:
    timings = [
        BeatTiming(beat_id=0, audio_path="audio/0.mp3", tl_start=0.0, tl_end=1.0, duration=1.0),
        BeatTiming(beat_id=1, audio_path="audio/1.mp3", tl_start=1.2, tl_end=2.2, duration=1.0),
    ]

    assert validate_beats_timing(timings, pause_s=0.2) == timings

    bad = [timings[0], timings[1].model_copy(update={"tl_start": 1.1, "tl_end": 2.1})]
    with pytest.raises(ValueError, match="previous tl_end"):
        validate_beats_timing(bad, pause_s=0.2)


def test_legacy_tts_meta_defaults_provider_diagnostics() -> None:
    meta = TtsMeta.model_validate({
        "voice_id": "voice",
        "provider_mode": "ai33",
        "model": "model",
        "speed": 1.0,
        "inter_beat_pause_s": 0.15,
        "total_duration_s": 1.0,
        "total_chars": 10,
        "est_cost": 0.0,
        "created_at": "2026-07-12T00:00:00Z",
    })

    assert meta.providers_used == []
    assert meta.provider_counts == {}
    assert meta.fallback_count == 0
