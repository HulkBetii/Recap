from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from common.integrity import media_identity_hash
from common.schema import AudioAssets
from reaction_remix.stems.__main__ import StemsError, run_stems
from tests.reaction_factories import make_source


def test_stems_off_writes_deterministic_tts_only_assets(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    source_path = tmp_path / "reaction_source.json"
    source = make_source(input_path=film.as_posix(), input_hash=media_identity_hash(film))
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    output = tmp_path / "audio_assets.json"
    monkeypatch.setattr("reaction_remix.stems.__main__.require_ffmpeg", lambda: None)

    result = run_stems(
        argparse.Namespace(
            film=film,
            source=source_path,
            output=output,
            work_dir=tmp_path / "work",
            provider="off",
            model="htdemucs",
            device="cpu",
            force=False,
        )
    )

    assert result == 0
    assets = AudioAssets.model_validate_json(output.read_text(encoding="utf-8"))
    assert assets.items == []
    assert "TTS-only" in assets.warnings[0]


def test_demucs_failure_is_a_hard_production_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    source_path = tmp_path / "reaction_source.json"
    source = make_source(input_path=film.as_posix(), input_hash=media_identity_hash(film))
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("reaction_remix.stems.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "reaction_remix.stems.__main__.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "CUDA out of memory"),
    )

    with pytest.raises(StemsError, match="required Demucs separation failed"):
        run_stems(
            argparse.Namespace(
                film=film,
                source=source_path,
                output=tmp_path / "audio_assets.json",
                work_dir=tmp_path / "work",
                provider="demucs",
                model="htdemucs",
                device="cuda",
                force=False,
            )
        )


def test_demucs_preserves_unicode_source_path_and_uses_utf8_stdio(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "【海外の反応】日本旅行.mp4"
    film.write_bytes(b"film")
    source_path = tmp_path / "reaction_source.json"
    source = make_source(input_path=film.as_posix(), input_hash=media_identity_hash(film))
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    output = tmp_path / "audio_assets.json"
    work_dir = tmp_path / "work"
    monkeypatch.setattr("reaction_remix.stems.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("reaction_remix.stems.__main__.probe_duration", lambda _path: source.duration_s)
    monkeypatch.setattr("reaction_remix.stems.__main__._probe_audio", lambda _path: (44100, 2))

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        assert command[-1] == str(film.resolve())
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        stem = work_dir.resolve() / "demucs" / "htdemucs" / film.stem / "no_vocals.wav"
        stem.parent.mkdir(parents=True)
        stem.write_bytes(b"stem")
        return subprocess.CompletedProcess(command, 0, "Separated 日本旅行", "")

    monkeypatch.setattr("reaction_remix.stems.__main__.subprocess.run", fake_run)

    result = run_stems(
        argparse.Namespace(
            film=film,
            source=source_path,
            output=output,
            work_dir=work_dir,
            provider="demucs",
            model="htdemucs",
            device="cpu",
            force=False,
        )
    )

    assert result == 0
    assets = AudioAssets.model_validate_json(output.read_text(encoding="utf-8"))
    assert Path(assets.items[0].path).parent.name == film.stem
