from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tts.vieneu_provider import VieneuProviderError, VieneuSynthesizer, validate_vieneu_settings


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
    assert all(call["style"] == "doc_truyen" for call in engine.calls)
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
        ("pytorch", "int8", "doc_truyen", 0, "backend=onnx"),
        ("onnx", "float16", "doc_truyen", 0, "precision"),
        ("onnx", "int8", "dramatic", 0, "style"),
        ("onnx", "int8", "doc_truyen", -1, "threads"),
    ],
)
def test_vieneu_settings_are_strict(backend, precision, style, threads, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(VieneuProviderError, match=message):
        validate_vieneu_settings(backend=backend, precision=precision, style=style, threads=threads)
