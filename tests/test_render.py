from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from common.schema import EdlPlacement, RenderMeta
from render.cache import RenderCache
from render.captions import CaptionStyle, build_caption_events, escape_ass_filter_path, write_ass
from render.compose import build_bgm_audio_filter, concat_list_text, mux_final, mux_voiceover
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

def test_caption_source_prefers_micro_over_parent(tmp_path: Path) -> None:
    review = tmp_path / "review_script.json"
    timing = tmp_path / "beats_timing.json"
    micro = tmp_path / "review_script.micro.json"
    review.write_text('[{"beat_id":0,"narration":"Parent text.","src_tc_start":0,"src_tc_end":1,"is_hook":true}]', encoding="utf-8")
    timing.write_text('[{"beat_id":0,"audio_path":"audio/0.mp3","tl_start":0,"tl_end":2,"duration":2}]', encoding="utf-8")
    micro.write_text('[{"beat_id":0,"parent_beat_id":0,"sub_beat_id":0,"narration":"Micro text.","tl_start":0.5,"tl_end":1.5,"src_tc_start":0,"src_tc_end":1,"duration":1,"alignment_method":"whisperx","is_hook":true}]', encoding="utf-8")
    result = build_caption_events(review_script=review, beats_timing=timing, review_micro=micro, tts_align=None, style=CaptionStyle())
    assert result.source == "review_micro"
    assert result.events[0].text == "Micro text."
    assert result.events[0].start == 0.5

def test_write_ass_escapes_vietnamese_and_braces(tmp_path: Path) -> None:
    output = tmp_path / "captions.ass"
    write_ass(output, events=[type("E", (), {"start": 0.0, "end": 1.25, "text": "Tiếng Việt {test}\\ok"})()], width=1920, height=1080, style=CaptionStyle())
    text = output.read_text(encoding="utf-8")
    assert "Fontname" in text
    assert r"Tiếng Việt \{test\}\\ok" in text

def test_escape_ass_filter_path_handles_windows_drive() -> None:
    escaped = escape_ass_filter_path(Path("D:/VibeCoding/Recap/runs/x/captions.ass"))
    assert "D\\:" in escaped or "D\\\\:" in escaped
    assert "captions.ass" in escaped

def test_bgm_filter_has_trim_fade_and_amix() -> None:
    text = build_bgm_audio_filter(audio_duration_s=12.0, gain_db=-20.0, fade_in_s=1.5, fade_out_s=2.5, ducking="none")
    assert "atrim=0:12.000000" in text
    assert "afade=t=in:st=0:d=1.500" in text
    assert "afade=t=out:st=9.500:d=2.500" in text
    assert "amix=inputs=2:duration=first:normalize=0" in text

def test_mux_final_bgm_only_copies_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    mux_final(video_path=tmp_path / "video.mp4", voiceover_path=tmp_path / "voice.mp3", output_path=tmp_path / "out.mp4", audio_duration_s=3.0, bgm_path=tmp_path / "bgm.mp3")
    command = commands[0]
    assert "-stream_loop" in command
    assert "-c:v" in command
    assert command[command.index("-c:v") + 1] == "copy"
    assert any("amix=inputs=2:duration=first:normalize=0" in item for item in command)

def test_mux_final_captions_reencodes_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    captions = tmp_path / "captions.ass"
    captions.write_text("", encoding="utf-8")
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    mux_final(video_path=tmp_path / "video.mp4", voiceover_path=tmp_path / "voice.mp3", output_path=tmp_path / "out.mp4", audio_duration_s=3.0, captions_path=captions)
    command = commands[0]
    assert command[command.index("-c:v") + 1] == "libx264"
    assert any("ass='" in item for item in command)
