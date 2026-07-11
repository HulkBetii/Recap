from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from ingest.transcribe import transcribe_korean


class FakeWhisperModel:
    calls = 0

    def __init__(self, model_name: str, device: str) -> None:
        self.model_name = model_name
        self.device = device

    def transcribe(self, audio_path: str, **kwargs):  # type: ignore[no-untyped-def]
        FakeWhisperModel.calls += 1
        name = Path(audio_path).name
        if name.startswith("chunk-0000"):
            segments = [
                SimpleNamespace(start=1.0, end=2.0, text="first"),
                SimpleNamespace(start=8.5, end=9.5, text="boundary hello"),
            ]
        else:
            segments = [
                SimpleNamespace(start=0.0, end=0.6, text="boundary hello"),
                SimpleNamespace(start=2.0, end=3.0, text="second"),
            ]
        return segments, None


def install_fake_whisper(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisperModel))


def fake_extract(_audio: Path, chunk_path: Path, _start: float, _length: float) -> None:
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_path.write_bytes(b"wav" * 32)


def test_local_asr_overlap_dedupes_boundary_and_reuses_chunk_cache(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    install_fake_whisper(monkeypatch)
    monkeypatch.setattr("ingest.transcribe._extract_audio_chunk", fake_extract)
    FakeWhisperModel.calls = 0
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"source")
    chunks = tmp_path / "chunks"

    first = transcribe_korean(
        audio,
        "mock",
        "cpu",
        language="vi",
        duration=18.0,
        chunks_dir=chunks,
        chunk_s=10.0,
    )

    assert [segment.ko for segment in first] == ["first", "boundary hello", "second"]
    assert FakeWhisperModel.calls == 2
    second = transcribe_korean(
        audio,
        "mock",
        "cpu",
        language="vi",
        duration=18.0,
        chunks_dir=chunks,
        chunk_s=10.0,
    )
    assert [segment.model_dump() for segment in second] == [segment.model_dump() for segment in first]
    assert FakeWhisperModel.calls == 2


def test_local_asr_corrupt_cache_rebuilds_only_affected_chunk(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    install_fake_whisper(monkeypatch)
    monkeypatch.setattr("ingest.transcribe._extract_audio_chunk", fake_extract)
    FakeWhisperModel.calls = 0
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"source")
    chunks = tmp_path / "chunks"
    transcribe_korean(audio, "mock", "cpu", duration=18.0, chunks_dir=chunks, chunk_s=10.0)
    json_paths = sorted(chunks.glob("*.json"))
    json_paths[0].write_text("{broken", encoding="utf-8")

    transcribe_korean(audio, "mock", "cpu", duration=18.0, chunks_dir=chunks, chunk_s=10.0)

    assert FakeWhisperModel.calls == 3
    assert not list(chunks.glob("*.tmp"))
