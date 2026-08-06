from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from common.media import MediaError, probe_audio_stream_count
from common.schema import EditPlan, EdlPlacement, PlacementEdit, RenderMeta
from render.audio import build_master_mix, build_music_bed, build_sfx_bed, resolve_audio_assets
from render.cache import RenderCache
from render.compose import (
    concat_list_text,
    concat_video,
    mux_master_audio,
    mux_voiceover,
    pad_video_by_tail,
    pad_video_to_duration,
    tail_pad_frame_count,
)
from render.cut import (
    RenderParams,
    build_enhanced_video_filter,
    build_video_filter,
    clamp_source,
    cut_temp_clip,
    required_enhanced_source_duration,
    temp_cache_key,
)
from render.quantize import QuantizeError, quantize_placements


def placement(
    tl_start: float, tl_end: float, src_in: float | None = None, src_out: float | None = None, speed: float = 1.0
) -> EdlPlacement:
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


def test_probe_audio_stream_count_reports_all_streams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"streams": [{"index": 1}, {"index": 2}]})
    monkeypatch.setattr(
        "common.media.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=payload, stderr=""),
    )
    assert probe_audio_stream_count(tmp_path / "video.mp4") == 2


def test_quantize_global_timeline_is_continuous() -> None:
    frames = quantize_placements([placement(0, 1.01), placement(1.01, 2.0, 1.01, 2.0)], fps=30)
    assert frames[0].f_start == 0
    assert frames[0].f_end == frames[1].f_start
    assert frames[-1].f_end == round(2.0 * 30)


def test_quantize_absorbs_small_timeline_gap_within_render_tolerance() -> None:
    frames = quantize_placements([placement(0, 1.0), placement(1.05, 2.05, 1.05, 2.05)], fps=30)
    assert frames[0].f_end == frames[1].f_start
    assert frames[-1].f_end == round(2.05 * 30)


def test_quantize_rejects_large_timeline_gap() -> None:
    with pytest.raises(QuantizeError, match="timeline has frame gap or overlap"):
        quantize_placements([placement(0, 1.0), placement(1.08, 2.08, 1.08, 2.08)], fps=30)


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
    assert temp_cache_key(film_path=film, frame=frame, source=source, params=params) != temp_cache_key(
        film_path=film, frame=frame, source=source, params=changed
    )


def test_render_cache_hits_existing_temp(tmp_path: Path) -> None:
    cache = RenderCache(tmp_path / "work")
    cache.prepare()
    path = cache.temp_path("abc")
    path.write_bytes(b"video")
    assert cache.get_cached_temp("abc") == path
    assert cache.cache_hits == ["temp_clips/abc.mp4"]


def test_render_cache_rejects_invalid_existing_temp(tmp_path: Path) -> None:
    cache = RenderCache(tmp_path / "work")
    cache.prepare()
    path = cache.temp_path("abc")
    path.write_bytes(b"partial mp4")
    assert cache.get_cached_temp("abc", validator=lambda candidate: candidate.stat().st_size > 100) is None
    assert cache.cache_hits == []


def test_render_cache_tracks_audio_separately(tmp_path: Path) -> None:
    cache = RenderCache(tmp_path / "work")
    cache.prepare()
    path = cache.audio_path("music", "abc")
    path.write_bytes(b"audio")
    assert cache.get_cached_audio("music", "abc") == path
    assert cache.cache_hits == ["audio_cache/music-abc.m4a"]


def test_cut_temp_clip_commits_output_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "clip.mp4"
    frame = quantize_placements([placement(0, 1)], fps=30)[0]
    source = clamp_source(frame.placement, 10)
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")
    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"complete")

    monkeypatch.setattr("render.cut.run_command", fake_run)
    cut_temp_clip(
        film_path=tmp_path / "film.mp4",
        output_path=output,
        frame=frame,
        source=source,
        params=params,
    )
    assert output.read_bytes() == b"complete"
    assert commands[0].index("-t") < commands[0].index("-i")
    assert not list(tmp_path.glob("*.partial.mp4"))


def test_cut_temp_clip_failure_preserves_existing_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"previous")
    frame = quantize_placements([placement(0, 1)], fps=30)[0]
    source = clamp_source(frame.placement, 10)
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")

    def fake_run(command: list[str]) -> None:
        Path(command[-1]).write_bytes(b"partial")
        raise MediaError("interrupted")

    monkeypatch.setattr("render.cut.run_command", fake_run)
    with pytest.raises(MediaError, match="interrupted"):
        cut_temp_clip(
            film_path=tmp_path / "film.mp4",
            output_path=output,
            frame=frame,
            source=source,
            params=params,
        )
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.partial.mp4"))


