from __future__ import annotations

from pathlib import Path
from argparse import Namespace
import json
import io
import subprocess

import numpy as np
import pytest

from common.integrity import file_hash, media_identity_hash
from common.schema import RemixCommandManifest, RemixEdl, RemixRenderTimeline
from reaction_remix.compose.composer import compose_remix
from reaction_remix.qa.__main__ import protected_narrator_overlap_block_ids, run_qa
from reaction_remix.qa.checks import (
    BoundaryFrameMeasurement,
    ReactionPlacementPreservation,
    ReactionPreservationMeasurement,
    best_correlation,
    boundary_audio_defects,
    decoded_media_counts,
    declared_reaction_mismatches,
    sample_reaction_preservation,
    visual_operation_counts,
    write_boundary_frames,
)
from tests.reaction_factories import (
    NOW,
    make_blocks,
    make_commentary_audio,
    make_commentary_script,
    make_plan,
    make_source,
    make_transcript,
)


def make_edl(tmp_path: Path):  # type: ignore[no-untyped-def]
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")
    edl, repair = compose_remix(
        film_path=film,
        source=make_source(),
        blocks=make_blocks(),
        plan=make_plan(),
        commentary_audio=make_commentary_audio(tts),
        commentary_audio_base=tmp_path,
        plan_hash="d" * 64,
        commentary_audio_hash="e" * 64,
    )
    assert repair is None
    return edl


def test_best_correlation_detects_one_frame_scale_lag() -> None:
    rng = np.random.default_rng(7)
    reference = rng.normal(size=4000).astype(np.float32)
    candidate = np.concatenate((np.zeros(12, dtype=np.float32), reference[:-12]))

    correlation, lag = best_correlation(reference, candidate, max_lag_samples=20)

    assert correlation > 0.999
    assert lag == 12


def test_best_correlation_treats_matching_silence_as_preserved() -> None:
    silence = np.zeros(4000, dtype=np.float32)

    correlation, lag = best_correlation(silence, silence.copy(), max_lag_samples=20)

    assert correlation == 1.0
    assert lag == 0


def test_best_correlation_rejects_one_sided_silence() -> None:
    silence = np.zeros(4000, dtype=np.float32)
    signal = np.ones(4000, dtype=np.float32) * 100.0

    correlation, lag = best_correlation(silence, signal, max_lag_samples=20)

    assert correlation == 0.0
    assert lag == 0


def test_declared_reaction_contract_is_unmodified(tmp_path: Path) -> None:
    edl = make_edl(tmp_path)

    checked, speed, gain, span = declared_reaction_mismatches(edl)

    assert (checked, speed, gain, span) == (1, 0, 0, 0)


