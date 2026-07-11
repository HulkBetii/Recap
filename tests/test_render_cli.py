from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from common.media import MediaError
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


def test_render_cli_tail_pads_when_video_is_shorter_than_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("render.__main__.probe_video_stream", lambda path: {"width":1920,"height":1080,"codec":"h264","fps":30.0,"duration":10.0})

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.5
        if name == "video_only.mp4":
            return 2.0
        if name == "recap.mp4":
            return 2.5
        return 2.0

    tail_calls = []
    mux_inputs = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr("render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video"))

    def fake_tail_pad(**kwargs):  # type: ignore[no-untyped-def]
        tail_calls.append(kwargs)
        kwargs["output_path"].write_bytes(b"padded")
        return 15

    def fake_mux(video_path, voiceover_path, output_path, audio_delay_s=0.0):  # type: ignore[no-untyped-def]
        mux_inputs.append(video_path)
        output_path.write_bytes(b"recap")

    monkeypatch.setattr("render.__main__.pad_video_by_tail", fake_tail_pad)
    monkeypatch.setattr("render.__main__.pad_video_to_duration", lambda *args, **kwargs: pytest.fail("legacy padding should not run"))
    monkeypatch.setattr("render.__main__.mux_voiceover", fake_mux)
    assert run_render(args) == 0
    assert tail_calls
    assert tail_calls[0]["shortage_s"] == pytest.approx(0.5)
    assert Path(mux_inputs[0]).name == "video_only_padded.mp4"
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert any("tail-padded" in warning for warning in meta["warnings"])

def test_render_cli_skips_padding_within_tolerance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("render.__main__.probe_video_stream", lambda path: {"width":1920,"height":1080,"codec":"h264","fps":30.0,"duration":10.0})

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.05
        if name == "video_only.mp4":
            return 2.0
        if name == "recap.mp4":
            return 2.05
        return 2.0

    mux_inputs = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr("render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video"))
    monkeypatch.setattr("render.__main__.pad_video_by_tail", lambda *args, **kwargs: pytest.fail("tail padding should not run"))

    def fake_mux(video_path, voiceover_path, output_path, audio_delay_s=0.0):  # type: ignore[no-untyped-def]
        mux_inputs.append(video_path)
        output_path.write_bytes(b"recap")

    monkeypatch.setattr("render.__main__.mux_voiceover", fake_mux)
    assert run_render(args) == 0
    assert Path(mux_inputs[0]).name == "video_only.mp4"

def test_render_cli_tail_padding_includes_audio_delay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    args.audio_delay_s = 0.25
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("render.__main__.probe_video_stream", lambda path: {"width":1920,"height":1080,"codec":"h264","fps":30.0,"duration":10.0})

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.1
        if name == "video_only.mp4":
            return 2.0
        if name == "recap.mp4":
            return 2.35
        return 2.0

    tail_calls = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr("render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video"))
    monkeypatch.setattr("render.__main__.mux_voiceover", lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"))

    def fake_tail_pad(**kwargs):  # type: ignore[no-untyped-def]
        tail_calls.append(kwargs)
        kwargs["output_path"].write_bytes(b"padded")
        return 11

    monkeypatch.setattr("render.__main__.pad_video_by_tail", fake_tail_pad)
    assert run_render(args) == 0
    assert tail_calls[0]["shortage_s"] == pytest.approx(0.35)
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert any("delayed audio duration 2.350s" in warning for warning in meta["warnings"])

def test_render_cli_falls_back_to_legacy_padding_when_tail_padding_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("render.__main__.probe_video_stream", lambda path: {"width":1920,"height":1080,"codec":"h264","fps":30.0,"duration":10.0})

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.5
        if name == "video_only.mp4":
            return 2.0
        if name == "recap.mp4":
            return 2.5
        return 2.0

    legacy_calls = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr("render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video"))
    monkeypatch.setattr("render.__main__.pad_video_by_tail", lambda **kwargs: (_ for _ in ()).throw(MediaError("tail failed")))

    def fake_legacy_pad(video_path, output_path, duration_s):  # type: ignore[no-untyped-def]
        legacy_calls.append((video_path, output_path, duration_s))
        output_path.write_bytes(b"padded")

    monkeypatch.setattr("render.__main__.pad_video_to_duration", fake_legacy_pad)
    monkeypatch.setattr("render.__main__.mux_voiceover", lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"))
    assert run_render(args) == 0
    assert legacy_calls
    assert legacy_calls[0][2] == pytest.approx(2.5)
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert any("fell back to full re-encode padding" in warning for warning in meta["warnings"])

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
