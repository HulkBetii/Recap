from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from common.schema import EdlPlacement, RenderMeta
from render.cache import RenderCache
from render.compose import concat_list_text, mux_voiceover
from render.cut import RenderParams, build_video_filter, clamp_source, temp_cache_key
from render.quantize import quantize_placements


def placement(tl_start: float, tl_end: float, src_in: float | None = None, src_out: float | None = None, speed: float = 1.0) -> EdlPlacement:
    if src_in is None:
        src_in = tl_start
    if src_out is None:
        src_out = src_in + (tl_end - tl_start) * speed
    return EdlPlacement(
        tl_start=tl_start,
        tl_end=tl_end,
        src="film.mp4",
        src_in=src_in,
        src_out=src_out,
        beat_id=0,
        shot_index=0,
        reused=False,
        speed=speed,
    )


def test_render_meta_schema() -> None:
    meta = RenderMeta(
        width=1920,
        height=1080,
        fps=30,
        codec="h264",
        video_duration_s=10,
        audio_duration_s=10,
        duration_match=True,
        n_placements=2,
        n_temp_clips=2,
        created_at="2026-07-02T00:00:00Z",
    )
    assert meta.duration_match is True


def test_quantize_global_timeline_is_continuous() -> None:
    frames = quantize_placements([placement(0, 1.01), placement(1.01, 2.0, 1.01, 2.0)], fps=30)
    assert frames[0].f_start == 0
    assert frames[0].f_end == frames[1].f_start
    assert frames[-1].f_end == round(2.0 * 30)


def test_clamp_source_warns_and_never_negative() -> None:
    result = clamp_source(placement(0, 1, 8, 12, speed=4.0), film_duration=10)
    assert result.src_in == 8
    assert result.src_out == 10
    assert result.warnings


def test_temp_cache_key_changes_with_params(tmp_path: Path) -> None:
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    frame = quantize_placements([placement(0, 1)], fps=30)[0]
    source = clamp_source(frame.placement, 10)
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")
    changed = RenderParams(width=1280, height=720, fps=30, fit="cover", crf=20, preset="medium")
    assert temp_cache_key(film_path=film, frame=frame, source=source, params=params) != temp_cache_key(film_path=film, frame=frame, source=source, params=changed)


def test_render_cache_hits_existing_temp(tmp_path: Path) -> None:
    cache = RenderCache(tmp_path / "work")
    cache.prepare()
    path = cache.temp_path("abc")
    path.write_bytes(b"video")
    assert cache.get_cached_temp("abc") == path
    assert cache.cache_hits == ["temp_clips/abc.mp4"]


def test_cover_filter_and_speed_setpts() -> None:
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")
    filter_text = build_video_filter(params=params, frame_count=90, source_duration=6, target_duration=3, speed=2.0)
    assert "scale=1920:1080:force_original_aspect_ratio=increase" in filter_text
    assert "crop=1920:1080" in filter_text
    assert "fps=30" in filter_text
    assert "setpts=PTS/2.000000" in filter_text
    assert "format=yuv420p" in filter_text


def test_concat_list_text_preserves_order(tmp_path: Path) -> None:
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    text = concat_list_text([first, second])
    assert text.splitlines()[0].endswith("a.mp4'")
    assert text.splitlines()[1].endswith("b.mp4'")


def test_mux_voiceover_can_delay_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    mux_voiceover(tmp_path / "video.mp4", tmp_path / "voice.mp3", tmp_path / "out.mp4", audio_delay_s=0.25)
    command = commands[0]
    assert "-filter_complex" in command
    assert "[1:a]adelay=250:all=1[a]" in command
    assert "[a]" in command
