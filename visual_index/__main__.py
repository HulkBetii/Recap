from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from common.media import MediaError, extract_frame, require_ffmpeg
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
from match.cache import file_hash, stable_hash
from match.inputs import load_shots
from visual_index.encoder import DEFAULT_VISUAL_MODEL, TransformerVisualEncoder, VisualEncoder, VisualEncoderError, normalize_vector

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

def output_is_valid(output_path: Path, shots: list[Shot]) -> bool:
    if not output_path.is_file():
        return False
    try:
        index = ShotVisualIndexFile.model_validate_json(output_path.read_text(encoding="utf-8"))
        validate_shot_visual_index(index, shots)
        refs = [item.shot_embedding_ref for item in index.shots]
        refs.extend(frame.embedding_ref for item in index.shots for frame in item.keyframes)
        return all(resolve_ref(output_path, ref).is_file() for ref in refs)
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

def resolve_ref(index_path: Path, ref: str) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else index_path.parent / path

def save_vector(path: Path, vector: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.asarray(normalize_vector(vector), dtype=np.float16))

def load_vector(path: Path) -> list[float]:
    return normalize_vector([float(value) for value in np.load(path).astype("float32").tolist()])

def pool_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    width = len(vectors[0])
    pooled = [0.0] * width
    for vector in vectors:
        for index, value in enumerate(vector):
            pooled[index] += value
    return normalize_vector([value / len(vectors) for value in pooled])

def build_encoder(args: argparse.Namespace) -> VisualEncoder:
    trust_remote_code = args.embedding_mode == "jina-clip-v2"
    return TransformerVisualEncoder(args.embedding_model, device=args.device, trust_remote_code=trust_remote_code)

def build_visual_index(args: argparse.Namespace, encoder: VisualEncoder | None = None) -> ShotVisualIndexFile:
    film = args.film.expanduser().resolve()
    shots_path = args.shots.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    asset_dir = args.asset_dir.expanduser().resolve()
    frame_dir = asset_dir / "frames"
    emb_dir = asset_dir / "emb"
    shots = validate_shots(load_shots(shots_path))
    if not args.force and output_is_valid(output_path, shots):
        return ShotVisualIndexFile.model_validate_json(output_path.read_text(encoding="utf-8"))

    frames_to_encode: list[Path] = []
    frame_meta: list[tuple[int, int, Path]] = []
    entries: list[dict] = []
    cache_hits: list[str] = []
    for shot in shots:
        keyframes = []
        for keyframe_index, (tc, role) in enumerate(keyframe_times(shot, args.keyframes_per_shot)):
            frame_path = frame_dir / f"shot_{shot.index:06d}_k{keyframe_index}.jpg"
            frame_embedding_path = emb_dir / f"shot_{shot.index:06d}_k{keyframe_index}.f16.npy"
            if args.force or not frame_path.is_file():
                extract_frame(film, tc, frame_path)
            else:
                cache_hits.append(f"frame/shot-{shot.index}-{keyframe_index}")
            if args.force or not frame_embedding_path.is_file():
                frames_to_encode.append(frame_path)
                frame_meta.append((shot.index, keyframe_index, frame_embedding_path))
            else:
                cache_hits.append(f"embedding/shot-{shot.index}-{keyframe_index}")
            keyframes.append(
                ShotKeyframe(
                    frame_path=relative_ref(frame_path, output_path.parent),
                    tc=round(tc, 3),
                    role=role,
                    embedding_ref=relative_ref(frame_embedding_path, output_path.parent),
                )
            )
        entries.append({"shot": shot, "keyframes": keyframes})

    if frames_to_encode:
        encoder = encoder or build_encoder(args)
        vectors = encoder.encode_images(frames_to_encode, batch_size=args.batch_size)
        for (_shot_index, _keyframe_index, embedding_path), vector in zip(frame_meta, vectors):
            save_vector(embedding_path, vector)
    else:
        encoder = encoder or build_encoder(args)

    output_shots: list[ShotVisualIndex] = []
    embedding_dim = 0
    for entry in entries:
        shot: Shot = entry["shot"]
        keyframes: list[ShotKeyframe] = entry["keyframes"]
        vectors = [load_vector(resolve_ref(output_path, frame.embedding_ref)) for frame in keyframes]
        pooled = pool_vectors(vectors)
        embedding_dim = embedding_dim or len(pooled)
        shot_embedding_path = emb_dir / f"shot_{shot.index:06d}_pool.f16.npy"
        if args.force or not shot_embedding_path.is_file():
            save_vector(shot_embedding_path, pooled)
        else:
            cache_hits.append(f"embedding/shot-{shot.index}-pool")
        output_shots.append(
            ShotVisualIndex(
                shot_index=shot.index,
                tc_start=shot.tc_start,
                tc_end=shot.tc_end,
                duration=shot.duration,
                is_story=shot.is_story,
                is_usable=shot.is_usable,
                keyframes=keyframes,
                shot_embedding_ref=relative_ref(shot_embedding_path, output_path.parent),
            )
        )

    meta = VisualIndexMeta(
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
    )
    return validate_shot_visual_index(ShotVisualIndexFile(meta=meta, shots=output_shots), shots)

def run_visual_index(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    validate_args(args)
    require_ffmpeg()
    output_path = args.output.expanduser().resolve()
    args.work_dir.expanduser().resolve().mkdir(parents=True, exist_ok=True)
    cache_key_path = args.work_dir.expanduser().resolve() / "config.hash"
    config_key = stable_hash({
        "film": file_hash(args.film.expanduser().resolve()),
        "shots": file_hash(args.shots.expanduser().resolve()),
        "embedding_mode": args.embedding_mode,
        "embedding_model": args.embedding_model,
        "device": args.device,
        "keyframes_per_shot": args.keyframes_per_shot,
    })
    if args.force and cache_key_path.exists():
        cache_key_path.unlink()
    index = build_visual_index(args)
    write_json(output_path, index)
    cache_key_path.write_text(config_key + "\n", encoding="utf-8")
    logging.getLogger("visual_index").info("Done: %s", output_path)
    return 0

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run_visual_index(args)
    except (VisualIndexError, VisualEncoderError, MediaError, ValueError) as exc:
        parser.exit(2, f"visual_index: error: {exc}\n")

if __name__ == "__main__":
    raise SystemExit(main())
