from __future__ import annotations

from argparse import Namespace
from fractions import Fraction
from pathlib import Path

import pytest

from common.integrity import media_identity_hash
from common.schema import RemixCommandManifest, RemixEdl, RemixRenderMeta, RemixRenderTimeline
from reaction_remix.compose.composer import compose_remix
from reaction_remix.render.engine import render_remix
from reaction_remix.render.__main__ import run_render
from reaction_remix.render.commands import RemixRenderError
from reaction_remix.render.quantize import quantize_remix_placements
from tests.reaction_factories import make_blocks, make_commentary_audio, make_plan, make_source


def make_edl(tmp_path: Path):  # type: ignore[no-untyped-def]
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")
    edl, repair = compose_remix(
        film_path=film,
        source=make_source(input_path=film.as_posix()),
        blocks=make_blocks(),
        plan=make_plan(),
        commentary_audio=make_commentary_audio(tts),
        commentary_audio_base=tmp_path,
        plan_hash="d" * 64,
        commentary_audio_hash="e" * 64,
    )
    assert repair is None
    return film, edl


def test_quantize_remix_timeline_is_frame_and_sample_locked(tmp_path: Path) -> None:
    _film, edl = make_edl(tmp_path)

    placements = quantize_remix_placements(
        edl.placements,
        fps_num=edl.output.fps_num,
        fps_den=edl.output.fps_den,
        sample_rate=edl.output.audio_sample_rate,
    )

    assert placements[0].frame_start == 0
    assert placements[1].frame_start == placements[0].frame_end
    assert placements[1].sample_start == placements[0].sample_end
    assert placements[-1].frame_end == round(edl.total_duration_s * 30000 / 1001)
    assert placements[-1].sample_end == round(edl.total_duration_s * 44100)


