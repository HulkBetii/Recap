from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from common.media import MediaError, require_ffmpeg
from common.schema import (
    Shot,
    ShotKeyframe,
    ShotVisualIndex,
    ShotVisualIndexFile,
    VisualIndexMeta,
    validate_shot_visual_index,
    validate_shots,
    write_json,
)
from match.cache import file_hash
from match.inputs import load_shots
from visual_index.encoder import DEFAULT_VISUAL_MODEL, TransformerVisualEncoder, VisualEncoder, VisualEncoderError, normalize_vector
from visual_index.frames import FrameRequest, extract_keyframes
from visual_index.integrity import (
    PREPROCESSING_VERSION,
    media_identity_hash,
    metadata_is_current,
    sha256_file,
    validate_visual_index_artifacts,
    vector_is_valid,
    visual_index_config_hash,
)


class VisualIndexError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 4.5 visual index: film + shots -> shot_visual_index.json")
    parser.add_argument("--film", required=True, type=Path)
    parser.add_argument("--shots", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--asset-dir", required=True, type=Path)
    parser.add_argument("--embedding-mode", default="siglip2", choices=["siglip2", "jina-clip-v2"])
    parser.add_argument("--embedding-model", default=DEFAULT_VISUAL_MODEL)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--batch-size", default=16, type=int)
    parser.add_argument("--keyframes-per-shot", default=2, type=int)
    parser.add_argument("--frame-sampling", default="per-frame", choices=["per-frame", "batch"])
    parser.add_argument("--work-dir", default=Path("work/visual_index"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not args.film.expanduser().resolve().is_file():
        raise VisualIndexError(f"film does not exist: {args.film}")
    if not args.shots.expanduser().resolve().is_file():
        raise VisualIndexError(f"shots file does not exist: {args.shots}")
    if args.keyframes_per_shot <= 0:
        raise VisualIndexError("--keyframes-per-shot must be > 0")
    if args.batch_size <= 0:
        raise VisualIndexError("--batch-size must be > 0")


def output_is_valid(
    output_path: Path,
    shots: list[Shot],
    *,
    film_path: Path,
    shots_path: Path,
    config_hash: str,
) -> bool:
    if not output_path.is_file():
        return False
    try:
        index = ShotVisualIndexFile.model_validate_json(output_path.read_text(encoding="utf-8"))
        if not metadata_is_current(index, film_path=film_path, shots_path=shots_path, config_hash=config_hash):
            return False
        validate_visual_index_artifacts(output_path, index, shots, require_frames=True, require_calibration=True)
        return True
    except Exception:
        return False


def keyframe_times(shot: Shot, count: int) -> list[tuple[float, str]]:
    if count == 1:
        return [(shot.tc_start + shot.duration / 2, "mid")]
    output: list[tuple[float, str]] = []
    roles = ["early", "mid", "late"]
    for index in range(count):
        fraction = (index + 1) / (count + 1)
        tc = shot.tc_start + shot.duration * fraction
        role = roles[index] if index < len(roles) else f"k{index}"
        output.append((tc, role))
    return output


def relative_ref(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def save_vector(path: Path, vector: list[float]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.asarray(normalize_vector(vector), dtype=np.float16))
    return sha256_file(path)


def load_vector(path: Path) -> list[float]:
    return normalize_vector([float(value) for value in np.load(path, allow_pickle=False).astype("float32").tolist()])


def pool_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    return normalize_vector(matrix.mean(axis=0).tolist())


def build_encoder(args: argparse.Namespace) -> VisualEncoder:
    trust_remote_code = args.embedding_mode == "jina-clip-v2"
    return TransformerVisualEncoder(args.embedding_model, device=args.device, trust_remote_code=trust_remote_code)


def build_visual_index(
    args: argparse.Namespace,
    encoder: VisualEncoder | None = None,
    *,
    config_hash: str | None = None,
    cache_compatible: bool | None = None,
) -> ShotVisualIndexFile:
    film = args.film.expanduser().resolve()
    shots_path = args.shots.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    asset_dir = args.asset_dir.expanduser().resolve()
    frame_dir = asset_dir / "frames"
    emb_dir = asset_dir / "emb"
    frame_sampling = getattr(args, "frame_sampling", "per-frame")
    shots = validate_shots(load_shots(shots_path))
    config_hash = config_hash or visual_index_config_hash(
        film_path=film,
        shots_path=shots_path,
        embedding_mode=args.embedding_mode,
        embedding_model=args.embedding_model,
        keyframes_per_shot=args.keyframes_per_shot,
        frame_sampling=frame_sampling,
    )
    if not args.force and output_is_valid(
        output_path,
        shots,
        film_path=film,
        shots_path=shots_path,
        config_hash=config_hash,
    ):
        return ShotVisualIndexFile.model_validate_json(output_path.read_text(encoding="utf-8"))

    previous_index: ShotVisualIndexFile | None = None
    if output_path.is_file():
        try:
            previous_index = ShotVisualIndexFile.model_validate_json(output_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            previous_index = None
    if cache_compatible is None:
        cache_compatible = bool(
            previous_index
            and metadata_is_current(previous_index, film_path=film, shots_path=shots_path, config_hash=config_hash)
        )
    rebuild_artifacts = args.force or not cache_compatible
    previous_by_shot = {item.shot_index: item for item in previous_index.shots} if previous_index else {}
    previous_embedding_dim = previous_index.meta.embedding_dim if previous_index else 0

    frame_requests: list[FrameRequest] = []
    frames_to_encode: list[Path] = []
    frame_meta: list[tuple[int, int, Path]] = []
    regenerated_embeddings: set[tuple[int, int]] = set()
    entries: list[dict] = []
    cache_hits: list[str] = []
    for shot in shots:
        keyframes: list[dict] = []
        previous_shot = previous_by_shot.get(shot.index)
        for keyframe_index, (tc, role) in enumerate(keyframe_times(shot, args.keyframes_per_shot)):
            frame_path = frame_dir / f"shot_{shot.index:06d}_k{keyframe_index}.jpg"
            embedding_path = emb_dir / f"shot_{shot.index:06d}_k{keyframe_index}.f16.npy"
            frame_changed = rebuild_artifacts or not frame_path.is_file()
            if frame_changed:
                frame_requests.append(FrameRequest(timestamp=tc, output_path=frame_path))
            else:
                cache_hits.append(f"frame/shot-{shot.index}-{keyframe_index}")
            embedding_valid = False
            if not rebuild_artifacts and not frame_changed and embedding_path.is_file():
                previous_frame = (
                    previous_shot.keyframes[keyframe_index]
                    if previous_shot is not None and keyframe_index < len(previous_shot.keyframes)
                    else None
                )
                embedding_valid = bool(
                    previous_frame is not None
                    and previous_frame.embedding_sha256
                    and previous_embedding_dim > 0
                    and vector_is_valid(
                        embedding_path,
                        embedding_dim=previous_embedding_dim,
                        expected_sha256=previous_frame.embedding_sha256,
                    )
                )
            if not embedding_valid:
                frames_to_encode.append(frame_path)
                frame_meta.append((shot.index, keyframe_index, embedding_path))
                regenerated_embeddings.add((shot.index, keyframe_index))
            else:
                cache_hits.append(f"embedding/shot-{shot.index}-{keyframe_index}")
            keyframes.append(
                {
                    "frame_path": frame_path,
                    "embedding_path": embedding_path,
                    "keyframe_index": keyframe_index,
                    "tc": tc,
                    "role": role,
                }
            )
        entries.append({"shot": shot, "keyframes": keyframes})

    extract_keyframes(film, frame_requests, mode=frame_sampling)
    if frames_to_encode:
        encoder = encoder or build_encoder(args)
        vectors = encoder.encode_images(frames_to_encode, batch_size=args.batch_size)
        if len(vectors) != len(frame_meta):
            raise VisualIndexError(f"visual encoder returned {len(vectors)} vectors for {len(frame_meta)} frames")
        for (_shot_index, _keyframe_index, embedding_path), vector in zip(frame_meta, vectors):
            save_vector(embedding_path, vector)

    previous_scale = previous_index.meta.logit_scale if previous_index else None
    previous_bias = previous_index.meta.logit_bias if previous_index else None
    if encoder is None and (previous_scale is None or previous_bias is None):
        encoder = build_encoder(args)
    logit_scale = float(getattr(encoder, "logit_scale", previous_scale or 1.0))
    logit_bias = float(getattr(encoder, "logit_bias", previous_bias or 0.0))

    output_shots: list[ShotVisualIndex] = []
    embedding_dim = 0
    for entry in entries:
        shot: Shot = entry["shot"]
        keyframe_models: list[ShotKeyframe] = []
        vectors: list[list[float]] = []
        for frame in entry["keyframes"]:
            embedding_path: Path = frame["embedding_path"]
            vector = load_vector(embedding_path)
            vectors.append(vector)
            keyframe_models.append(
                ShotKeyframe(
                    frame_path=relative_ref(frame["frame_path"], output_path.parent),
                    tc=round(float(frame["tc"]), 3),
                    role=str(frame["role"]),
                    embedding_ref=relative_ref(embedding_path, output_path.parent),
                    embedding_sha256=sha256_file(embedding_path),
                )
            )
        pooled = pool_vectors(vectors)
        embedding_dim = embedding_dim or len(pooled)
        shot_embedding_path = emb_dir / f"shot_{shot.index:06d}_pool.f16.npy"
        keyframe_changed = any(
            (shot.index, int(frame["keyframe_index"])) in regenerated_embeddings for frame in entry["keyframes"]
        )
        previous_shot = previous_by_shot.get(shot.index)
        pooled_valid = bool(
            previous_shot is not None
            and previous_shot.shot_embedding_sha256
            and vector_is_valid(
                shot_embedding_path,
                embedding_dim=embedding_dim,
                expected_sha256=previous_shot.shot_embedding_sha256,
            )
        )
        if rebuild_artifacts or keyframe_changed or not pooled_valid:
            shot_embedding_sha256 = save_vector(shot_embedding_path, pooled)
        else:
            cache_hits.append(f"embedding/shot-{shot.index}-pool")
            shot_embedding_sha256 = sha256_file(shot_embedding_path)
        output_shots.append(
            ShotVisualIndex(
                shot_index=shot.index,
                tc_start=shot.tc_start,
                tc_end=shot.tc_end,
                duration=shot.duration,
                is_story=shot.is_story,
                is_usable=shot.is_usable,
                keyframes=keyframe_models,
                shot_embedding_ref=relative_ref(shot_embedding_path, output_path.parent),
                shot_embedding_sha256=shot_embedding_sha256,
            )
        )

    meta = VisualIndexMeta(
        version="1.1",
        src=str(film),
        embedding_mode=args.embedding_mode,
        embedding_model=args.embedding_model,
        device=getattr(encoder, "device", args.device),
        embedding_dim=embedding_dim,
        keyframes_per_shot=args.keyframes_per_shot,
        n_shots=len(output_shots),
        created_at=datetime.now(timezone.utc),
        cache_hits=cache_hits,
        warnings=[],
        film_hash=media_identity_hash(film),
        shots_hash=file_hash(shots_path),
        config_hash=config_hash,
        preprocessing_version=PREPROCESSING_VERSION,
        logit_scale=logit_scale,
        logit_bias=logit_bias,
    )
    index = validate_shot_visual_index(ShotVisualIndexFile(meta=meta, shots=output_shots), shots)
    return validate_visual_index_artifacts(output_path, index, shots, require_frames=True, require_calibration=True)


def run_visual_index(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    validate_args(args)
    require_ffmpeg()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_key_path = work_dir / "config.hash"
    config_key = visual_index_config_hash(
        film_path=args.film.expanduser().resolve(),
        shots_path=args.shots.expanduser().resolve(),
        embedding_mode=args.embedding_mode,
        embedding_model=args.embedding_model,
        keyframes_per_shot=args.keyframes_per_shot,
        frame_sampling=args.frame_sampling,
    )
    previous_key = cache_key_path.read_text(encoding="utf-8").strip() if cache_key_path.is_file() else None
    index = build_visual_index(args, config_hash=config_key, cache_compatible=previous_key == config_key)
    write_json(output_path, index)
    cache_key_path.write_text(config_key + "\n", encoding="utf-8")
    logging.getLogger("visual_index").info("Done: %s", output_path)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_visual_index(args)
    except (VisualIndexError, VisualEncoderError, MediaError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"visual_index: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
