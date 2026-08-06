from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from common.media import MediaError
from common.schema import EdlPlacement, PlacementEdit
from render.audio import EnhancedAudioResult
from render.cache import RenderCache
from render.cut import RenderParams
from render.quantize import quantize_placements
from render.__main__ import RenderError, render_temp_clips, run_render


def write_edl(tmp_path: Path) -> Path:
    edl = [
        {
            "tl_start": 0,
            "tl_end": 1,
            "src": "film.mp4",
            "src_in": 0,
            "src_out": 1,
            "beat_id": 0,
            "shot_index": 0,
            "reused": False,
            "speed": 1.0,
        },
        {
            "tl_start": 1,
            "tl_end": 2,
            "src": "film.mp4",
            "src_in": 1,
            "src_out": 2,
            "beat_id": 1,
            "shot_index": 1,
            "reused": False,
            "speed": 1.0,
        },
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
        source_map=None,
        output=tmp_path / "recap.mp4",
        width=1920,
        height=1080,
        fps=30.0,
        fit="cover",
        crf=20,
        preset="medium",
        concurrency=2,
        audio_delay_s=0.0,
        edit_plan=None,
        audio_assets=None,
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


def test_render_cli_requires_both_enhanced_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    args.edit_plan = tmp_path / "edit_plan.json"
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    with pytest.raises(RenderError, match="must be provided together"):
        run_render(args)


@pytest.mark.parametrize(("field", "value"), [("src_in", 0.1), ("src_out", 1.1)])
def test_render_cli_rejects_tampered_edit_plan_source_span_before_media_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: float,
) -> None:
    args = make_args(tmp_path)
    args.edit_plan = tmp_path / "edit_plan.json"
    args.audio_assets = tmp_path / "missing-audio-assets.yaml"
    first_edit = {"placement_index": 0, "beat_id": 0, "src_in": 0, "src_out": 1}
    first_edit[field] = value
    args.edit_plan.write_text(
        json.dumps(
            {
                "seed": 1,
                "total_duration_s": 2,
                "placements": [
                    first_edit,
                    {"placement_index": 1, "beat_id": 1, "src_in": 1, "src_out": 2},
                ],
                "music_cues": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: pytest.fail("tampered edit plan must fail before probing source media"),
    )
    with pytest.raises(RenderError, match="source span does not match EDL base placement"):
        run_render(args)


def test_render_temp_clips_skips_ramp_when_actual_source_has_no_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "film.mp4"
    source_path.write_bytes(b"film")
    placement = EdlPlacement(
        tl_start=0,
        tl_end=1,
        src="film.mp4",
        src_in=0,
        src_out=1,
        beat_id=0,
        shot_index=0,
        reused=False,
        speed=1,
    )
    edit = PlacementEdit(
        placement_index=0,
        beat_id=0,
        src_in=0,
        src_out=1,
        source_extension_s=0.2,
        speed_ramp=[
            {"start_ratio": 0, "end_ratio": 0.6, "speed": 1},
            {"start_ratio": 0.6, "end_ratio": 0.8, "speed": 1.15},
            {"start_ratio": 0.8, "end_ratio": 1, "speed": 1.35},
        ],
    )
    passed_edits = []

    def fake_cut(**kwargs):  # type: ignore[no-untyped-def]
        passed_edits.append(kwargs["edit"])
        kwargs["output_path"].write_bytes(b"temp")

    monkeypatch.setattr("render.__main__.cut_temp_clip", fake_cut)
    cache = RenderCache(tmp_path / "work")
    cache.prepare()
    _, warnings = render_temp_clips(
        source_paths={"film.mp4": source_path},
        source_durations={"film.mp4": 1.0},
        frames=quantize_placements([placement], 30),
        params=RenderParams(width=1920, height=1080, fps=30, fit="cover", crf=20, preset="medium"),
        cache=cache,
        concurrency=1,
        edits=[edit],
    )
    assert passed_edits[0].speed_ramp == []
    assert any("speed ramp was skipped" in warning for warning in warnings)


def test_render_cli_outputs_meta_and_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)

    def fake_probe_video_stream(path):  # type: ignore[no-untyped-def]
        duration = 1.0 if Path(path).parent.name == "temp_clips" else 10.0
        return {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": duration}

    monkeypatch.setattr("render.__main__.probe_video_stream", fake_probe_video_stream)
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


def test_render_cli_enhanced_path_uses_master_audio_and_reports_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = make_args(tmp_path)
    music = tmp_path / "music.mp3"
    whoosh = tmp_path / "whoosh.wav"
    impact = tmp_path / "impact.wav"
    for path in (music, whoosh, impact):
        path.write_bytes(b"audio")
    args.edit_plan = tmp_path / "edit_plan.json"
    args.audio_assets = tmp_path / "audio_assets.yaml"
    args.edit_plan.write_text(
        json.dumps(
            {
                "version": 1,
                "profile": "dynamic_anime",
                "seed": 7,
                "total_duration_s": 2,
                "placements": [
                    {
                        "placement_index": 0,
                        "beat_id": 0,
                        "mood": "action",
                        "role": "transition",
                        "src_in": 0,
                        "src_out": 1,
                        "source_extension_s": 0.2,
                        "zoom_start": 1.02,
                        "zoom_end": 1.05,
                        "contrast": 1.1,
                        "saturation": 1.12,
                        "speed_ramp": [
                            {"start_ratio": 0, "end_ratio": 0.6, "speed": 1},
                            {"start_ratio": 0.6, "end_ratio": 0.8, "speed": 1.15},
                            {"start_ratio": 0.8, "end_ratio": 1, "speed": 1.35},
                        ],
                    },
                    {
                        "placement_index": 1,
                        "beat_id": 1,
                        "mood": "suspense",
                        "role": "reveal",
                        "src_in": 1,
                        "src_out": 2,
                        "zoom_start": 1.02,
                        "zoom_end": 1.08,
                        "aspect_ratio": 2.35,
                        "freeze_duration_s": 0.45,
                    },
                ],
                "music_cues": [{"asset_id": "music", "tl_start": 0, "tl_end": 2, "mood": "action", "gain_db": -24}],
                "sfx_cues": [
                    {"asset_id": "whoosh", "kind": "whoosh", "tl_start": 0.8, "gain_db": -14},
                    {"asset_id": "impact", "kind": "impact", "tl_start": 1.55, "gain_db": -10},
                ],
                "original_audio_included": False,
            }
        ),
        encoding="utf-8",
    )
    args.audio_assets.write_text(
        """version: 1
assets:
  - asset_id: music
    path: music.mp3
    kind: music
    mood: default
    loopable: true
    license_source: test
  - asset_id: whoosh
    path: whoosh.wav
    kind: sfx
    sfx_kind: whoosh
    license_source: test
  - asset_id: impact
    path: impact.wav
    kind: sfx
    sfx_kind: impact
    license_source: test
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)

    def fake_probe_video_stream(path):  # type: ignore[no-untyped-def]
        duration = 1.0 if Path(path).parent.name == "temp_clips" else 2.0
        return {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": duration}

    monkeypatch.setattr("render.__main__.probe_video_stream", fake_probe_video_stream)
    monkeypatch.setattr("render.__main__.probe_duration", lambda path: 2.0)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.probe_audio_stream_count", lambda path: 1)
    cut_edits = []

    def fake_cut(**kwargs):  # type: ignore[no-untyped-def]
        cut_edits.append(kwargs["edit"])
        kwargs["output_path"].write_bytes(b"temp")

    monkeypatch.setattr("render.__main__.cut_temp_clip", fake_cut)
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    master = tmp_path / "master.m4a"
    master.write_bytes(b"master")
    monkeypatch.setattr(
        "render.__main__.render_enhanced_audio",
        lambda **kwargs: EnhancedAudioResult(master_path=master, duration_s=2),
    )
    mux_calls = []

    def fake_master_mux(video_path, master_audio_path, output_path):  # type: ignore[no-untyped-def]
        mux_calls.append((video_path, master_audio_path))
        output_path.write_bytes(b"recap")

    monkeypatch.setattr("render.__main__.mux_master_audio", fake_master_mux)
    monkeypatch.setattr("render.__main__.mux_voiceover", lambda *args, **kwargs: pytest.fail("legacy mux must not run"))
    assert run_render(args) == 0
    assert [edit.source_extension_s for edit in cut_edits] == [0.2, 0.0]
    assert mux_calls[0][1] == master
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert meta["n_zoom"] == 2
    assert meta["n_aspect"] == 1
    assert meta["n_speed_ramp"] == 1
    assert meta["n_freeze"] == 1
    assert meta["n_music_cues"] == 1
    assert meta["n_sfx_cues"] == 2
    assert meta["audio_stream_count"] == 1
    assert meta["original_audio_included"] is False


def test_render_cli_enhanced_rejects_multiple_audio_streams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    args.edit_plan = tmp_path / "edit_plan.json"
    args.audio_assets = tmp_path / "audio_assets.yaml"
    args.edit_plan.write_text(
        json.dumps(
            {
                "seed": 1,
                "total_duration_s": 2,
                "placements": [
                    {"placement_index": 0, "beat_id": 0, "src_in": 0, "src_out": 1},
                    {"placement_index": 1, "beat_id": 1, "src_in": 1, "src_out": 2},
                ],
                "music_cues": [{"asset_id": "music", "tl_start": 0, "tl_end": 2, "mood": "default", "gain_db": -24}],
            }
        ),
        encoding="utf-8",
    )
    args.audio_assets.write_text(
        """assets:
  - asset_id: music
    path: music.mp3
    kind: music
    mood: default
    loopable: true
    license_source: test
""",
        encoding="utf-8",
    )
    (tmp_path / "music.mp3").write_bytes(b"music")
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 2.0},
    )
    monkeypatch.setattr("render.__main__.probe_duration", lambda path: 2.0)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.probe_audio_stream_count", lambda path: 2)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    master = tmp_path / "master.m4a"
    master.write_bytes(b"master")
    monkeypatch.setattr(
        "render.__main__.render_enhanced_audio",
        lambda **kwargs: EnhancedAudioResult(master_path=master, duration_s=2),
    )
    monkeypatch.setattr(
        "render.__main__.mux_master_audio",
        lambda video_path, master_audio_path, output_path: output_path.write_bytes(b"recap"),
    )
    with pytest.raises(RenderError, match="exactly one audio stream; found 2"):
        run_render(args)


def test_render_cli_rejects_truncated_concat_before_tail_padding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)

    def fake_probe_video_stream(path):  # type: ignore[no-untyped-def]
        if Path(path).name == "video_only.mp4":
            return {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 0.7, "frame_count": 21}
        return {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0, "frame_count": None}

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        return 0.7 if Path(path).name == "video_only.mp4" else 2.5

    monkeypatch.setattr("render.__main__.probe_video_stream", fake_probe_video_stream)
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    monkeypatch.setattr(
        "render.__main__.pad_video_by_tail",
        lambda **kwargs: pytest.fail("tail padding must not hide a truncated concat"),
    )

    with pytest.raises(RenderError, match="concat frame count mismatch"):
        run_render(args)


def test_render_cli_tail_pads_when_video_is_shorter_than_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0},
    )

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.5
        if name == "video_only.mp4":
            return 2.0
        if name == "video_only_padded.mp4":
            return 2.5
        if name == "recap.mp4":
            return 2.5
        return 2.0

    tail_calls = []
    mux_inputs = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )

    def fake_tail_pad(**kwargs):  # type: ignore[no-untyped-def]
        tail_calls.append(kwargs)
        kwargs["output_path"].write_bytes(b"padded")
        return 15

    def fake_mux(video_path, voiceover_path, output_path, audio_delay_s=0.0):  # type: ignore[no-untyped-def]
        mux_inputs.append(video_path)
        output_path.write_bytes(b"recap")

    monkeypatch.setattr("render.__main__.pad_video_by_tail", fake_tail_pad)
    monkeypatch.setattr(
        "render.__main__.pad_video_to_duration", lambda *args, **kwargs: pytest.fail("legacy padding should not run")
    )
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
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0},
    )

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.05
        if name == "video_only.mp4":
            return 2.0
        if name == "video_only_padded.mp4":
            return 2.35
        if name == "recap.mp4":
            return 2.05
        return 2.0

    mux_inputs = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    monkeypatch.setattr(
        "render.__main__.pad_video_by_tail", lambda *args, **kwargs: pytest.fail("tail padding should not run")
    )

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
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0},
    )

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.1
        if name == "video_only.mp4":
            return 2.0
        if name == "video_only_padded.mp4":
            return 2.5
        if name == "recap.mp4":
            return 2.35
        return 2.0

    tail_calls = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    monkeypatch.setattr(
        "render.__main__.mux_voiceover",
        lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"),
    )

    def fake_tail_pad(**kwargs):  # type: ignore[no-untyped-def]
        tail_calls.append(kwargs)
        kwargs["output_path"].write_bytes(b"padded")
        return 11

    monkeypatch.setattr("render.__main__.pad_video_by_tail", fake_tail_pad)
    assert run_render(args) == 0
    assert tail_calls[0]["shortage_s"] == pytest.approx(0.35)
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert any("delayed audio duration 2.350s" in warning for warning in meta["warnings"])


def test_render_cli_falls_back_to_legacy_padding_when_tail_padding_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0},
    )

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.5
        if name == "video_only.mp4":
            return 2.0
        if name == "video_only_padded.mp4":
            return 2.5
        if name == "recap.mp4":
            return 2.5
        return 2.0

    legacy_calls = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    monkeypatch.setattr(
        "render.__main__.pad_video_by_tail", lambda **kwargs: (_ for _ in ()).throw(MediaError("tail failed"))
    )

    def fake_legacy_pad(video_path, output_path, duration_s):  # type: ignore[no-untyped-def]
        legacy_calls.append((video_path, output_path, duration_s))
        output_path.write_bytes(b"padded")

    monkeypatch.setattr("render.__main__.pad_video_to_duration", fake_legacy_pad)
    monkeypatch.setattr(
        "render.__main__.mux_voiceover",
        lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"),
    )
    assert run_render(args) == 0
    assert legacy_calls
    assert legacy_calls[0][2] == pytest.approx(2.5)
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert any("fell back to full re-encode padding" in warning for warning in meta["warnings"])


def test_render_cli_falls_back_when_tail_output_is_still_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0},
    )

    def fake_probe_duration(path):  # type: ignore[no-untyped-def]
        name = Path(path).name
        if name == "voiceover.mp3":
            return 2.5
        if name == "video_only.mp4":
            return 2.0
        if name == "video_only_padded.mp4":
            return 2.1 if not legacy_calls else 2.5
        if name == "recap.mp4":
            return 2.5
        return 2.0

    legacy_calls = []
    monkeypatch.setattr("render.__main__.probe_duration", fake_probe_duration)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    monkeypatch.setattr(
        "render.__main__.pad_video_by_tail", lambda **kwargs: kwargs["output_path"].write_bytes(b"short") or 15
    )

    def fake_legacy_pad(video_path, output_path, duration_s):  # type: ignore[no-untyped-def]
        legacy_calls.append(duration_s)
        output_path.write_bytes(b"padded")

    monkeypatch.setattr("render.__main__.pad_video_to_duration", fake_legacy_pad)
    monkeypatch.setattr(
        "render.__main__.mux_voiceover",
        lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"),
    )

    assert run_render(args) == 0
    assert legacy_calls == [pytest.approx(2.5)]


def test_render_cli_duration_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0},
    )
    monkeypatch.setattr("render.__main__.probe_duration", lambda path: 2.5 if Path(path).name == "recap.mp4" else 2.0)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    monkeypatch.setattr("render.__main__.cut_temp_clip", lambda **kwargs: kwargs["output_path"].write_bytes(b"temp"))
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    monkeypatch.setattr(
        "render.__main__.mux_voiceover",
        lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"),
    )
    assert run_render(args) == 0
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert meta["duration_match"] is False
    assert meta["warnings"]


def test_render_cli_source_map_uses_matching_episode_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = make_args(tmp_path)
    source_one = tmp_path / "Grand_Blue.S03E01.mp4"
    source_two = tmp_path / "Grand_Blue.S03E02.mp4"
    source_one.write_bytes(b"one")
    source_two.write_bytes(b"two")
    edl = [
        {
            "tl_start": 0,
            "tl_end": 1,
            "src": "s03e01/Grand_Blue.S03E01.mp4",
            "src_in": 0,
            "src_out": 1,
            "beat_id": 0,
            "shot_index": 0,
            "reused": False,
            "speed": 1.0,
        },
        {
            "tl_start": 1,
            "tl_end": 2,
            "src": "s03e02/Grand_Blue.S03E02.mp4",
            "src_in": 2,
            "src_out": 3,
            "beat_id": 1,
            "shot_index": 1,
            "reused": False,
            "speed": 1.0,
        },
    ]
    args.edl.write_text(json.dumps(edl), encoding="utf-8")
    args.film = None
    args.source_map = tmp_path / "edl.source_map.json"
    args.concurrency = 1
    args.source_map.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    "s03e01/Grand_Blue.S03E01.mp4": str(source_one),
                    "s03e02/Grand_Blue.S03E02.mp4": str(source_two),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("render.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "render.__main__.probe_video_stream",
        lambda path: {"width": 1920, "height": 1080, "codec": "h264", "fps": 30.0, "duration": 10.0},
    )
    monkeypatch.setattr("render.__main__.probe_duration", lambda path: 2.0)
    monkeypatch.setattr("render.__main__.has_audio_stream", lambda path: True)
    film_paths = []

    def fake_cut(**kwargs):  # type: ignore[no-untyped-def]
        film_paths.append(kwargs["film_path"])
        kwargs["output_path"].write_bytes(b"temp")

    monkeypatch.setattr("render.__main__.cut_temp_clip", fake_cut)
    monkeypatch.setattr(
        "render.__main__.concat_video", lambda temp_paths, output_path, work_dir: output_path.write_bytes(b"video")
    )
    monkeypatch.setattr(
        "render.__main__.mux_voiceover",
        lambda video_path, voiceover_path, output_path, audio_delay_s=0.0: output_path.write_bytes(b"recap"),
    )

    assert run_render(args) == 0
    assert film_paths == [source_one.resolve(), source_two.resolve()]
    meta = json.loads((tmp_path / "render.meta.json").read_text(encoding="utf-8"))
    assert meta["source_count"] == 2
    assert meta["source_names"] == ["s03e01/Grand_Blue.S03E01.mp4", "s03e02/Grand_Blue.S03E02.mp4"]