def test_render_builds_no_visual_edit_manifest_and_unfiltered_reaction_audio(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film, edl = make_edl(tmp_path)
    output = tmp_path / "out.mp4"

    def fake_run(args: list[str], commands: list[list[str]]) -> None:
        commands.append(list(args))
        target = Path(args[-1])
        if args[-1] != "-" and target.suffix in {".mp4", ".wav"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"media")

    monkeypatch.setattr("reaction_remix.render.engine.run_media_command", fake_run)
    monkeypatch.setattr("reaction_remix.render.engine.probe_duration", lambda _path: edl.total_duration_s)
    monkeypatch.setattr("reaction_remix.render.engine.probe_source_true_peak_dbfs", lambda _path: -0.5)

    timeline_payload, manifest_payload, meta_payload = render_remix(
        film_path=film,
        edl=edl,
        edl_hash="9" * 64,
        output_path=output,
        work_dir=tmp_path / "render-work",
    )

    timeline = RemixRenderTimeline.model_validate(timeline_payload)
    manifest = RemixCommandManifest.model_validate(manifest_payload)
    meta_payload["timeline_hash"] = "7" * 64
    meta_payload["command_manifest_hash"] = "8" * 64
    meta = RemixRenderMeta.model_validate(meta_payload)
    assert timeline.total_frames > 0
    assert meta.crf == 18
    command_text = " ".join(arg for command in manifest.commands for arg in command.args).lower()
    assert not any(term in command_text for term in manifest.denylist)
    source_audio_commands = [
        command.args
        for command in manifest.commands
        if command.purpose == "render_audio_clip" and "-ss" in command.args and "-vn" in command.args
    ]
    assert source_audio_commands
    assert "-af" not in source_audio_commands[0]
    assert "-filter_complex" not in source_audio_commands[0]
    commentary_audio_commands = [
        command.args
        for command in manifest.commands
        if command.purpose == "render_audio_clip" and "alimiter=" in " ".join(command.args)
    ]
    assert commentary_audio_commands
    commentary_filter = " ".join(commentary_audio_commands[0])
    assert "limit=0.841395142" in commentary_filter
    assert "level=false" in commentary_filter
    assert "latency=true" in commentary_filter
    video_commands = [command.args for command in manifest.commands if command.purpose == "render_video_clip"]
    assert video_commands
    assert all("-r" not in command for command in video_commands)
    assert manifest.warnings == [
        "commentary limiter ceiling -1.500 dBFS selected from source true peak "
        "-0.500 dBFS with 0.300 dB codec headroom"
    ]
    assert manifest.commands[-2].purpose == "mux_output"
    assert "-shortest" not in manifest.commands[-2].args
    assert manifest.commands[-1].args[-1] == "-"


def test_render_uses_quantized_source_indexes_for_decimal_spans(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film, base_edl = make_edl(tmp_path)
    payload = base_edl.model_dump(mode="json")
    payload["placements"][0]["video"].update({"src_in": 0.12345, "src_out": 80.12345})
    payload["placements"][0]["audio"].update({"source_in": 0.12345, "source_out": 80.12345})
    edl = RemixEdl.model_validate(payload)
    captured_video_identities = []
    captured_audio_identities = []

    def fake_run(args: list[str], commands: list[list[str]]) -> None:
        commands.append(list(args))
        target = Path(args[-1])
        if args[-1] != "-" and target.suffix in {".mp4", ".wav"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"media")

    from reaction_remix.render.cache import RemixRenderCache

    original_video_path = RemixRenderCache.video_path
    original_audio_path = RemixRenderCache.audio_path

    def capture_video_path(self, identity):  # type: ignore[no-untyped-def]
        captured_video_identities.append(identity)
        return original_video_path(self, identity)

    def capture_audio_path(self, identity):  # type: ignore[no-untyped-def]
        captured_audio_identities.append(identity)
        return original_audio_path(self, identity)

    monkeypatch.setattr("reaction_remix.render.engine.run_media_command", fake_run)
    monkeypatch.setattr("reaction_remix.render.engine.probe_duration", lambda _path: edl.total_duration_s)
    monkeypatch.setattr("reaction_remix.render.engine.probe_source_true_peak_dbfs", lambda _path: -0.5)
    monkeypatch.setattr(RemixRenderCache, "video_path", capture_video_path)
    monkeypatch.setattr(RemixRenderCache, "audio_path", capture_audio_path)

    timeline_payload, manifest_payload, _meta = render_remix(
        film_path=film,
        edl=edl,
        edl_hash="9" * 64,
        output_path=tmp_path / "out.mp4",
        work_dir=tmp_path / "render-work",
    )

    timeline = RemixRenderTimeline.model_validate(timeline_payload)
    manifest = RemixCommandManifest.model_validate(manifest_payload)
    first = timeline.placements[0]
    expected_source_frame = round(Fraction("0.12345") * Fraction(30000, 1001))
    expected_source_sample = round(Fraction("0.12345") * 44100)
    assert first.src_start_frame == expected_source_frame
    assert first.src_end_frame == expected_source_frame + first.tl_end_frame - first.tl_start_frame
    assert first.src_start_sample == expected_source_sample
    assert first.src_end_sample == expected_source_sample + first.tl_end_sample - first.tl_start_sample
    assert captured_video_identities[0]["source_frames"] == [first.src_start_frame, first.src_end_frame]
    assert "src_in" not in captured_video_identities[0]
    assert captured_audio_identities[0]["source_samples"] == [first.src_start_sample, first.src_end_sample]
    assert "source" not in captured_audio_identities[0]

    video_command = next(command.args for command in manifest.commands if command.purpose == "render_video_clip")
    audio_command = next(
        command.args
        for command in manifest.commands
        if command.purpose == "render_audio_clip" and "-vn" in command.args
    )
    expected_video_start_s = expected_source_frame * 1001 / 30000
    expected_audio_start_s = expected_source_sample / 44100
    assert video_command[video_command.index("-ss") + 1] == f"{expected_video_start_s:.12f}"
    assert "-r" not in video_command
    assert audio_command[audio_command.index("-ss") + 1] == f"{expected_audio_start_s:.12f}"
    assert audio_command[audio_command.index("-t") + 1] == f"{first.tl_end_sample / 44100:.12f}"
    assert "-af" not in audio_command
    assert "-filter_complex" not in audio_command


def test_render_limiter_tracks_quieter_source_peak_and_cache_identity(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film, edl = make_edl(tmp_path)
    probe_calls = 0
    captured_audio_identities = []

    def fake_run(args: list[str], commands: list[list[str]]) -> None:
        commands.append(list(args))
        target = Path(args[-1])
        if args[-1] != "-" and target.suffix in {".mp4", ".wav"}:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"media")

    def fake_probe(_path: Path) -> float:
        nonlocal probe_calls
        probe_calls += 1
        return -6.0

    from reaction_remix.render.cache import RemixRenderCache

    original_audio_path = RemixRenderCache.audio_path

    def capture_audio_path(self, identity):  # type: ignore[no-untyped-def]
        captured_audio_identities.append(identity)
        return original_audio_path(self, identity)

    monkeypatch.setattr("reaction_remix.render.engine.run_media_command", fake_run)
    monkeypatch.setattr("reaction_remix.render.engine.probe_duration", lambda _path: edl.total_duration_s)
    monkeypatch.setattr("reaction_remix.render.engine.probe_source_true_peak_dbfs", fake_probe)
    monkeypatch.setattr(RemixRenderCache, "audio_path", capture_audio_path)

    timeline_payload, manifest_payload, meta_payload = render_remix(
        film_path=film,
        edl=edl,
        edl_hash="9" * 64,
        output_path=tmp_path / "out.mp4",
        work_dir=tmp_path / "render-work",
    )

    timeline = RemixRenderTimeline.model_validate(timeline_payload)
    manifest = RemixCommandManifest.model_validate(manifest_payload)
    warning = (
        "commentary limiter ceiling -6.300 dBFS selected from source true peak "
        "-6.000 dBFS with 0.300 dB codec headroom"
    )
    commentary_command = next(
        command.args
        for command in manifest.commands
        if command.purpose == "render_audio_clip" and "alimiter=" in " ".join(command.args)
    )
    assert probe_calls == 1
    assert "limit=0.484172368" in " ".join(commentary_command)
    assert captured_audio_identities[-1]["commentary_limit_db"] == -6.3
    assert timeline.warnings == [warning]
    assert manifest.warnings == [warning]
    assert meta_payload["warnings"] == [warning]


def test_render_rejects_vfr_source_before_running_ffmpeg(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film, edl = make_edl(tmp_path)
    source_hash = media_identity_hash(film)
    source = make_source(input_path=film.as_posix(), input_hash=source_hash)
    source = source.model_copy(update={"video": source.video.model_copy(update={"frame_rate_mode": "vfr"})})
    source_path = tmp_path / "reaction_source.json"
    source_path.write_text(source.model_dump_json(indent=2), encoding="utf-8")
    edl_path = tmp_path / "remix_edl.json"
    edl_path.write_text(edl.model_copy(update={"source_hash": source_hash}).model_dump_json(indent=2), encoding="utf-8")
    monkeypatch.setattr("reaction_remix.render.__main__.require_ffmpeg", lambda: None)

    with pytest.raises(RemixRenderError, match="requires a CFR source"):
        run_render(
            Namespace(
                film=film,
                source=source_path,
                edl=edl_path,
                output=tmp_path / "out.mp4",
                work_dir=tmp_path / "render-work",
                timeline_output=None,
                command_manifest=None,
                meta_output=None,
                repair_request=None,
                crf=18,
                preset="medium",
                audio_bitrate="192k",
                force=False,
            )
        )
