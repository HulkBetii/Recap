from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path

from recap_ui.qa import build_delivery_qa
from recap_ui.repository import Repository
from recap_ui.schemas import (
    ArtifactKind,
    ArtifactRecord,
    DeliveryStatus,
    EpisodeDetail,
    EpisodeRecord,
    JobKind,
    JobStatus,
    RunRecord,
    RunRegistrationInspection,
    StageStatus,
)
from recap_ui.security import PathAccessError, PathRegistry


EPISODE_DIR_RE = re.compile(r"^(?:s\d{1,2}e\d{1,3}|e\d{1,3})$", re.IGNORECASE)
SKIP_DIRECTORY_NAMES = {"__pycache__", ".git", "temp_clips"}
ALLOWED_SUFFIXES = {
    ".json",
    ".jsonl",
    ".html",
    ".txt",
    ".log",
    ".yaml",
    ".yml",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".mp3",
    ".wav",
    ".mp4",
}


class RunNotFoundError(KeyError):
    pass


class ArtifactNotFoundError(KeyError):
    pass


class RunService:
    def __init__(
        self,
        repo_root: Path,
        repository: Repository,
        path_registry: PathRegistry,
    ) -> None:
        self.repo_root = repo_root.expanduser().resolve()
        self.repository = repository
        self.path_registry = path_registry

    def discover_runs(self) -> list[RunRecord]:
        paths: dict[str, Path] = {}
        default_root = self.repo_root / "runs"
        if default_root.is_dir():
            for child in default_root.iterdir():
                if child.is_dir() and self._looks_like_run(child):
                    paths[str(child.resolve()).casefold()] = child.resolve()
        for registered in self.repository.list_registered_runs():
            path = Path(registered.path).expanduser().resolve()
            if path.is_dir() and self._looks_like_run(path):
                paths[str(path).casefold()] = path
        records: list[RunRecord] = []
        for path in paths.values():
            try:
                records.append(self._build_record(path))
            except (OSError, PathAccessError):
                continue
        return sorted(records, key=lambda item: item.modified_at, reverse=True)

    def get_run(self, run_id: str) -> RunRecord:
        for run in self.discover_runs():
            if run.id == run_id:
                return run
        raise RunNotFoundError(run_id)

    def inspect_registration(self, path: Path) -> RunRegistrationInspection:
        resolved = path.expanduser().resolve()
        recognizable = resolved.is_dir() and self._looks_like_run(resolved)
        if not recognizable:
            raise ValueError("directory does not contain a recognizable run")
        kind = JobKind.SERIES if (resolved / "series_recap").is_dir() else JobKind.SINGLE
        output = resolved / "series_recap" / "series_recap.mp4" if kind == JobKind.SERIES else resolved / "recap.mp4"
        artifact_count = sum(
            1
            for candidate in resolved.rglob("*")
            if candidate.is_file()
            and candidate.suffix.lower() in ALLOWED_SUFFIXES
            and not any(part in SKIP_DIRECTORY_NAMES for part in candidate.relative_to(resolved).parts)
        )
        return RunRegistrationInspection(
            path_token=self.path_registry.token_for(resolved),
            kind=kind,
            recognizable=True,
            default_title=_artifact_display_title(resolved, kind) or resolved.name,
            output_available=output.is_file(),
            artifact_count=artifact_count,
        )

    def list_artifacts(self, run_id: str, *, limit: int = 5000) -> list[ArtifactRecord]:
        run_path = self._path_for_run(run_id)
        artifacts: list[ArtifactRecord] = []
        paths = [
            path
            for path in run_path.rglob("*")
            if path.is_file()
            and not any(part in SKIP_DIRECTORY_NAMES for part in path.relative_to(run_path).parts)
            and path.suffix.lower() in ALLOWED_SUFFIXES
        ]
        paths.sort(key=lambda path: _artifact_priority(run_path, path))
        for path in paths[: max(1, min(limit, 20000))]:
            if not path.is_file() or any(part in SKIP_DIRECTORY_NAMES for part in path.relative_to(run_path).parts):
                continue
            if path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            stat = path.stat()
            relative = path.relative_to(run_path).as_posix()
            kind, media_type = _artifact_type(path)
            artifacts.append(
                ArtifactRecord(
                    id=_artifact_id(run_id, relative),
                    run_id=run_id,
                    relative_path=relative,
                    name=path.name,
                    kind=kind,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    media_type=media_type,
                    previewable=kind != ArtifactKind.BINARY,
                )
            )
        return sorted(artifacts, key=lambda item: item.relative_path.casefold())

    def resolve_artifact(self, run_id: str, artifact_id: str) -> Path:
        run_path = self._path_for_run(run_id)
        for artifact in self.list_artifacts(run_id):
            if artifact.id != artifact_id:
                continue
            resolved = (run_path / artifact.relative_path).resolve()
            try:
                resolved.relative_to(run_path)
            except ValueError as exc:
                raise ArtifactNotFoundError(artifact_id) from exc
            if not resolved.is_file():
                break
            return resolved
        raise ArtifactNotFoundError(artifact_id)

    def episodes(self, run_id: str) -> list[EpisodeRecord]:
        run_path = self._path_for_run(run_id)
        records: list[EpisodeRecord] = []
        directories = [run_path] if not (run_path / "series_recap").is_dir() else [
            child for child in run_path.iterdir() if child.is_dir() and EPISODE_DIR_RE.match(child.name)
        ]
        for directory in sorted(directories, key=lambda item: item.name):
            meta = _read_json(directory / "episode_meta.json")
            film_meta = _read_json(directory / "film_map.meta.json")
            records.append(
                EpisodeRecord(
                    episode_key=str(meta.get("episode_key") or directory.name) if isinstance(meta, dict) else directory.name,
                    title=meta.get("title") if isinstance(meta, dict) else None,
                    episode_number=meta.get("episode_number") if isinstance(meta, dict) else None,
                    stage_statuses=_episode_stage_statuses(directory),
                    translation_ratio=film_meta.get("translation_success_ratio") if isinstance(film_meta, dict) else None,
                    approximate_timecodes=film_meta.get("approximate_timecodes") if isinstance(film_meta, dict) else None,
                )
            )
        return records

    def get_episode_detail(self, run_id: str, episode_key: str) -> EpisodeDetail:
        run_path = self._path_for_run(run_id)
        record = self.get_run(run_id)
        if record.kind == JobKind.SINGLE:
            directory = run_path
            episodes = self.episodes(run_id)
            if not episodes or episode_key not in {episodes[0].episode_key, "single"}:
                raise RunNotFoundError(episode_key)
            episode = episodes[0]
        else:
            if not EPISODE_DIR_RE.match(episode_key):
                raise RunNotFoundError(episode_key)
            directory = (run_path / episode_key).resolve()
            try:
                directory.relative_to(run_path)
            except ValueError as exc:
                raise RunNotFoundError(episode_key) from exc
            if not directory.is_dir():
                raise RunNotFoundError(episode_key)
            episode = next((item for item in self.episodes(run_id) if item.episode_key == episode_key), None)
            if episode is None:
                raise RunNotFoundError(episode_key)
        metadata = {
            "film_map": _read_json(directory / "film_map.meta.json") or {},
            "story_map": _read_json(directory / "story_map.meta.json") or {},
            "review": _read_json(directory / "review_script.meta.json") or {},
            "tts": _read_json(directory / "tts_meta.json") or {},
            "shots": _read_json(directory / "shots.meta.json") or {},
        }
        return EpisodeDetail(
            episode=episode,
            film_map=_json_list(directory / "film_map.json"),
            story_map=_json_list(directory / "story_map.json"),
            review_script=_json_list(directory / "review_script.json"),
            beats_timing=_json_list(directory / "beats_timing.json"),
            shots=_json_list(directory / "shots.json"),
            metadata=metadata,
        )

    def qa(self, run_id: str):  # type: ignore[no-untyped-def]
        record = self.get_run(run_id)
        return build_delivery_qa(self._path_for_run(run_id), record.kind, run_id=run_id)

    def _path_for_run(self, run_id: str) -> Path:
        for run in self.discover_runs():
            if run.id == run_id:
                return self.path_registry.resolve(run.path_token, expect="directory")
        raise RunNotFoundError(run_id)

    def _build_record(self, path: Path) -> RunRecord:
        run_id = _run_id(path)
        kind = JobKind.SERIES if (path / "series_recap").is_dir() else JobKind.SINGLE
        artifacts = self._output_artifacts(path, run_id, kind)
        jobs = [job for job in self.repository.list_jobs(limit=1000) if _same_path(Path(job.run_dir), path)]
        registered = next(
            (item for item in self.repository.list_registered_runs() if _same_path(Path(item.path), path)),
            None,
        )
        execution_status = jobs[0].status if jobs else self._inferred_execution_status(path, kind)
        qa = build_delivery_qa(path, kind, run_id=run_id, media_probe=_metadata_media_probe)
        episode_keys = [item.name for item in path.iterdir() if item.is_dir() and EPISODE_DIR_RE.match(item.name)]
        candidate_paths = [path]
        if kind == JobKind.SERIES:
            candidate_paths.extend(
                path / "series_recap" / name
                for name in ("series_recap.mp4", "summary.json", "render.meta.json", "series_recap.log")
            )
        else:
            candidate_paths.extend(path / name for name in ("recap.mp4", "summary.json", "render.meta.json", "run.log"))
        modified_timestamp = max(candidate.stat().st_mtime for candidate in candidate_paths if candidate.exists())
        artifact_title = _artifact_display_title(path, kind)
        display_title = (
            ((jobs[0].display_title if jobs else None) or (registered.display_title if registered else None))
            if jobs
            else ((registered.display_title if registered else None) or artifact_title or path.name)
        )
        management_mode = "managed" if jobs else "artifact_only"
        if management_mode == "managed":
            if jobs[0].status in {
                JobStatus.QUEUED,
                JobStatus.STARTING,
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }:
                available_actions = ["view", "cancel"]
            elif jobs[0].status == JobStatus.SUCCEEDED:
                available_actions = ["view", "rerun"]
            elif jobs[0].status in {
                JobStatus.FAILED,
                JobStatus.INTERRUPTED,
                JobStatus.CANCELLED,
                JobStatus.BLOCKED,
            }:
                available_actions = ["view", "resume"]
            else:
                available_actions = ["view"]
            read_only_reason = None
        else:
            available_actions = ["view"]
            read_only_reason = "This run was discovered from artifacts and has no managed job record."
        return RunRecord(
            id=run_id,
            kind=kind,
            name=path.name,
            display_title=display_title,
            path_token=self.path_registry.token_for(path),
            job_id=jobs[0].id if jobs else None,
            episode_keys=sorted(episode_keys),
            output_artifact_id=artifacts[0].id if artifacts else None,
            execution_status=execution_status,
            delivery_status=qa.status,
            management_mode=management_mode,
            available_actions=available_actions,
            read_only_reason=read_only_reason,
            modified_at=datetime.fromtimestamp(modified_timestamp, tz=timezone.utc),
        )

    @staticmethod
    def _output_artifacts(path: Path, run_id: str, kind: JobKind) -> list[ArtifactRecord]:
        output = path / "series_recap" / "series_recap.mp4" if kind == JobKind.SERIES else path / "recap.mp4"
        if not output.is_file():
            return []
        stat = output.stat()
        return [
            ArtifactRecord(
                id=_artifact_id(run_id, output.relative_to(path).as_posix()),
                run_id=run_id,
                relative_path=output.relative_to(path).as_posix(),
                name=output.name,
                kind=ArtifactKind.VIDEO,
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                media_type="video/mp4",
            )
        ]

    @staticmethod
    def _looks_like_run(path: Path) -> bool:
        markers = (
            path / "summary.json",
            path / "film_map.json",
            path / "recap.mp4",
            path / "series_recap",
        )
        return any(marker.exists() for marker in markers)

    @staticmethod
    def _inferred_execution_status(path: Path, kind: JobKind) -> JobStatus | None:
        output = path / "series_recap" / "series_recap.mp4" if kind == JobKind.SERIES else path / "recap.mp4"
        if output.is_file():
            return JobStatus.SUCCEEDED
        if (path / "summary.json").is_file() or (path / "series_recap" / "summary.json").is_file():
            return JobStatus.INTERRUPTED
        return None