def test_preservation_hard_gate_samples_every_reaction_mixed_and_unknown(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    base = make_edl(tmp_path)
    placements = []
    for index in range(10):
        payload = base.placements[0].model_dump(mode="json")
        payload.update(
            {
                "placement_id": f"placement-{index:04d}",
                "item_id": f"item-{index:04d}",
                "kind": ("reaction", "mixed", "unknown")[index % 3],
                "tl_start": float(index),
                "tl_end": float(index + 1),
            }
        )
        payload["video"].update({"src_in": float(index), "src_out": float(index + 1)})
        payload["audio"].update({"source_in": float(index), "source_out": float(index + 1)})
        placements.append(payload)
    edl_payload = base.model_dump(mode="json")
    edl_payload.update({"placements": placements, "total_duration_s": 10.0})
    edl = RemixEdl.model_validate(edl_payload)
    calls = 0
    frame_calls = 0
    samples = np.arange(32000, dtype=np.float32)

    def fake_decode(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return samples

    monkeypatch.setattr("reaction_remix.qa.checks.decode_audio", fake_decode)
    def fake_frame_similarity(*_args):  # type: ignore[no-untyped-def]
        nonlocal frame_calls
        frame_calls += 1
        return 0.999

    monkeypatch.setattr("reaction_remix.qa.checks.frame_similarity", fake_frame_similarity)

    result = sample_reaction_preservation(
        film_path=tmp_path / "source.mp4",
        output_path=tmp_path / "out.mp4",
        edl=edl,
        max_samples=2,
    )

    assert result[0] > 0.999
    assert calls == 20
    assert frame_calls == 30


@pytest.mark.parametrize("corrupt_edge", ["head", "tail"])
def test_preservation_catches_edge_corruption_when_middle_is_intact(
    tmp_path: Path,
    monkeypatch,
    corrupt_edge: str,
) -> None:  # type: ignore[no-untyped-def]
    edl = make_edl(tmp_path)
    film_path = tmp_path / "source.mp4"
    output_path = tmp_path / "out.mp4"
    samples = np.random.default_rng(19).normal(size=32000).astype(np.float32)

    def fake_decode(path: Path, *, start_s: float = 0.0, duration_s: float | None = None, **_kwargs):  # type: ignore[no-untyped-def]
        length = round((duration_s or 0.0) * 16000)
        decoded = samples[:length].copy()
        is_head = start_s < 0.1
        is_tail = start_s > 79.0
        if path == output_path and ((corrupt_edge == "head" and is_head) or (corrupt_edge == "tail" and is_tail)):
            edge_samples = min(1600, len(decoded))
            if corrupt_edge == "head":
                decoded[:edge_samples] = 0.0
            else:
                decoded[-edge_samples:] = 0.0
        return decoded

    monkeypatch.setattr("reaction_remix.qa.checks.decode_audio", fake_decode)
    monkeypatch.setattr("reaction_remix.qa.checks.frame_similarity", lambda *_args: 0.999)

    correlation, _lag_ms, _frame_similarity, _gain_delta = sample_reaction_preservation(
        film_path=film_path,
        output_path=output_path,
        edl=edl,
    )

    assert correlation < 0.98


def test_preservation_silence_policy_keeps_matching_silence_and_rejects_one_sided_signal(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    edl = make_edl(tmp_path)
    film_path = tmp_path / "source.mp4"
    output_path = tmp_path / "out.mp4"
    monkeypatch.setattr("reaction_remix.qa.checks.frame_similarity", lambda *_args: 1.0)
    monkeypatch.setattr(
        "reaction_remix.qa.checks.decode_audio",
        lambda *_args, **_kwargs: np.zeros(80 * 16000, dtype=np.float32),
    )

    correlation, _lag_ms, _similarity, gain_delta = sample_reaction_preservation(
        film_path=film_path,
        output_path=output_path,
        edl=edl,
    )

    assert correlation == 1.0
    assert gain_delta == 0.0

    def one_sided_decode(path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        if path == output_path:
            return np.ones(80 * 16000, dtype=np.float32) * 100.0
        return np.zeros(80 * 16000, dtype=np.float32)

    monkeypatch.setattr("reaction_remix.qa.checks.decode_audio", one_sided_decode)
    correlation, _lag_ms, _similarity, gain_delta = sample_reaction_preservation(
        film_path=film_path,
        output_path=output_path,
        edl=edl,
    )

    assert correlation == 0.0
    assert gain_delta == 120.0


def test_preservation_uses_quantized_timeline_indexes_instead_of_fractional_edl(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    base = make_edl(tmp_path)
    placement_payload = base.placements[0].model_dump(mode="json")
    placement_payload.update({"tl_start": 0.017, "tl_end": 4.017})
    placement_payload["video"].update({"src_in": 0.08, "src_out": 4.08})
    placement_payload["audio"].update({"source_in": 0.08, "source_out": 4.08})
    edl_payload = base.model_dump(mode="json")
    edl_payload.update({"placements": [placement_payload], "total_duration_s": 4.017})
    edl = RemixEdl.model_validate(edl_payload)
    timeline = RemixRenderTimeline.model_validate(
        {
            "source_hash": edl.source_hash,
            "edl_hash": "f" * 64,
            "fps_num": 30000,
            "fps_den": 1001,
            "audio_sample_rate": 44100,
            "placements": [
                {
                    "placement_id": edl.placements[0].placement_id,
                    "tl_start_frame": 0,
                    "tl_end_frame": 120,
                    "tl_start_sample": 0,
                    "tl_end_sample": 176400,
                    "src_start_frame": 3,
                    "src_end_frame": 123,
                    "src_start_sample": 2205,
                    "src_end_sample": 178605,
                }
            ],
            "total_frames": 120,
            "total_samples": 176400,
            "created_at": NOW,
        }
    )
    film_path = tmp_path / "source.mp4"
    output_path = tmp_path / "out.mp4"
    signal = np.random.default_rng(23).normal(size=100000).astype(np.float32)
    decode_calls: list[tuple[Path, float, float]] = []

    def fake_decode(path: Path, *, start_s: float, duration_s: float, **_kwargs):  # type: ignore[no-untyped-def]
        decode_calls.append((path, start_s, duration_s))
        canonical_start = start_s - (0.05 if path == film_path else 0.0)
        start = round(canonical_start * 16000)
        length = round(duration_s * 16000)
        return signal[start : start + length]

    frame_s = 1001 / 30000
    monkeypatch.setattr("reaction_remix.qa.checks.decode_audio", fake_decode)
    monkeypatch.setattr(
        "reaction_remix.qa.checks.frame_similarity",
        lambda _film, source_tc, _output, output_tc: (
            1.0 if abs((source_tc - 3 * frame_s) - output_tc) < 1e-9 else 0.0
        ),
    )

    correlation, _lag_ms, similarity, gain_delta = sample_reaction_preservation(
        film_path=film_path,
        output_path=output_path,
        edl=edl,
        timeline=timeline,
    )

    assert correlation > 0.999
    assert similarity == 1.0
    assert gain_delta < 1e-6
    assert len(decode_calls) == 2
    assert decode_calls[0][1] == pytest.approx(0.05)
    assert decode_calls[1][1] == pytest.approx(0.0)


def test_preservation_clamps_tail_probe_to_decoded_frame_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    edl = make_edl(tmp_path)
    protected = edl.placements[0]
    timeline = RemixRenderTimeline.model_validate(
        {
            "source_hash": edl.source_hash,
            "edl_hash": "f" * 64,
            "fps_num": 30000,
            "fps_den": 1001,
            "audio_sample_rate": 44100,
            "placements": [
                {
                    "placement_id": protected.placement_id,
                    "tl_start_frame": 20,
                    "tl_end_frame": 120,
                    "tl_start_sample": 0,
                    "tl_end_sample": 147147,
                    "src_start_frame": 10,
                    "src_end_frame": 110,
                    "src_start_sample": 0,
                    "src_end_sample": 147147,
                }
            ],
            "total_frames": 120,
            "total_samples": 147147,
            "created_at": NOW,
        }
    )
    frame_calls: list[tuple[float, float]] = []

    monkeypatch.setattr(
        "reaction_remix.qa.checks.decode_audio",
        lambda *_args, **_kwargs: np.ones(60000, dtype=np.float32),
    )

    def fake_frame_similarity(
        _film_path: Path,
        source_tc: float,
        _output_path: Path,
        output_tc: float,
    ) -> float:
        frame_calls.append((source_tc, output_tc))
        return 1.0

    monkeypatch.setattr("reaction_remix.qa.checks.frame_similarity", fake_frame_similarity)

    sample_reaction_preservation(
        film_path=tmp_path / "source.mp4",
        output_path=tmp_path / "out.mp4",
        edl=edl,
        timeline=timeline,
        source_frame_count=105,
        output_frame_count=110,
    )

    frame_s = 1001 / 30000
    assert frame_calls[-1] == pytest.approx(((10 + 89) * frame_s, (20 + 89) * frame_s))
    assert all(source_tc <= 104 * frame_s for source_tc, _ in frame_calls)
    assert all(output_tc <= 109 * frame_s for _, output_tc in frame_calls)


def test_boundary_frame_gate_uses_quantized_protected_sides_only(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    edl = make_edl(tmp_path)
    frames_80 = round(80 * 30000 / 1001)
    frames_85 = round(85 * 30000 / 1001)
    timeline = RemixRenderTimeline.model_validate(
        {
            "source_hash": edl.source_hash,
            "edl_hash": "f" * 64,
            "fps_num": 30000,
            "fps_den": 1001,
            "audio_sample_rate": 44100,
            "placements": [
                {
                    "placement_id": edl.placements[0].placement_id,
                    "tl_start_frame": 0,
                    "tl_end_frame": frames_80,
                    "tl_start_sample": 0,
                    "tl_end_sample": 80 * 44100,
                    "src_start_frame": 0,
                    "src_end_frame": frames_80,
                    "src_start_sample": 0,
                    "src_end_sample": 80 * 44100,
                },
                {
                    "placement_id": edl.placements[1].placement_id,
                    "tl_start_frame": frames_80,
                    "tl_end_frame": frames_85,
                    "tl_start_sample": 80 * 44100,
                    "tl_end_sample": 85 * 44100,
                    "src_start_frame": frames_80,
                    "src_end_frame": frames_85,
                    "src_start_sample": 80 * 44100,
                    "src_end_sample": 85 * 44100,
                },
            ],
            "total_frames": frames_85,
            "total_samples": 85 * 44100,
            "created_at": NOW,
        }
    )
    output_path = tmp_path / "out.mp4"
    calls: list[tuple[Path, float]] = []

    def fake_frame(path: Path, timestamp: float):  # type: ignore[no-untyped-def]
        calls.append((path, timestamp))
        if path == output_path and timestamp >= 80.0:
            return np.ones((4, 4, 3), dtype=np.uint8) * 255
        return np.zeros((4, 4, 3), dtype=np.uint8)

    monkeypatch.setattr("reaction_remix.qa.checks._frame_png", fake_frame)

    measurement = write_boundary_frames(
        film_path=tmp_path / "source.mp4",
        output_path=output_path,
        edl=edl,
        qa_dir=tmp_path / "qa",
        timeline=timeline,
    )

    expected_tail_tc = (frames_80 - 1) * 1001 / 30000
    assert measurement.min_frame_similarity == 1.0
    assert measurement.failed_placement_ids(min_frame_similarity=0.995) == []
    assert (tmp_path / "source.mp4", expected_tail_tc) in calls
    assert (output_path, expected_tail_tc) in calls
    assert any(path == output_path and timestamp >= 80.0 for path, timestamp in calls)


def test_boundary_frame_gate_clamps_reordered_source_tail_to_decoded_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    edl = make_edl(tmp_path)
    timeline = RemixRenderTimeline.model_validate(
        {
            "source_hash": edl.source_hash,
            "edl_hash": "f" * 64,
            "fps_num": 30,
            "fps_den": 1,
            "audio_sample_rate": 44100,
            "placements": [
                {
                    "placement_id": edl.placements[0].placement_id,
                    "tl_start_frame": 0,
                    "tl_end_frame": 10,
                    "tl_start_sample": 0,
                    "tl_end_sample": 441000,
                    "src_start_frame": 90,
                    "src_end_frame": 100,
                    "src_start_sample": 0,
                    "src_end_sample": 441000,
                },
                {
                    "placement_id": edl.placements[1].placement_id,
                    "tl_start_frame": 10,
                    "tl_end_frame": 15,
                    "tl_start_sample": 441000,
                    "tl_end_sample": 661500,
                    "src_start_frame": 10,
                    "src_end_frame": 15,
                    "src_start_sample": 441000,
                    "src_end_sample": 661500,
                },
            ],
            "total_frames": 15,
            "total_samples": 661500,
            "created_at": NOW,
        }
    )
    calls: list[tuple[Path, float]] = []

    def fake_frame(path: Path, timestamp: float):  # type: ignore[no-untyped-def]
        calls.append((path, timestamp))
        return np.zeros((4, 4, 3), dtype=np.uint8)

    monkeypatch.setattr("reaction_remix.qa.checks._frame_png", fake_frame)

    measurement = write_boundary_frames(
        film_path=tmp_path / "source.mp4",
        output_path=tmp_path / "out.mp4",
        edl=edl,
        qa_dir=tmp_path / "qa",
        timeline=timeline,
        source_frame_count=98,
        output_frame_count=15,
    )

    assert measurement.min_frame_similarity == 1.0
    assert (tmp_path / "source.mp4", 97 / 30) in calls
    assert (tmp_path / "out.mp4", 7 / 30) in calls
    assert all(timestamp <= 97 / 30 for path, timestamp in calls if path.name == "source.mp4")


def test_decoded_media_counts_streams_audio_without_buffering_whole_file(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "reaction_remix.qa.checks.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout="123\n", stderr=""),
    )

    class FakeProcess:
        stdout = io.BytesIO(b"\0" * (2 * 2 * 456))
        stderr = io.BytesIO(b"")

        @staticmethod
        def wait() -> int:
            return 0

    monkeypatch.setattr("reaction_remix.qa.checks.subprocess.Popen", lambda *args, **kwargs: FakeProcess())

    assert decoded_media_counts(tmp_path / "output.mp4", audio_channels=2) == (123, 456)


def test_visual_operation_counts_detects_forbidden_filter() -> None:
    manifest = RemixCommandManifest.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": "a" * 64,
            "edl_hash": "b" * 64,
            "denylist": ["subtitles=", "drawtext=", "overlay="],
            "commands": [
                {"command_id": "command-0000", "purpose": "test", "args": ["ffmpeg", "-vf", "drawtext=text=x", "out.mp4"]}
            ],
            "created_at": NOW,
            "warnings": [],
        }
    )

    counts = visual_operation_counts(manifest)

    assert counts["text_overlays"] == 1


def test_protected_narrator_overlap_audits_only_mixed_or_unknown_blocks() -> None:
    transcript = make_transcript()
    blocks = make_blocks()
    narrator_block = blocks.blocks[1].model_copy(
        update={
            "kind": "mixed",
            "preservation": blocks.blocks[1].preservation.model_copy(update={"audio": "source_mix"}),
        }
    )
    blocks = blocks.model_copy(update={"blocks": [blocks.blocks[0], narrator_block]})

    assert protected_narrator_overlap_block_ids(transcript, blocks) == ["block-0002"]

    commentary_block = narrator_block.model_copy(update={"kind": "commentary"})
    blocks = blocks.model_copy(update={"blocks": [blocks.blocks[0], commentary_block]})
    assert protected_narrator_overlap_block_ids(transcript, blocks) == []


def test_boundary_audio_defects_flags_silence_and_click(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    edl = make_edl(tmp_path)
    samples = np.ones(85 * 16000, dtype=np.float32) * 200
    boundary = 80 * 16000
    samples[boundary - 2000 : boundary + 2000] = 0
    samples[boundary] = 30000
    monkeypatch.setattr("reaction_remix.qa.checks.decode_audio", lambda *_args, **_kwargs: samples)

    silence_count, click_count = boundary_audio_defects(tmp_path / "out.mp4", edl)

    assert silence_count == 0  # The injected click prevents the entire boundary window from being silent.
    assert click_count == 1


def test_qa_cli_validates_full_provenance_and_writes_meta(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    source_hash = media_identity_hash(film)
    source = make_source(input_path=film.as_posix(), input_hash=source_hash)
    source_path = tmp_path / "reaction_source.json"
    source_path.write_text(source.model_dump_json(indent=2), encoding="utf-8")

    transcript = make_transcript(source_hash=source_hash)
    transcript_path = tmp_path / "reaction_transcript.json"
    transcript_path.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
    blocks = make_blocks(source_hash=source_hash, transcript_hash=file_hash(transcript_path) or "")
    blocks_path = tmp_path / "reaction_blocks.json"
    blocks_path.write_text(blocks.model_dump_json(indent=2), encoding="utf-8")
    plan = make_plan(source_hash=source_hash, blocks_hash=file_hash(blocks_path) or "")
    plan_path = tmp_path / "remix_plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    script = make_commentary_script(source_hash=source_hash, plan_hash=file_hash(plan_path) or "")
    script_path = tmp_path / "commentary_script.json"
    script_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")
    commentary_audio = make_commentary_audio(tts, source_hash=source_hash, script_hash=file_hash(script_path) or "")
    commentary_audio_path = tmp_path / "commentary_audio.json"
    commentary_audio_path.write_text(commentary_audio.model_dump_json(indent=2), encoding="utf-8")

    edl, repair = compose_remix(
        film_path=film,
        source=source,
        blocks=blocks,
        plan=plan,
        commentary_audio=commentary_audio,
        commentary_audio_base=tmp_path,
        plan_hash=file_hash(plan_path) or "",
        commentary_audio_hash=file_hash(commentary_audio_path) or "",
    )
    assert repair is None
    edl = edl.model_copy(update={"plan_hash": file_hash(plan_path), "commentary_audio_hash": file_hash(commentary_audio_path)})
    edl_path = tmp_path / "remix_edl.json"
    edl_path.write_text(edl.model_dump_json(indent=2), encoding="utf-8")
    edl_hash = file_hash(edl_path)
    frames_80 = round(80 * 30000 / 1001)
    frames_85 = round(85 * 30000 / 1001)
    timeline_path = tmp_path / "render.timeline.json"
    timeline_path.write_text(
        json.dumps(
            {
                "schema_version": "reaction-remix.v1",
                "source_hash": source_hash,
                "edl_hash": edl_hash,
                "fps_num": 30000,
                "fps_den": 1001,
                "audio_sample_rate": 44100,
                "placements": [
                    {"placement_id": "placement-0000", "tl_start_frame": 0, "tl_end_frame": frames_80, "tl_start_sample": 0, "tl_end_sample": 80 * 44100, "src_start_frame": 0, "src_end_frame": frames_80, "src_start_sample": 0, "src_end_sample": 80 * 44100},
                    {"placement_id": "placement-0001", "tl_start_frame": frames_80, "tl_end_frame": frames_85, "tl_start_sample": 80 * 44100, "tl_end_sample": 85 * 44100, "src_start_frame": frames_80, "src_end_frame": frames_85, "src_start_sample": 80 * 44100, "src_end_sample": 85 * 44100},
                ],
                "total_frames": frames_85,
                "total_samples": 85 * 44100,
                "created_at": NOW.isoformat(),
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "render.command-manifest.json"
    manifest_path.write_text(
        RemixCommandManifest.model_validate(
            {
                "schema_version": "reaction-remix.v1",
                "source_hash": source_hash,
                "edl_hash": edl_hash,
                "denylist": ["subtitles=", "drawtext=", "overlay="],
                "commands": [{"command_id": "command-0000", "purpose": "mux", "args": ["ffmpeg", "-i", "source.mp4", "out.mp4"]}],
                "created_at": NOW,
                "warnings": [],
            }
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    video = tmp_path / "reaction_remix.mp4"
    video.write_bytes(b"video")
    meta_path = tmp_path / "render.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "schema_version": "reaction-remix.v1",
                "source_hash": source_hash,
                "edl_hash": edl_hash,
                "output_path": video.as_posix(),
                "video_codec": "h264",
                "audio_codec": "aac",
                "crf": 18,
                "audio_bitrate": "192k",
                "width": 1920,
                "height": 1080,
                "fps_num": 30000,
                "fps_den": 1001,
                "audio_sample_rate": 44100,
                "audio_channels": 2,
                "duration_s": 85.0,
                "n_placements": 2,
                "decode_ok": True,
                "timeline_hash": file_hash(timeline_path),
                "command_manifest_hash": file_hash(manifest_path),
                "created_at": NOW.isoformat(),
                "cache_hits": [],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    qa_output = tmp_path / "remix_qa.json"
    review_html = tmp_path / "remix.review.html"
    stale_repairs = edl_path.with_name("remix_repair_requests.json")
    stale_repairs.write_text(
        json.dumps(
            {
                "schema_version": "reaction-remix.v1",
                "source_hash": source_hash,
                "items": [
                    {
                        "repair_id": "repair-0000",
                        "kind": "reaction_media_mismatch",
                        "affected_ids": ["placement-0000"],
                        "reason": "stale audit artifact",
                        "attempt": 1,
                        "requested_stage": "render",
                    }
                ],
                "created_at": NOW.isoformat(),
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("reaction_remix.qa.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("reaction_remix.qa.__main__.probe_output", lambda _path: {"duration_s": 85.0, "video_codec": "h264", "audio_codec": "aac", "width": 1920, "height": 1080, "fps": 30000 / 1001, "sample_rate": 44100, "channels": 2})
    monkeypatch.setattr("reaction_remix.qa.__main__.full_decode_ok", lambda _path: True)
    monkeypatch.setattr("reaction_remix.qa.__main__.decoded_media_counts", lambda *_args, **_kwargs: (frames_85, 85 * 44100))
    monkeypatch.setattr("reaction_remix.qa.__main__.decoded_video_frame_count", lambda _path: frames_85)
    monkeypatch.setattr(
        "reaction_remix.qa.__main__.measure_reaction_preservation",
        lambda **_kwargs: ReactionPreservationMeasurement(
            (
                ReactionPlacementPreservation(
                    placement_id="placement-0000",
                    min_audio_correlation=0.999,
                    max_av_drift_ms=10.0,
                    min_frame_similarity=0.999,
                    max_gain_delta_db=0.1,
                ),
            )
        ),
    )
    monkeypatch.setattr(
        "reaction_remix.qa.__main__.write_boundary_frames",
        lambda **_kwargs: BoundaryFrameMeasurement((("placement-0000", 0.999),)),
    )
    monkeypatch.setattr("reaction_remix.qa.__main__.commentary_leakage_placement_ids", lambda *_args: [])
    monkeypatch.setattr("reaction_remix.qa.__main__.narrator_phrase_leakage_placement_ids", lambda **_kwargs: [])
    monkeypatch.setattr("reaction_remix.qa.__main__.boundary_audio_defects", lambda *_args: (0, 0))
    monkeypatch.setattr("reaction_remix.qa.__main__.commentary_peak_dbfs", lambda *_args: -1.5)
    monkeypatch.setattr("reaction_remix.qa.__main__.program_peak_dbfs", lambda path: -1.5 if path == video else -1.6)
    monkeypatch.setattr(
        "reaction_remix.qa.__main__.protected_narrator_overlap_block_ids",
        lambda *_args: ["block-0002"],
    )

    result = run_qa(
        Namespace(
            film=film,
            source=source_path,
            transcript=transcript_path,
            blocks=blocks_path,
            plan=plan_path,
            commentary_script=script_path,
            commentary_audio=commentary_audio_path,
            edl=edl_path,
            video=video,
            render_meta=meta_path,
            render_timeline=timeline_path,
            command_manifest=manifest_path,
            output=qa_output,
            review_html=review_html,
            qa_dir=tmp_path / "qa",
            repair_requests=None,
            min_output_ratio=0.8,
            preferred_min_ratio=0.85,
            preferred_max_ratio=0.9,
            min_correlation=0.98,
            min_frame_similarity=0.995,
            min_tts_asr_match=0.9,
            max_samples=8,
            leakage_asr_model="mock",
            leakage_asr_device="cpu",
        )
    )

    assert result == 0
    assert qa_output.is_file()
    assert qa_output.with_name("remix_qa.meta.json").is_file()
    qa_payload = json.loads(qa_output.read_text(encoding="utf-8"))
    assert qa_payload["timeline"]["actual_frame_count"] == frames_85
    assert qa_payload["timeline"]["actual_sample_count"] == 85 * 44100
    assert qa_payload["commentary"]["protected_narrator_overlap_block_ids"] == ["block-0002"]
    assert qa_payload["reaction_preservation"]["failed_placement_ids"] == []
    assert qa_payload["repairs"] == []
    assert qa_payload["status"] == "pass"
    review_text = review_html.read_text(encoding="utf-8")
    assert "Protected narrator overlap" in review_text
    assert "Count: 1" in review_text
    assert "block-0002" in review_text
    qa_meta = json.loads(qa_output.with_name("remix_qa.meta.json").read_text(encoding="utf-8"))
    assert qa_meta["algorithm_version"] == "reaction-qa-v7"

    monkeypatch.setattr(
        "reaction_remix.qa.__main__.write_boundary_frames",
        lambda **_kwargs: BoundaryFrameMeasurement((("placement-0000", 0.90),)),
    )
    assert run_qa(
        Namespace(
            film=film,
            source=source_path,
            transcript=transcript_path,
            blocks=blocks_path,
            plan=plan_path,
            commentary_script=script_path,
            commentary_audio=commentary_audio_path,
            edl=edl_path,
            video=video,
            render_meta=meta_path,
            render_timeline=timeline_path,
            command_manifest=manifest_path,
            output=qa_output,
            review_html=None,
            qa_dir=tmp_path / "qa",
            repair_requests=None,
            min_output_ratio=0.8,
            preferred_min_ratio=0.85,
            preferred_max_ratio=0.9,
            min_correlation=0.98,
            min_frame_similarity=0.995,
            min_tts_asr_match=0.9,
            max_samples=8,
            leakage_asr_model="mock",
            leakage_asr_device="cpu",
        )
    ) == 1
    boundary_failed_payload = json.loads(qa_output.read_text(encoding="utf-8"))
    assert boundary_failed_payload["reaction_preservation"]["failed_placement_ids"] == [
        "placement-0000"
    ]

    monkeypatch.setattr(
        "reaction_remix.qa.__main__.write_boundary_frames",
        lambda **_kwargs: BoundaryFrameMeasurement((("placement-0000", 0.999),)),
    )

    monkeypatch.setattr(
        "reaction_remix.qa.__main__.decoded_media_counts",
        lambda *_args, **_kwargs: (frames_85 + 2, 85 * 44100),
    )
    assert run_qa(
        Namespace(
            film=film,
            source=source_path,
            transcript=transcript_path,
            blocks=blocks_path,
            plan=plan_path,
            commentary_script=script_path,
            commentary_audio=commentary_audio_path,
            edl=edl_path,
            video=video,
            render_meta=meta_path,
            render_timeline=timeline_path,
            command_manifest=manifest_path,
            output=qa_output,
            review_html=None,
            qa_dir=tmp_path / "qa",
            repair_requests=None,
            min_output_ratio=0.8,
            preferred_min_ratio=0.85,
            preferred_max_ratio=0.9,
            min_correlation=0.98,
            min_frame_similarity=0.995,
            min_tts_asr_match=0.9,
            max_samples=8,
            leakage_asr_model="mock",
            leakage_asr_device="cpu",
        )
    ) == 1
    failed_payload = json.loads(qa_output.read_text(encoding="utf-8"))
    assert failed_payload["timeline"]["frame_count_delta"] == 2
    assert failed_payload["timeline"]["status"] == "fail"
