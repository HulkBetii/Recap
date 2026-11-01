from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from common.schema import TranscriptSegment
from ingest import transcribe
from ingest.asr import align_with_whisperx


class FakeCuda:
    def __init__(self) -> None:
        self.emptied = 0

    def is_available(self) -> bool:
        return True

    def is_initialized(self) -> bool:
        return True

    def empty_cache(self) -> None:
        self.emptied += 1


def test_release_gpu_memory_empties_torch_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_torch = types.SimpleNamespace(cuda=FakeCuda())
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    transcribe.release_gpu_memory()
    assert fake_torch.cuda.emptied == 1


def test_release_gpu_memory_skips_when_torch_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    transcribe.release_gpu_memory()


def test_cuda_dll_preload_is_noop_off_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(transcribe.ctypes, "CDLL", lambda path: calls.append(path))
    transcribe.register_cuda_runtime_dlls("cpu")
    assert calls == []


def test_whisperx_alignment_releases_memory_even_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    released: list[bool] = []

    def failing_align(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("alignment exploded")

    fake_whisperx = types.SimpleNamespace(
        load_audio=lambda _path: [0.0],
        load_align_model=lambda **_kwargs: (object(), {}),
        align=failing_align,
    )
    monkeypatch.setitem(sys.modules, "whisperx", fake_whisperx)
    monkeypatch.setattr("ingest.asr.release_gpu_memory", lambda: released.append(True))
    segment = TranscriptSegment(id=0, tc_start=0.0, tc_end=1.0, ko="안녕")

    with pytest.raises(RuntimeError, match="alignment exploded"):
        align_with_whisperx([segment], audio_path=tmp_path / "a.wav", device="cuda")
    assert released == [True]
