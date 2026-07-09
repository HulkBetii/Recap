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
        bgm=None,
        bgm_gain_db=-20.0,
        bgm_fade_in_s=1.5,
        bgm_fade_out_s=2.5,
        bgm_ducking="none",
        captions=False,
        review_script=None,
        beats_timing=None,
        review_micro=None,
        tts_align=None,
        captions_output=None,
        caption_font_name="Arial",
        caption_font_size=54,
        caption_margin_v=64,
        caption_outline=3,
        caption_max_chars_per_line=42,
        caption_max_lines=2,
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

def test_render_cli_bgm_and_captions_use_final_mux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    bgm = tmp_path / "bgm.mp3"
    bgm.write_bytes(b"bgm")
    args.bgm = bgm
    args.captions = True
    args.review_script = tmp_path / "review_script.json"
    args.beats_timing = tmp_path / "beats_timing.json"
    args.review_script.write_text('[{"beat_id":0,"narration":"Xin chào.","src_tc_start":0,"src_tc_end":1,"is_hook":true}]', encoding="utf-8")
    args.beats_timing.write_text('[{"beat_id":0,"audio_path":"audio/0.mp3","tl_start":0,"tl_end":2,"duration":2}]', encoding="utf-8")
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("render.__main__.probe_video_stream", lambda path: {"width":1920,"height":1080,"codec":"h264","fps":30.0,"duration":10.0})
    monkeypatch.setattr("render.__main__.probe_duration", lambda path: 2.0)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr("render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video"))
    calls = []
    monkeypatch.setattr("render.__main__.mux_final", lambda **kwargs: (calls.append(kwargs), kwargs["output_path"].write_bytes(b"recap")))
    assert run_render(args) == 0
    assert calls and calls[0]["bgm_path"] == bgm
    assert calls[0]["captions_path"].name == "captions.ass"
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert meta["bgm"]["applied"] is True
    assert meta["captions"]["enabled"] is True
    assert meta["captions"]["event_count"] >= 1