def test_cover_filter_and_speed_setpts() -> None:
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")
    filter_text = build_video_filter(params=params, frame_count=90, source_duration=6, target_duration=3, speed=2.0)
    assert "scale=1920:1080:force_original_aspect_ratio=increase" in filter_text
    assert "crop=1920:1080" in filter_text
    assert "fps=30" in filter_text
    assert "setpts=PTS/2.000000" in filter_text
    assert filter_text.index("setpts=PTS/2.000000") < filter_text.index("fps=30")
    assert "tpad=stop_mode=clone:stop=90" in filter_text
    assert "trim=end_frame=90" in filter_text
    assert "trim=duration" not in filter_text
    assert "format=yuv420p" in filter_text


def test_enhanced_filter_is_frame_locked_and_applies_visual_directives() -> None:
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")
    edit = PlacementEdit(
        placement_index=0,
        beat_id=0,
        mood="action",
        role="transition",
        src_in=0,
        src_out=3,
        zoom_start=1.02,
        zoom_end=1.08,
        pan_x=0.02,
        aspect_ratio=2.35,
        contrast=1.1,
        saturation=1.12,
        warmth=0.02,
        speed_ramp=[
            {"start_ratio": 0.0, "end_ratio": 0.6, "speed": 1.0},
            {"start_ratio": 0.6, "end_ratio": 0.8, "speed": 1.15},
            {"start_ratio": 0.8, "end_ratio": 1.0, "speed": 1.35},
        ],
        freeze_duration_s=0.45,
    )
    filter_text = build_enhanced_video_filter(params=params, frame_count=90, fallback_speed=1.0, edit=edit)
    assert "split=3" in filter_text
    assert "setpts=(PTS-STARTPTS)/1.150000" in filter_text
    assert "setpts=(PTS-STARTPTS)/1.350000" in filter_text
    assert "tpad=stop_mode=clone:stop=14,trim=end_frame=90" in filter_text
    assert "zoompan=" in filter_text
    assert "eq=contrast=1.100000:saturation=1.120000" in filter_text
    assert "colorbalance=rs=0.020000:bs=-0.020000" in filter_text
    assert "drawbox=x=0:y=0" in filter_text
    assert filter_text.endswith("format=yuv420p[vout]")
    assert required_enhanced_source_duration(frame_count=90, fps=30, edit=edit, fallback_speed=1.0) == pytest.approx(
        2.7833333333
    )


def test_temp_cache_key_changes_with_edit_plan(tmp_path: Path) -> None:
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    frame = quantize_placements([placement(0, 1)], fps=30)[0]
    source = clamp_source(frame.placement, 10)
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")
    edit = PlacementEdit(placement_index=0, beat_id=0, src_in=0, src_out=1, zoom_start=1.02, zoom_end=1.05)
    assert temp_cache_key(film_path=film, frame=frame, source=source, params=params) != temp_cache_key(
        film_path=film, frame=frame, source=source, params=params, edit=edit
    )


def test_concat_list_text_preserves_order(tmp_path: Path) -> None:
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    text = concat_list_text([first, second])
    assert text.splitlines()[0].endswith("a.mp4'")
    assert text.splitlines()[1].endswith("b.mp4'")


def test_concat_video_can_use_custom_list_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    list_file = concat_video([first, second], tmp_path / "out.mp4", tmp_path / "work", list_filename="concat_pad.txt")
    assert list_file == tmp_path / "work" / "concat_pad.txt"
    assert list_file.read_text(encoding="utf-8") == concat_list_text([first, second])
    assert str(list_file) in commands[0]


def test_tail_pad_frame_count_ceilings_to_whole_frames() -> None:
    assert tail_pad_frame_count(1.524, 30) == 46
    assert tail_pad_frame_count(0.001, 30) == 1