def _episode_stage_statuses(path: Path) -> dict[str, StageStatus]:
    markers = {
        "preflight": ("video_profile.json",),
        "ingest": ("film_map.json", "film_map.meta.json"),
        "storymap": ("story_map.json", "story_map.meta.json"),
        "episode_planner": ("episode_meta.json", "episode_memory.json"),
        "review": ("review_script.json", "review_script.meta.json"),
        "tts": ("voiceover.mp3", "beats_timing.json", "tts_meta.json"),
        "shots": ("shots.json", "shots.meta.json"),
        "match": ("edl.json", "edl.meta.json"),
        "render": ("recap.mp4", "render.meta.json"),
    }
    return {
        stage: StageStatus.SUCCEEDED if all((path / name).is_file() for name in names) else StageStatus.PENDING
        for stage, names in markers.items()
    }


def _artifact_type(path: Path) -> tuple[ArtifactKind, str]:
    suffix = path.suffix.lower()
    if suffix in {".json", ".jsonl", ".yaml", ".yml"}:
        return ArtifactKind.JSON, "application/json"
    if suffix == ".html":
        return ArtifactKind.HTML, "text/html"
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ArtifactKind.IMAGE, mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if suffix in {".mp3", ".wav"}:
        return ArtifactKind.AUDIO, mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    if suffix == ".mp4":
        return ArtifactKind.VIDEO, "video/mp4"
    if suffix == ".log":
        return ArtifactKind.LOG, "text/plain"
    if suffix == ".txt":
        return ArtifactKind.TEXT, "text/plain"
    return ArtifactKind.BINARY, "application/octet-stream"


