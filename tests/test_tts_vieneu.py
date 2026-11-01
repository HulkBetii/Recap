from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tts.vieneu_provider import (
    VieneuProviderError,
    VieneuSynthesizer,
    reference_identity,
    validate_vieneu_settings,
    voice_reference_path,
)


class FakeEngine:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[dict[str, object]] = []
        self.guard = threading.Lock()

    def list_preset_voices(self) -> list[tuple[str, str]]:
        return [("Female storytelling", "Ngọc Linh")]

    def infer(self, text: str, **kwargs):  # type: ignore[no-untyped-def]
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            self.calls.append({"text": text, **kwargs})
            return b"audio"
        finally:
            with self.guard:
                self.active -= 1

    def save(self, _audio, output_path: Path) -> None:  # type: ignore[no-untyped-def]
        Path(output_path).write_bytes(b"wav")


def test_vieneu_synthesizer_loads_once_serializes_and_encodes_speed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    engine = FakeEngine()
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs):  # type: ignore[no-untyped-def]
        factory_calls.append(kwargs)
        return engine

    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp3")

    monkeypatch.setattr("tts.vieneu_provider.run_command", fake_run)
    synthesizer = VieneuSynthesizer(engine_factory=factory)

    def run(index: int) -> None:
        synthesizer.synthesize(
            text=f"beat {index}",
            voice_id="Ngọc Linh",
            style="doc_truyen",
            backend="onnx",
            precision="int8",
            model="model-id",
            threads=2,
            speed=1.1,
            output_path=tmp_path / f"{index}.mp3",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))

    assert len(factory_calls) == 1
    assert factory_calls[0] == {
        "mode": "v3turbo",
        "backbone_repo": "model-id",
        "backend": "onnx",
        "device": "cpu",
        "precision": "int8",
        "threads": 2,
    }
    assert engine.max_active == 1
    assert all(call["voice"] == "Ngọc Linh" for call in engine.calls)
    assert all("style" not in call for call in engine.calls), "style is ignored by SDK >= 3.7 and must not be sent"
    assert all(call["apply_watermark"] is True for call in engine.calls)
    assert all("atempo=1.100000" in command for command in commands)
    assert (tmp_path / "0.mp3").read_bytes() == b"mp3"
    assert not list(tmp_path.glob("*.vieneu.*"))


def test_vieneu_rejects_unknown_voice_without_writing_output(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("tts.vieneu_provider.run_command", lambda _command: None)
    synthesizer = VieneuSynthesizer(engine_factory=lambda **_kwargs: FakeEngine())

    with pytest.raises(VieneuProviderError, match="Unknown VieNeu preset voice"):
        synthesizer.synthesize(
            text="hello",
            voice_id="Missing",
            style="doc_truyen",
            backend="onnx",
            precision="int8",
            model="model-id",
            threads=0,
            speed=1.0,
            output_path=tmp_path / "out.mp3",
        )

    assert not (tmp_path / "out.mp3").exists()


@pytest.mark.parametrize(
    ("backend", "precision", "style", "threads", "message"),
    [
        ("torch", "int8", "doc_truyen", 0, "backend must be onnx or pytorch"),
        ("onnx", "float16", "doc_truyen", 0, "precision"),
        ("onnx", "int8", "dramatic", 0, "style"),
        ("onnx", "int8", "doc_truyen", -1, "threads"),
    ],
)
def test_vieneu_settings_are_strict(backend, precision, style, threads, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(VieneuProviderError, match=message):
        validate_vieneu_settings(backend=backend, precision=precision, style=style, threads=threads)

def test_vieneu_pytorch_backend_runs_on_cuda_without_onnx_precision(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs):  # type: ignore[no-untyped-def]
        factory_calls.append(kwargs)
        return FakeEngine()

    monkeypatch.setattr("tts.vieneu_provider.run_command", lambda command: Path(command[-1]).write_bytes(b"mp3"))
    VieneuSynthesizer(engine_factory=factory).synthesize(
        text="beat",
        voice_id="Ngọc Linh",
        style="doc_truyen",
        backend="pytorch",
        precision="int8",
        model="model-id",
        threads=0,
        speed=1.0,
        output_path=tmp_path / "out.mp3",
    )
    assert factory_calls == [{"mode": "v3turbo", "backbone_repo": "model-id", "backend": "pytorch", "threads": 0, "device": "cuda"}]


def test_voice_id_pointing_at_an_audio_file_is_cloned_once_and_reused(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reference = tmp_path / "ref.wav"
    reference.write_bytes(b"RIFFfake")
    enrolled: list[tuple[str, Path, bool]] = []

    class CloningEngine(FakeEngine):
        def add_voice(self, name, ref_audio, *, denoise=True, **_kwargs):  # type: ignore[no-untyped-def]
            enrolled.append((name, Path(ref_audio), denoise))
            return name

    engine = CloningEngine()
    monkeypatch.setattr("tts.vieneu_provider.run_command", lambda command: Path(command[-1]).write_bytes(b"mp3"))
    synthesizer = VieneuSynthesizer(engine_factory=lambda **_kwargs: engine)
    for index in range(3):
        synthesizer.synthesize(
            text=f"beat {index}",
            voice_id=str(reference),
            style="doc_truyen",
            backend="pytorch",
            precision="int8",
            model="model-id",
            threads=0,
            speed=1.0,
            output_path=tmp_path / f"{index}.mp3",
        )

    assert len(enrolled) == 1 and enrolled[0][1] == reference.resolve() and enrolled[0][2] is True
    voices = {call["voice"] for call in engine.calls}
    assert voices == {f"clone_{reference_identity(reference)[:12]}"}
    assert voice_reference_path("Ngọc Linh") is None
    assert voice_reference_path(str(tmp_path / "missing.wav")) is None


def test_clone_enrollment_failure_is_a_provider_error(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reference = tmp_path / "ref.wav"
    reference.write_bytes(b"x")

    class BrokenEngine(FakeEngine):
        def add_voice(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise ValueError("reference too short")

    synthesizer = VieneuSynthesizer(engine_factory=lambda **_kwargs: BrokenEngine())
    with pytest.raises(VieneuProviderError, match="could not enroll"):
        synthesizer.synthesize(
            text="beat",
            voice_id=str(reference),
            style="doc_truyen",
            backend="onnx",
            precision="int8",
            model="model-id",
            threads=0,
            speed=1.0,
            output_path=tmp_path / "out.mp3",
        )
    assert not (tmp_path / "out.mp3").exists()
