from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import reaction_remix.tts.core as tts_core
import reaction_remix.tts.__main__ as tts_main
import reaction_remix.tts.audio as tts_audio
from common.integrity import file_hash
from reaction_remix.plan.core import build_remix_plan
from reaction_remix.tts.audio import AudioMetrics
from reaction_remix.tts.core import CommentaryTtsError, ReactionTtsSettings, synthesize_commentary
from reaction_remix.tts.japanese import normalize_japanese_tts_text
from reaction_remix.write.core import build_commentary_script
from tests.test_remix_plan import FakePlanClient, planner_payload, reaction_fixture
from tests.test_remix_write import FakeWriteClient
from tts.providers import ProviderResult, TtsProviderError


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        kwargs["output_path"].write_bytes(b"raw-audio")
        return ProviderResult(provider="ai33", voice_id=kwargs["voice_id"], audio_url="file://audio", model=kwargs["model"])


class FakeAsr:
    model_name = "large-v3"

    def similarity(self, _path: Path, _expected: str) -> float:
        return 0.96


class LowSimilarityAsr(FakeAsr):
    def similarity(self, _path: Path, _expected: str) -> float:
        return 0.5


def script_fixture():  # type: ignore[no-untyped-def]
    source, transcript, blocks = reaction_fixture()
    plan = asyncio.run(build_remix_plan(source, transcript, blocks, FakePlanClient(planner_payload())))
    script, _qa = asyncio.run(build_commentary_script(plan, blocks, transcript, FakeWriteClient()))
    return source, script


def test_ja_basic_never_inserts_vietnamese() -> None:
    text = normalize_japanese_tts_text("ニキ/ネキ\u200b  草だぜ。")

    assert text == "ニキ/ネキ 草だぜ。"
    assert "hoặc" not in text


def test_tts_policy_is_strict() -> None:
    with pytest.raises(CommentaryTtsError, match="voice_id is locked"):
        ReactionTtsSettings(voice_id="other").validate()


