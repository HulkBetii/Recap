from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pytest

from common.schema import Shot, ShotVisualIndexFile, validate_shot_visual_index, write_json
from visual_index.__main__ import build_visual_index

class FakeImageEncoder:
    device = "cpu"
    logit_scale = 10.0
    logit_bias = -5.0

    def encode_images(self, image_paths: list[Path], *, batch_size: int) -> list[list[float]]:
        return [[1.0, 0.0] for _path in image_paths]


class VectorImageEncoder(FakeImageEncoder):
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def encode_images(self, image_paths: list[Path], *, batch_size: int) -> list[list[float]]:
        return [self.vector for _path in image_paths]

def test_build_visual_index_writes_sidecar_embeddings(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    shots_path = tmp_path / "shots.json"
    shots = [
        Shot(src="film.mp4", index=0, tc_start=0, tc_end=2, duration=2, thumb="shots/0.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=True),
    ]
    write_json(shots_path, shots)

    def fake_extract_keyframes(_film: Path, requests, *, mode: str) -> None:  # type: ignore[no-untyped-def]
        assert mode == "per-frame"
        for request in requests:
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b"jpg")

    monkeypatch.setattr("visual_index.__main__.extract_keyframes", fake_extract_keyframes)
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
        frame_sampling="per-frame",
        work_dir=tmp_path / "work",
        force=False,
    )

    index = build_visual_index(args, encoder=FakeImageEncoder())
    write_json(args.output, index)

    parsed = ShotVisualIndexFile.model_validate_json(args.output.read_text(encoding="utf-8"))
    validate_shot_visual_index(parsed, shots)
    assert parsed.meta.n_shots == 1
    assert parsed.meta.version == "1.1"
    assert parsed.meta.logit_scale == 10.0
    assert len(parsed.shots[0].keyframes) == 2
    vector_path = tmp_path / parsed.shots[0].shot_embedding_ref
    assert vector_path.is_file()
    assert np.load(vector_path).shape == (2,)


def test_missing_keyframe_embedding_rebuilds_pooled_vector(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    shots_path = tmp_path / "shots.json"
    shots = [Shot(src="film.mp4", index=0, tc_start=0, tc_end=2, duration=2, thumb="shots/0.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=True)]
    write_json(shots_path, shots)

    def fake_extract_keyframes(_film: Path, requests, *, mode: str) -> None:  # type: ignore[no-untyped-def]
        for request in requests:
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b"jpg")

    monkeypatch.setattr("visual_index.__main__.extract_keyframes", fake_extract_keyframes)
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
        frame_sampling="per-frame",
        work_dir=tmp_path / "work",
        force=False,
    )
    first = build_visual_index(args, encoder=VectorImageEncoder([1.0, 0.0]))
    write_json(args.output, first)
    missing = tmp_path / first.shots[0].keyframes[1].embedding_ref
    missing.unlink()

    rebuilt = build_visual_index(args, encoder=VectorImageEncoder([0.0, 1.0]), cache_compatible=True)
    pooled = np.load(tmp_path / rebuilt.shots[0].shot_embedding_ref).astype("float32")
    assert pooled[0] == pytest.approx(pooled[1], abs=1e-3)


def test_checksum_mismatch_reencodes_keyframe_and_rebuilds_pool(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    film = tmp_path / "film.mp4"
    film.write_bytes(b"film")
    shots_path = tmp_path / "shots.json"
    shots = [Shot(src="film.mp4", index=0, tc_start=0, tc_end=2, duration=2, thumb="shots/0.jpg", motion_score=0.5, face_count=0, face_area=0, brightness=0.5, is_usable=True)]
    write_json(shots_path, shots)

    def fake_extract_keyframes(_film: Path, requests, *, mode: str) -> None:  # type: ignore[no-untyped-def]
        for request in requests:
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(b"jpg")

    monkeypatch.setattr("visual_index.__main__.extract_keyframes", fake_extract_keyframes)
    args = argparse.Namespace(
        film=film,
        shots=shots_path,
        output=tmp_path / "shot_visual_index.json",
        asset_dir=tmp_path / "visual_index",
        embedding_mode="siglip2",
        embedding_model="mock",
        device="cpu",
        batch_size=2,
        keyframes_per_shot=1,
        frame_sampling="per-frame",
        work_dir=tmp_path / "work",
        force=False,
    )
    first = build_visual_index(args, encoder=VectorImageEncoder([1.0, 0.0]))
    write_json(args.output, first)
    keyframe_path = tmp_path / first.shots[0].keyframes[0].embedding_ref
    np.save(keyframe_path, np.asarray([0.0, 1.0], dtype=np.float16))

    rebuilt = build_visual_index(args, encoder=VectorImageEncoder([0.6, 0.8]), cache_compatible=True)

    assert np.load(keyframe_path).astype("float32").tolist() == pytest.approx([0.6, 0.8], abs=1e-3)
    assert np.load(tmp_path / rebuilt.shots[0].shot_embedding_ref).astype("float32").tolist() == pytest.approx([0.6, 0.8], abs=1e-3)
