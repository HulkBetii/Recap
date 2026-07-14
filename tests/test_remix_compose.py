from __future__ import annotations

from pathlib import Path
from argparse import Namespace

import pytest

from common.integrity import file_hash, media_identity_hash
from common.schema import AudioAssets, RemixEdl
from reaction_remix.compose.__main__ import run_compose
from reaction_remix.compose.composer import compose_remix
from tests.reaction_factories import HASH_A, NOW, make_blocks, make_commentary_audio, make_plan, make_source


@pytest.mark.parametrize("protected_kind", ["reaction", "mixed", "unknown"])
def test_compose_preserves_protected_audio_video_and_uses_tts_only_without_bed(
    tmp_path: Path,
    protected_kind: str,
) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")

    blocks = make_blocks()
    protected = blocks.blocks[0].model_copy(update={"kind": protected_kind})
    blocks = blocks.model_copy(update={"blocks": [protected, *blocks.blocks[1:]]})

    edl, repair = compose_remix(
        film_path=film,
        source=make_source(),
        blocks=blocks,
        plan=make_plan(),
        commentary_audio=make_commentary_audio(tts),
        commentary_audio_base=tmp_path,
        plan_hash="d" * 64,
        commentary_audio_hash="e" * 64,
    )

    assert repair is None
    assert edl.total_duration_s == 85.0
    protected_placement = edl.placements[0]
    assert protected_placement.kind == protected_kind
    assert protected_placement.audio.mode == "source"
    assert protected_placement.audio.source_src == protected_placement.video.src
    assert protected_placement.audio.source_in == protected_placement.video.src_in
    assert protected_placement.audio.source_out == protected_placement.video.src_out
    assert protected_placement.audio.source_gain_db == 0.0
    assert protected_placement.video.speed == 1.0
    assert protected_placement.video.filters == []
    assert protected_placement.audio.filters == []
    assert edl.placements[1].audio.mode == "tts"
    assert edl.placements[1].video.filters == []


def test_compose_uses_only_non_leaking_no_vocals_asset(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")
    bed = tmp_path / "no_vocals.wav"
    bed.write_bytes(b"bed")
    assets = AudioAssets.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": HASH_A,
            "items": [
                {
                    "asset_id": "stem-no-vocals",
                    "kind": "no_vocals",
                    "path": bed.as_posix(),
                    "content_hash": file_hash(bed),
                    "source_hash": HASH_A,
                    "duration_s": 100.0,
                    "sample_rate": 44100,
                    "channels": 2,
                    "src_tc_start": 0.0,
                    "src_tc_end": 100.0,
                    "leakage_detected": False,
                    "warnings": [],
                }
            ],
            "created_at": NOW,
            "warnings": [],
        }
    )

    edl, repair = compose_remix(
        film_path=film,
        source=make_source(),
        blocks=make_blocks(),
        plan=make_plan(),
        commentary_audio=make_commentary_audio(tts),
        commentary_audio_base=tmp_path,
        plan_hash="d" * 64,
        commentary_audio_hash="e" * 64,
        audio_assets=assets,
        audio_assets_base=tmp_path,
    )

    assert repair is None
    commentary = edl.placements[1]
    assert commentary.audio.mode == "tts_bed"
    assert commentary.audio.bed_audio_path == bed.resolve().as_posix()
    assert "bed_fade_180ms" in commentary.audio.filters
    assert "boundary_fade_50ms" in commentary.audio.filters

    repaired_edl, repaired = compose_remix(
        film_path=film,
        source=make_source(),
        blocks=make_blocks(),
        plan=make_plan(),
        commentary_audio=make_commentary_audio(tts),
        commentary_audio_base=tmp_path,
        plan_hash="d" * 64,
        commentary_audio_hash="e" * 64,
        audio_assets=assets,
        audio_assets_base=tmp_path,
        force_tts_slots={"slot-0001"},
    )
    assert repaired is None
    assert repaired_edl.placements[1].audio.mode == "tts"