def test_tts_cache_and_empty_fit_request(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source, script = script_fixture()
    provider = FakeProvider()
    monkeypatch.setattr(tts_core, "normalize_commentary_audio", lambda src, dst, **_kwargs: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(tts_core, "measure_audio", lambda _path: AudioMetrics(5.0, -14.0, -2.2))

    async def run_once():  # type: ignore[no-untyped-def]
        return await synthesize_commentary(
            script,
            source,
            output_path=tmp_path / "commentary_audio.json",
            fit_request_path=tmp_path / "commentary_fit_requests.json",
            work_dir=tmp_path / "work",
            provider_client=provider,  # type: ignore[arg-type]
            asr_verifier=FakeAsr(),  # type: ignore[arg-type]
            script_hash="e" * 64,
        )

    audio, requests = asyncio.run(run_once())
    _audio2, requests2 = asyncio.run(run_once())

    assert requests.requests == []
    assert requests2.requests == []
    assert provider.calls == 1
    assert (tmp_path / "commentary_fit_requests.json").is_file()
    assert audio.items[0].audio_sha256 is not None
    assert audio.items[0].requested_model == "eleven_multilingual_v2"
    assert audio.items[0].actual_model is None
    assert audio.script_hash == "e" * 64

    manifest_path = tmp_path / "work" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry_key = next(iter(manifest))
    manifest[entry_key]["true_peak_dbfs"] = -1.8
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    asyncio.run(run_once())
    assert provider.calls == 1

    cached_file = next((tmp_path / "work" / "audio").glob("*.mp3"))
    cached_file.write_bytes(b"corrupt")
    asyncio.run(run_once())
    assert provider.calls == 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry_key = next(iter(manifest))
    manifest[entry_key]["asr_text_match"] = 0.5
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _audio3, quality_requests = asyncio.run(run_once())
    assert provider.calls == 2
    assert quality_requests.requests[0].direction == "clarify"


def test_tts_emits_selective_fit_request(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source, script = script_fixture()
    provider = FakeProvider()
    monkeypatch.setattr(tts_core, "normalize_commentary_audio", lambda src, dst, **_kwargs: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(tts_core, "measure_audio", lambda _path: AudioMetrics(11.0, -14.0, -2.2))

    _audio, requests = asyncio.run(
        synthesize_commentary(
            script,
            source,
            output_path=tmp_path / "commentary_audio.json",
            fit_request_path=tmp_path / "commentary_fit_requests.json",
            work_dir=tmp_path / "work",
            provider_client=provider,  # type: ignore[arg-type]
            asr_verifier=FakeAsr(),  # type: ignore[arg-type]
        )
    )

    assert len(requests.requests) == 1
    assert requests.requests[0].slot_id == "commentary-slot-0001"
    assert requests.requests[0].direction == "shorten"


def test_tts_cache_invalidates_when_audio_normalization_changes(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source, script = script_fixture()
    provider = FakeProvider()
    monkeypatch.setattr(tts_core, "normalize_commentary_audio", lambda src, dst, **_kwargs: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(tts_core, "measure_audio", lambda _path: AudioMetrics(5.0, -14.0, -2.2))

    async def run_once():  # type: ignore[no-untyped-def]
        return await synthesize_commentary(
            script,
            source,
            output_path=tmp_path / "commentary_audio.json",
            fit_request_path=tmp_path / "commentary_fit_requests.json",
            work_dir=tmp_path / "work",
            provider_client=provider,  # type: ignore[arg-type]
            asr_verifier=FakeAsr(),  # type: ignore[arg-type]
        )

    asyncio.run(run_once())
    monkeypatch.setattr(tts_audio, "COMMENTARY_AUDIO_NORMALIZATION_VERSION", "reaction-commentary-audio-v2")
    asyncio.run(run_once())
    monkeypatch.setattr(tts_audio, "ENCODE_TRUE_PEAK_HEADROOM_DB", 0.4)
    asyncio.run(run_once())

    assert provider.calls == 3


def test_tts_emits_clarify_request_for_low_japanese_asr_similarity(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source, script = script_fixture()
    provider = FakeProvider()
    monkeypatch.setattr(tts_core, "normalize_commentary_audio", lambda src, dst, **_kwargs: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(tts_core, "measure_audio", lambda _path: AudioMetrics(5.0, -14.0, -2.2))

    _audio, requests = asyncio.run(
        synthesize_commentary(
            script,
            source,
            output_path=tmp_path / "commentary_audio.json",
            fit_request_path=tmp_path / "commentary_fit_requests.json",
            work_dir=tmp_path / "work",
            provider_client=provider,  # type: ignore[arg-type]
            asr_verifier=LowSimilarityAsr(),  # type: ignore[arg-type]
        )
    )

    assert requests.requests[0].direction == "clarify"
    assert "TTS-friendly" in requests.requests[0].reason


def test_tts_does_not_hide_programming_errors(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source, script = script_fixture()
    provider = FakeProvider()

    def programming_error(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise TypeError("programming error")

    monkeypatch.setattr(tts_core, "normalize_commentary_audio", programming_error)

    with pytest.raises(TypeError, match="programming error"):
        asyncio.run(
            synthesize_commentary(
                script,
                source,
                output_path=tmp_path / "commentary_audio.json",
                fit_request_path=tmp_path / "commentary_fit_requests.json",
                work_dir=tmp_path / "work",
                provider_client=provider,  # type: ignore[arg-type]
                asr_verifier=FakeAsr(),  # type: ignore[arg-type]
            )
        )


def test_tts_failure_keeps_completed_slot_cache_for_resume(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source, script = script_fixture()
    first = script.slots[0]
    script = script.model_copy(
        update={
            "slots": [
                first,
                first.model_copy(
                    update={
                        "slot_id": "commentary-slot-0002",
                        "before_item_id": "item-0002",
                        "after_item_id": "item-0003",
                    }
                ),
            ]
        }
    )

    class FailSecondOnceProvider(FakeProvider):
        async def synthesize(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 2:
                raise TtsProviderError("synthetic provider failure")
            kwargs["output_path"].write_bytes(b"raw-audio")
            return ProviderResult(
                provider="ai33",
                voice_id=kwargs["voice_id"],
                audio_url="file://audio",
                model=kwargs["model"],
            )

    provider = FailSecondOnceProvider()
    monkeypatch.setattr(tts_core, "normalize_commentary_audio", lambda src, dst, **_kwargs: dst.write_bytes(src.read_bytes()))
    monkeypatch.setattr(tts_core, "measure_audio", lambda _path: AudioMetrics(5.0, -14.0, -2.2))

    async def run_once():  # type: ignore[no-untyped-def]
        return await synthesize_commentary(
            script,
            source,
            output_path=tmp_path / "commentary_audio.json",
            fit_request_path=tmp_path / "commentary_fit_requests.json",
            work_dir=tmp_path / "work",
            provider_client=provider,  # type: ignore[arg-type]
            asr_verifier=FakeAsr(),  # type: ignore[arg-type]
        )

    with pytest.raises(CommentaryTtsError, match="synthetic provider failure"):
        asyncio.run(run_once())
    assert (tmp_path / "work" / "manifest.json").is_file()

    audio, _requests = asyncio.run(run_once())

    assert provider.calls == 3
    assert len(audio.items) == 2


def test_tts_cli_propagates_audio_asr_and_fit_settings(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    source, script = script_fixture()
    script_path = tmp_path / "commentary_script.json"
    source_path = tmp_path / "reaction_source.json"
    script_path.write_text(script.model_dump_json(), encoding="utf-8")
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    captured = {}

    async def fake_synthesize(_script, _source, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return None, type("Fit", (), {"requests": []})()

    monkeypatch.setattr(tts_main, "require_ffmpeg", lambda: None)
    monkeypatch.setattr(tts_main, "synthesize_commentary", fake_synthesize)
    args = tts_main.build_parser().parse_args(
        [
            "--script", str(script_path),
            "--source", str(source_path),
            "--output", str(tmp_path / "audio.json"),
            "--fit-request-output", str(tmp_path / "fit.json"),
            "--trim-handle-ms", "95",
            "--target-lufs", "-13.5",
            "--max-true-peak-db", "-2.3",
            "--asr-model", "medium",
            "--min-asr-similarity", "0.92",
            "--fit-tolerance-s", "0.12",
            "--max-fit-iterations", "1",
        ]
    )

    assert asyncio.run(tts_main.run(args)) == 0
    settings = captured["settings"]
    assert settings.trim_handle_ms == 95
    assert settings.target_lufs == -13.5
    assert settings.max_true_peak_db == -2.3
    assert settings.min_asr_similarity == 0.92
    assert settings.fit_tolerance_s == 0.12
    assert settings.max_fit_iterations == 1
    assert captured["asr_verifier"].model_name == "medium"
    assert captured["script_hash"] == file_hash(script_path)