def _run_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:24]


def _same_path(left: Path, right: Path) -> bool:
    return str(left.expanduser().resolve()).casefold() == str(right.expanduser().resolve()).casefold()


def _artifact_display_title(path: Path, kind: JobKind) -> str | None:
    candidates = [path / "series_recap" / "summary.json", path / "summary.json"] if kind == JobKind.SERIES else [path / "summary.json"]
    for candidate in candidates:
        payload = _read_json(candidate)
        if not isinstance(payload, dict):
            continue
        for key in ("display_title", "title", "series_title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())
    return None


def _artifact_id(run_id: str, relative_path: str) -> str:
    return hashlib.sha256(f"{run_id}:{relative_path}".encode("utf-8")).hexdigest()[:32]


def _artifact_priority(run_root: Path, path: Path) -> tuple[int, str]:
    relative = path.relative_to(run_root).as_posix()
    if relative in {"recap.mp4", "series_recap/series_recap.mp4"}:
        return 0, relative.casefold()
    if relative.startswith("series_recap/"):
        return 1, relative.casefold()
    if "/" not in relative:
        return 2, relative.casefold()
    if path.suffix.lower() in {".json", ".jsonl", ".html", ".txt", ".log", ".yaml", ".yml"}:
        return 3, relative.casefold()
    if path.suffix.lower() in {".mp3", ".wav", ".mp4"}:
        return 4, relative.casefold()
    return 5, relative.casefold()


def _read_json(path: Path):  # type: ignore[no-untyped-def]
    import json

    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _json_list(path: Path) -> list[dict[str, object]]:
    payload = _read_json(path)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _metadata_media_probe(path: Path) -> dict[str, float | int]:
    if path.suffix.lower() == ".mp3":
        return {"duration": 0.0, "audio_duration": 0.0, "audio_streams": 1, "width": 0, "height": 0, "fps": 0.0}
    meta_path = path.with_name("render.meta.json")
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        raise RuntimeError("render metadata is unavailable")
    return {
        "width": int(meta.get("width") or 0),
        "height": int(meta.get("height") or 0),
        "fps": float(meta.get("fps") or 0.0),
        "duration": float(meta.get("video_duration_s") or 0.0),
        "audio_streams": 1,
        "audio_duration": float(meta.get("audio_duration_s") or 0.0),
    }