def test_compose_returns_repair_when_visual_capacity_is_too_short(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")

    blocks = make_blocks()
    eligible_broll = blocks.blocks[0].model_copy(
        update={
            "block_id": "block-0003",
            "kind": "broll",
            "turn_ids": [],
            "language_codes": [],
            "speaker_ids": [],
            "eligible_commentary_visual": True,
        }
    )
    blocks = blocks.model_copy(update={"blocks": [*blocks.blocks, eligible_broll]})

    _edl, repair = compose_remix(
        film_path=film,
        source=make_source(),
        blocks=blocks,
        plan=make_plan(),
        commentary_audio=make_commentary_audio(tts, duration_s=25.0),
        commentary_audio_base=tmp_path,
        plan_hash="d" * 64,
        commentary_audio_hash="e" * 64,
    )

    assert repair is not None
    assert repair.items[0].kind == "tts_fit"
    assert repair.items[0].affected_ids == ["slot-0001"]


def test_compose_never_reuses_a_plan_selected_source_block_as_commentary_visual(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")
    blocks = make_blocks()
    selected_broll = blocks.blocks[0].model_copy(
        update={
            "block_id": "block-0003",
            "kind": "broll",
            "turn_ids": [],
            "language_codes": [],
            "speaker_ids": [],
            "eligible_commentary_visual": True,
        }
    )
    blocks = blocks.model_copy(update={"blocks": [*blocks.blocks, selected_broll]})
    plan = make_plan()
    plan = plan.model_copy(
        update={
            "items": [
                *plan.items,
                plan.items[0].model_copy(
                    update={
                        "item_id": "item-0002",
                        "order": 2,
                        "role": "body",
                        "block_id": "block-0003",
                    }
                ),
            ]
        }
    )

    _edl, repair = compose_remix(
        film_path=film,
        source=make_source(),
        blocks=blocks,
        plan=plan,
        commentary_audio=make_commentary_audio(tts, duration_s=25.0),
        commentary_audio_base=tmp_path,
        plan_hash="d" * 64,
        commentary_audio_hash="e" * 64,
    )

    assert repair is not None
    assert repair.items[0].affected_ids == ["slot-0001"]


def test_compose_cli_records_exact_input_file_hashes(tmp_path: Path) -> None:
    film = tmp_path / "source.mp4"
    film.write_bytes(b"film")
    source_hash = media_identity_hash(film)
    source_path = tmp_path / "reaction_source.json"
    source_path.write_text(make_source(input_path=film.as_posix(), input_hash=source_hash).model_dump_json(indent=2), encoding="utf-8")
    blocks_path = tmp_path / "reaction_blocks.json"
    blocks_path.write_text(make_blocks(source_hash=source_hash).model_dump_json(indent=2), encoding="utf-8")
    plan_path = tmp_path / "remix_plan.json"
    plan_path.write_text(
        make_plan(source_hash=source_hash, blocks_hash=file_hash(blocks_path) or "").model_dump_json(indent=2),
        encoding="utf-8",
    )
    tts = tmp_path / "tts.mp3"
    tts.write_bytes(b"tts")
    audio_path = tmp_path / "commentary_audio.json"
    audio_path.write_text(make_commentary_audio(tts, source_hash=source_hash).model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "remix_edl.json"

    assert run_compose(
        Namespace(
            film=film,
            source=source_path,
            blocks=blocks_path,
            plan=plan_path,
            commentary_audio=audio_path,
            audio_assets=None,
            output=output,
            repair_request=None,
            repair_overrides=None,
            work_dir=tmp_path / "work",
            tts_gain_db=1.0,
            bed_gain_db=-14.0,
            boundary_fade_ms=50,
            bed_fade_ms=180,
            force=False,
        )
    ) == 0
    edl = RemixEdl.model_validate_json(output.read_text(encoding="utf-8"))
    assert edl.plan_hash == file_hash(plan_path)
    assert edl.commentary_audio_hash == file_hash(audio_path)
