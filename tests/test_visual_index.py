from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from common.schema import Shot, ShotVisualIndexFile, validate_shot_visual_index, write_json
from visual_index.__main__ import build_visual_index

class FakeImageEncoder:
    device = "cpu"

    def encode_images(self, image_paths: list[Path], *, batch_size: int) -> list[list[float]]:
        return [[1.0, 0.0] for _path in image_paths]

def test_build_visual_index_writes_sidecar_embeddings(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    shots_path = tmp_path / "shots.json"
    shots = [
        Shot(src="film.mp4", index=0, tc_start=0, tc_end=2, duration=2, thumb="shots/0.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=True),
    ]
    write_json(shots_path, shots)

    def fake_extract_frame(_film: Path, _tc: float, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"jpg")

    monkeypatch.setattr("visual_index.__main__.extract_frame", fake_extract_frame)
    args = argparse.Namespace(
        film=film,
        shots=shots_path,
        output=tmp_path / "shot_visual_index.json",
        asset_dir=tmp_path / "visual_index",
        embedding_mode="siglip2",
        embedding_model="mock",
        device="cpu",
        batch_size=2,
        keyframes_per_shot=2,
        work_dir=tmp_path / "work",
        force=False,
    )

    index = build_visual_index(args, encoder=FakeImageEncoder())
    write_json(args.output, index)

    parsed = ShotVisualIndexFile.model_validate_json(args.output.read_text(encoding="utf-8"))
    validate_shot_visual_index(parsed, shots)
    assert parsed.meta.n_shots == 1
    assert len(parsed.shots[0].keyframes) == 2
    vector_path = tmp_path / parsed.shots[0].shot_embedding_ref
    assert vector_path.is_file()
    assert np.load(vector_path).shape == (2,)