def test_pad_video_by_tail_extracts_frame_encodes_tail_and_concats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = []
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    params = RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium")
    pad_frames = pad_video_by_tail(
        video_path=tmp_path / "video_only.mp4",
        output_path=tmp_path / "video_only_padded.mp4",
        work_dir=tmp_path / "work",
        shortage_s=1.524,
        params=params,
    )
    assert pad_frames == 46
    assert commands[0][:4] == ["ffmpeg", "-y", "-sseof", "-0.1"]
    assert "-frames:v" in commands[1]
    assert commands[1][commands[1].index("-frames:v") + 1] == "46"
    assert "scale=1920:1080,fps=30,format=yuv420p" in commands[1]
    assert str(tmp_path / "work" / "concat_pad.txt") in commands[2]


def test_legacy_padding_scales_tpad_to_requested_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    pad_video_to_duration(tmp_path / "video.mp4", tmp_path / "padded.mp4", 25.5)
    command = commands[0]
    assert "tpad=stop_mode=clone:stop_duration=25.500000" in command


def test_mux_voiceover_can_delay_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    mux_voiceover(tmp_path / "video.mp4", tmp_path / "voice.mp3", tmp_path / "out.mp4", audio_delay_s=0.25)
    command = commands[0]
    assert "-filter_complex" in command
    assert "[1:a]adelay=250:all=1[a]" in command
    assert "[a]" in command


def test_mux_master_audio_maps_only_video_and_master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setattr("render.compose.run_command", lambda command: commands.append(command))
    mux_master_audio(tmp_path / "video.mp4", tmp_path / "master.m4a", tmp_path / "out.mp4")
    command = commands[0]
    assert command.count("-map") == 2
    assert "0:v:0" in command
    assert "1:a:0" in command
    assert "0:a" not in " ".join(command)


def test_enhanced_audio_builders_loop_fade_duck_and_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    music = tmp_path / "music.mp3"
    whoosh = tmp_path / "whoosh.wav"
    music.write_bytes(b"music")
    whoosh.write_bytes(b"sfx")
    manifest_path = tmp_path / "audio_assets.yaml"
    manifest = {
        "version": 1,
        "assets": [
            {
                "asset_id": "music",
                "path": "music.mp3",
                "kind": "music",
                "mood": "default",
                "loopable": True,
                "license_source": "test",
            },
            {
                "asset_id": "whoosh",
                "path": "whoosh.wav",
                "kind": "sfx",
                "sfx_kind": "whoosh",
                "license_source": "test",
            },
        ],
    }
    from common.schema import AudioAssetManifest

    parsed_manifest = AudioAssetManifest.model_validate(manifest)
    assets = resolve_audio_assets(parsed_manifest, manifest_path)
    plan = EditPlan(
        seed=1,
        total_duration_s=4,
        placements=[PlacementEdit(placement_index=0, beat_id=0, src_in=0, src_out=4)],
        music_cues=[
            {
                "asset_id": "music",
                "tl_start": 0,
                "tl_end": 2,
                "mood": "default",
                "crossfade_s": 0,
                "gain_db": -24,
            },
            {
                "asset_id": "music",
                "tl_start": 2,
                "tl_end": 4,
                "mood": "default",
                "crossfade_s": 1.5,
                "gain_db": -24,
            },
        ],
        sfx_cues=[{"asset_id": "whoosh", "kind": "whoosh", "tl_start": 1, "gain_db": -14}],
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"audio")

    monkeypatch.setattr("render.audio.run_command", fake_run)
    music_bed = tmp_path / "music.m4a"
    sfx_bed = tmp_path / "sfx.m4a"
    master = tmp_path / "master.m4a"
    voiceover = tmp_path / "voice.mp3"
    voiceover.write_bytes(b"voice")
    build_music_bed(plan=plan, assets=assets, output_path=music_bed, duration_s=4)
    build_sfx_bed(cues=plan.sfx_cues, assets=assets, output_path=sfx_bed, duration_s=4)
    build_master_mix(
        voiceover_path=voiceover,
        music_path=music_bed,
        sfx_path=sfx_bed,
        output_path=master,
        plan=plan,
        duration_s=4,
        audio_delay_s=0.25,
    )
    assert "-stream_loop" in commands[0]
    assert "afade=t=out" in commands[0][commands[0].index("-filter_complex") + 1]
    assert "adelay=1000:all=1" in commands[1][commands[1].index("-filter_complex") + 1]
    master_filter = commands[2][commands[2].index("-filter_complex") + 1]
    assert "adelay=250:all=1" in master_filter
    assert "sidechaincompress=" in master_filter
    assert "loudnorm=I=-14:TP=-1:LRA=11" in master_filter
    assert "alimiter=limit=0.891251" in master_filter
