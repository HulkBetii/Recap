from __future__ import annotations

import argparse
import json
from pathlib import Path

from tts.__main__ import run_tts_with_client
from tts.providers import ProviderResult, TtsProviderClient, TtsProviderError


class FakeProvider(TtsProviderClient):
    def __init__(self, fail_ai33: bool = False) -> None:
        self.fail_ai33 = fail_ai33
        self.calls: list[str] = []
        self.texts: list[str] = []

    async def _synthesize_ai33(self, text: str, voice_id: str, speed: float, output_path: Path) -> ProviderResult:
        self.calls.append("ai33")
        self.texts.append(text)
        if self.fail_ai33:
            raise TtsProviderError("ai33 failed")
        output_path.write_bytes(f"ai33:{text}".encode("utf-8"))
        return ProviderResult(provider="ai33", voice_id=voice_id, audio_url="file://ai33")

    async def _synthesize_genmax(self, text: str, voice_id: str, model: str, output_path: Path) -> ProviderResult:
        self.calls.append("genmax")
        self.texts.append(text)
        output_path.write_bytes(f"genmax:{text}".encode("utf-8"))
        return ProviderResult(provider="genmax", voice_id=voice_id, audio_url="file://genmax")


def write_review_script(tmp_path):  # type: ignore[no-untyped-def]
    data = [
        {"beat_id": 0, "narration": "Mở đầu căng thẳng.", "from_seg_id": 1, "to_seg_id": 1, "src_tc_start": 1.0, "src_tc_end": 2.0, "is_hook": True},
        {"beat_id": 1, "narration": "Câu chuyện tiếp tục.", "from_seg_id": 0, "to_seg_id": 0, "src_tc_start": 0.0, "src_tc_end": 1.0, "is_hook": False},
    ]
    path = tmp_path / "review_script.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    meta = tmp_path / "film_map.meta.json"
    meta.write_text(json.dumps({"duration_s": 10.0}), encoding="utf-8")
    return path, meta


def make_args(tmp_path, review_script, film_meta=None, force=False):  # type: ignore[no-untyped-def]
    return argparse.Namespace(
        review_script=review_script,
        output_audio=tmp_path / "out" / "voiceover.mp3",
        output_timing=tmp_path / "out" / "beats_timing.json",
        voice_id="ai33voice",
        provider_mode="auto",
        genmax_voice_id="gxvoice",
        model="eleven_multilingual_v2",
        speed=1.0,
        inter_beat_pause=0.15,
        concurrency=2,
        film_meta=film_meta,
        no_normalize=False,
        work_dir=tmp_path / "work" / "tts",
        force=force,
        cost_per_1k_chars=0.01,
        tts_text_normalization="vi",
        tts_pronunciation_lexicon=None,
        tts_normalized_script_output=None,
        tts_normalization_report=None,
        log_level="ERROR",
    )


def test_tts_cli_mock_end_to_end(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    review_script, film_meta = write_review_script(tmp_path)
    monkeypatch.setattr("tts.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("tts.__main__.normalize_audio", lambda src, dst: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr("tts.__main__.concat_voiceover", lambda paths, pause, work_dir, output: output.write_bytes(b"voiceover"))
    durations = {}

    def fake_probe(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "0.mp3":
            return 1.0
        if name == "1.mp3":
            return 2.0
        return 3.15

    monkeypatch.setattr("tts.__main__.probe_duration", fake_probe)

    import asyncio

    provider = FakeProvider()
    timings, meta = asyncio.run(run_tts_with_client(make_args(tmp_path, review_script, film_meta), provider))

    assert (tmp_path / "out" / "audio" / "0.mp3").exists()
    assert (tmp_path / "out" / "audio" / "1.mp3").exists()
    assert (tmp_path / "out" / "voiceover.mp3").exists()
    assert (tmp_path / "out" / "beats_timing.json").exists()
    assert (tmp_path / "out" / "tts_meta.json").exists()
    assert (tmp_path / "out" / "tts_script.json").exists()
    assert (tmp_path / "out" / "tts_normalization_report.json").exists()
    assert provider.texts == ["M\u1edf \u0111\u1ea7u c\u0103ng th\u1eb3ng.", "C\u00e2u chuy\u1ec7n ti\u1ebfp t\u1ee5c."]
    assert [(t.tl_start, t.tl_end) for t in timings] == [(0.0, 1.0), (1.15, 3.15)]
    assert meta.real_ratio == 0.315
    assert meta.est_cost > 0
    assert meta.text_normalization == "vi"


def test_tts_cli_cache_skips_second_run(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    review_script, film_meta = write_review_script(tmp_path)
    monkeypatch.setattr("tts.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("tts.__main__.normalize_audio", lambda src, dst: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr("tts.__main__.concat_voiceover", lambda paths, pause, work_dir, output: output.write_bytes(b"voiceover"))
    monkeypatch.setattr("tts.__main__.probe_duration", lambda path: 1.0 if Path(path).suffix == ".mp3" else 1.0)

    import asyncio

    first_provider = FakeProvider()
    asyncio.run(run_tts_with_client(make_args(tmp_path, review_script, film_meta), first_provider))
    second_provider = FakeProvider()
    _timings, meta = asyncio.run(run_tts_with_client(make_args(tmp_path, review_script, film_meta), second_provider))

    assert first_provider.calls == ["ai33", "ai33"]
    assert second_provider.calls == []
    assert meta.cache_hits == ["audio/0.mp3", "audio/1.mp3"]


def test_tts_cli_auto_uses_genmax_fallback(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    review_script, film_meta = write_review_script(tmp_path)
    monkeypatch.setattr("tts.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("tts.__main__.normalize_audio", lambda src, dst: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr("tts.__main__.concat_voiceover", lambda paths, pause, work_dir, output: output.write_bytes(b"voiceover"))
    monkeypatch.setattr("tts.__main__.probe_duration", lambda path: 1.0)

    import asyncio

    provider = FakeProvider(fail_ai33=True)
    asyncio.run(run_tts_with_client(make_args(tmp_path, review_script, film_meta), provider))

    assert provider.calls == ["ai33", "genmax", "ai33", "genmax"]
