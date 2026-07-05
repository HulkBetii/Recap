from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from render.__main__ import RenderError, run_render


def write_edl(tmp_path: Path) -> Path:
    edl = [
        {"tl_start":0,"tl_end":1,"src":"film.mp4","src_in":0,"src_out":1,"beat_id":0,"shot_index":0,"reused":False,"speed":1.0},
        {"tl_start":1,"tl_end":2,"src":"film.mp4","src_in":1,"src_out":2,"beat_id":1,"shot_index":1,"reused":False,"speed":1.0},
    ]
    path = tmp_path / "edl.json"
    path.write_text(json.dumps(edl), encoding="utf-8")
    return path


def make_args(tmp_path: Path, force: bool = False) -> argparse.Namespace:
    edl = write_edl(tmp_path)
    film = tmp_path / "film.mp4"
    voice = tmp_path / "voiceover.mp3"
    film.write_bytes(b"film")
    voice.write_bytes(b"voice")
    return argparse.Namespace(
        edl=edl,
        voiceover=voice,
        film=film,
        output=tmp_path / "recap.mp4",
        width=1920,
        height=1080,
        fps=30.0,
        fit="cover",
        crf=20,
        preset="medium",
        concurrency=2,
        audio_delay_s=0.0,
        work_dir=tmp_path / "work" / "render",
        force=force,
        log_level="ERROR",
    )


def test_render_cli_missing_input_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    args.voiceover.unlink()
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    with pytest.raises(RenderError, match="voiceover file does not exist"):
        run_render(args)


def test_render_cli_outputs_meta_and_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("render.__main__.probe_video_stream", lambda path: {"width":1920,"height":1080,"codec":"h264","fps":30.0,"duration":10.0})
    monkeypatch.setattr("render.__main__.probe_duration", lambda path: 2.0)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)

    def fake_cut(**kwargs):  # type: ignore[no-untyped-def]
        kwargs["output_path"].write_bytes(b"temp")

    def fake_concat(temp_paths, output_path, work_dir):  # type: ignore[no-untyped-def]
        output_path.write_bytes(b"video")
        return work_dir / "concat.txt"

    def fake_mux(video_path, voiceover_path, output_path, audio_delay_s=0.0):  # type: ignore[no-untyped-def]
        output_path.write_bytes(b"recap")

    monkeypatch.setattr("render.__main__.cut_temp_clip", fake_cut)
    monkeypatch.setattr("render.__main__.concat_video", fake_concat)
    monkeypatch.setattr("render.__main__.mux_voiceover", fake_mux)
    assert run_render(args) == 0
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert meta["duration_match"] is True
    assert meta["n_temp_clips"] == 2

    assert run_render(args) == 0
    cached_meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert len(cached_meta["cache_hits"]) == 2


def test_render_cli_duration_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("render.__main__.probe_video_stream", lambda path: {"width":1920,"height":1080,"codec":"h264","fps":30.0,"duration":10.0})
    monkeypatch.setattr("render.__main__.probe_duration", lambda path: 2.5 if Path(path).name == "recap.mp4" else 2.0)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr("render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video"))
    monkeypatch.setattr("render.__main__.mux_voiceover", lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"))
    assert run_render(args) == 0
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert meta["duration_match"] is False
    assert meta["warnings"]
