from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOTS = {
    "common",
    "episode_planner",
    "ingest",
    "match",
    "orchestrator",
    "preflight",
    "render",
    "review",
    "series_composer",
    "series_match",
    "series_recap",
    "shots",
    "storymap",
    "tts",
    "visual_index",
}
EXCLUDED_WHEEL_ROOTS = {"tests", "runs", "work", "data", "broll", "tts_align", "scripts"}
INGEST_ARTIFACTS = (
    "audio.wav",
    "transcript_aligned.json",
    "transcript_quality.json",
    "transcript_corrected.json",
    "transcript_correction.meta.json",
    "translated.json",
    "vision.json",
)


def resolve_release_work_dir(path: Path, *, root: Path = ROOT) -> Path:
    resolved_root = root.resolve()
    allowed_root = (resolved_root / "work").resolve()
    resolved = path.resolve() if path.is_absolute() else (resolved_root / path).resolve()
    if allowed_root not in resolved.parents:
        raise ValueError(f"release work dir must be a child of {allowed_root}: {resolved}")
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_ingest_cache(work_dir: Path) -> dict:
    manifest_path = work_dir / "cache_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts: dict[str, dict] = {}
    for name in INGEST_ARTIFACTS:
        path = work_dir / name
        artifacts[name] = {
            "exists": path.is_file(),
            "mtime_ns": path.stat().st_mtime_ns if path.is_file() else None,
            "sha256": sha256_file(path) if path.is_file() else None,
        }
    return {
        "cache_version": manifest.get("cache_version"),
        "keys": dict(manifest.get("keys", {})),
        "artifacts": artifacts,
    }


def artifact_unchanged(before: dict, after: dict, name: str) -> bool:
    return before["artifacts"][name] == after["artifacts"][name]


def artifact_rebuilt(before: dict, after: dict, name: str) -> bool:
    old = before["artifacts"][name]
    new = after["artifacts"][name]
    return bool(
        new["exists"]
        and (old["mtime_ns"] != new["mtime_ns"] or old["sha256"] != new["sha256"])
    )


def inspect_wheel(path: Path) -> dict:
    with ZipFile(path) as archive:
        names = set(archive.namelist())
    roots = {name.split("/", 1)[0] for name in names if "/" in name}
    missing = sorted(root for root in RUNTIME_ROOTS if not any(name.startswith(f"{root}/") for name in names))
    excluded = sorted(EXCLUDED_WHEEL_ROOTS & roots)
    has_run = "run.py" in names
    if missing or excluded or not has_run:
        raise ValueError(
            f"wheel content invalid: missing={missing}, excluded={excluded}, run.py={has_run}"
        )
    return {
        "wheel": path.name,
        "runtime_roots": sorted(RUNTIME_ROOTS),
        "entry_module": "run.py",
        "file_count": len(names),
        "excluded_roots_found": excluded,
    }
