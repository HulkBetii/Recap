from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psutil

from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from recap_ui.database import Database
from recap_ui.process_control import ProcessIdentity, identity_for_process, popen_command, process_matches, terminate_process_tree
from recap_ui.repository import Repository
from recap_ui.schemas import EventType, JobKind, JobRecord, JobStageRecord, JobStatus, StageStatus

HEARTBEAT_INTERVAL_S = 2.0
ARTIFACT_SCAN_INTERVAL_S = 2.0
PROFILE_RESOURCE = "profile:PROFILE_GPT_1"
PROFILE_DIR = CHATGPT_PLAYWRIGHT_PROFILE_DIR
SECRET_PATTERN = re.compile(r"(?i)(sk-[a-z0-9_-]{12,}|(?:OPENAI|VIVOO|GENMAX)_API_KEY\s*[=:]\s*\S+)")
STAGE_LINE_PATTERN = re.compile(r"^\[(?P<status>[^]]+)]\s+(?P<stage>[^\s]+)")


class JobWorker:
    def __init__(self, *, repository: Repository, repo_root: Path, state_dir: Path, worker_id: str | None = None) -> None:
        self.repository = repository
        self.repo_root = repo_root.resolve()
        self.state_dir = state_dir.resolve()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.log_dir = self.state_dir / "logs"
        self._validation_error_jobs: set[str] = set()
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def run_forever(self, stop_event: Any, *, poll_interval_s: float = 0.5) -> None:
        self.reconcile_running_jobs()
        while not stop_event.is_set():
            job = self.repository.claim_next_job(self.worker_id)
            if job is None:
                stop_event.wait(poll_interval_s)
                continue
            self.run_job(job)

    def run_job(self, job: JobRecord) -> None:
        acquired: list[str] = []
        resources = ["pipeline", f"run:{_normalized_run_dir(job.run_dir)}", PROFILE_RESOURCE]
        try:
            profile_test_lock = self.state_dir / "profile_test.lock"
            wait_deadline = time.monotonic() + 30.0
            while _marker_owner_alive(profile_test_lock) and time.monotonic() < wait_deadline:
                time.sleep(0.2)
            if _marker_owner_alive(profile_test_lock):
                self._finish(
                    job.id,
                    JobStatus.BLOCKED,
                    error_code="profile_test_in_progress",
                    error_message="ChatGPT profile test did not release its lock within 30 seconds",
                )
                return
            profile_users = _processes_using_profile(PROFILE_DIR)
            if profile_users:
                self._finish(
                    job.id,
                    JobStatus.BLOCKED,
                    error_code="profile_in_use",
                    error_message=f"PROFILE_GPT_1 is already used by process(es): {profile_users}",
                )
                return
            for resource in resources:
                if not self.repository.acquire_lock(resource, job.id):
                    self._finish(
                        job.id,
                        JobStatus.BLOCKED,
                        error_code="resource_locked",
                        error_message=f"resource is already locked: {resource}",
                    )
                    return
                acquired.append(resource)
            self._execute(job)
        except Exception as exc:
            self._finish(
                job.id,
                JobStatus.FAILED,
                error_code="worker_error",
                error_message=_redact(str(exc))[:2000],
            )
        finally:
            if acquired:
                self.repository.release_locks(job.id)

    def reconcile_running_jobs(self) -> None:
        for job in self.repository.list_jobs(limit=1000):
            if job.status not in {JobStatus.STARTING, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}:
                continue
            identity = _identity_from_job(job)
            if identity is not None and process_matches(identity):
                try:
                    self._monitor_existing(job, identity)
                finally:
                    self.repository.release_locks(job.id)
                continue
            status = JobStatus.SUCCEEDED if _outputs_complete(job) else JobStatus.INTERRUPTED
            self._finish(
                job.id,
                status,
                error_code=None if status == JobStatus.SUCCEEDED else "worker_restart",
                error_message=None if status == JobStatus.SUCCEEDED else "pipeline process stopped before outputs completed",
            )
            self.repository.release_locks(job.id)

    def _monitor_existing(self, job: JobRecord, identity: ProcessIdentity) -> None:
        self.repository.update_job(job.id, status=JobStatus.RUNNING, worker_id=self.worker_id)
        self.repository.append_event(
            job.id,
            EventType.JOB_STATE,
            {"status": JobStatus.RUNNING, "reason": "worker_restart_reattached", "pid": identity.pid},
            level="WARNING",
        )
        launcher_log = self.log_dir / f"{job.id}.log"
        tailers = _log_tailers(job)
        known_artifacts: dict[str, tuple[int, int]] = {}
        with launcher_log.open("a", encoding="utf-8") as log_file:
            while process_matches(identity):
                current = self.repository.get_job(job.id)
                if current is not None and (current.cancel_requested or current.status == JobStatus.CANCEL_REQUESTED):
                    terminate_process_tree(identity)
                    self._finish(job.id, JobStatus.CANCELLED)
                    return
                self.repository.update_job(job.id, heartbeat_at=_utc_now())
                self.repository.append_event(job.id, EventType.HEARTBEAT, {"pid": identity.pid, "reattached": True})
                self._tail_pipeline_logs(job.id, tailers, log_file)
                known_artifacts = self._emit_new_artifacts(job, known_artifacts)
                self._poll_stage_validation(job)
                time.sleep(HEARTBEAT_INTERVAL_S)
        status = JobStatus.SUCCEEDED if _outputs_complete(job) else JobStatus.INTERRUPTED
        self._finish(
            job.id,
            status,
            error_code=None if status == JobStatus.SUCCEEDED else "process_lost_after_restart",
            error_message=None if status == JobStatus.SUCCEEDED else "reattached process exited without complete outputs",
        )

    def _execute(self, job: JobRecord) -> None:
        launcher_log = self.log_dir / f"{job.id}.log"
        launcher_log.parent.mkdir(parents=True, exist_ok=True)
        process = popen_command(job.argv, cwd=self.repo_root, env=os.environ.copy())
        identity = identity_for_process(process, job.argv)
        now = _utc_now()
        self.repository.update_job(
            job.id,
            status=JobStatus.RUNNING,
            pid=identity.pid,
            process_create_time=identity.create_time,
            started_at=job.started_at or now,
            heartbeat_at=now,
        )
        self.repository.append_event(
            job.id,
            EventType.JOB_STATE,
            {"status": JobStatus.RUNNING, "pid": identity.pid, "attempt": job.attempt},
        )

        output_queue: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=_read_output, args=(process.stdout, output_queue), daemon=True)
        reader.start()
        next_heartbeat = 0.0
        next_artifact_scan = 0.0
        known_artifacts: dict[str, tuple[int, int]] = {}
        tailers = _log_tailers(job)
        cancelled = False

        with launcher_log.open("a", encoding="utf-8") as log_file:
            log_file.write("$ " + " ".join(job.argv) + "\n")
            while process.poll() is None:
                self._drain_output(job.id, output_queue, log_file)
                now_monotonic = time.monotonic()
                if now_monotonic >= next_heartbeat:
                    current = self.repository.get_job(job.id)
                    if current is not None and (current.cancel_requested or current.status == JobStatus.CANCEL_REQUESTED):
                        cancelled = terminate_process_tree(identity)
                        break
                    heartbeat = _utc_now()
                    self.repository.update_job(job.id, heartbeat_at=heartbeat)
                    self.repository.append_event(job.id, EventType.HEARTBEAT, {"pid": identity.pid})
                    self._tail_pipeline_logs(job.id, tailers, log_file)
                    next_heartbeat = now_monotonic + HEARTBEAT_INTERVAL_S
                if now_monotonic >= next_artifact_scan:
                    known_artifacts = self._emit_new_artifacts(job, known_artifacts)
                    self._poll_stage_validation(job)
                    next_artifact_scan = now_monotonic + ARTIFACT_SCAN_INTERVAL_S
                time.sleep(0.1)

            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                terminate_process_tree(identity, grace_s=0.1)
                process.wait(timeout=5.0)
            reader.join(timeout=2.0)
            self._drain_output(job.id, output_queue, log_file, drain_all=True)
            self._tail_pipeline_logs(job.id, tailers, log_file)
            self._emit_new_artifacts(job, known_artifacts)
            self._poll_stage_validation(job)

        if cancelled or self._cancel_was_requested(job.id):
            self._finish(job.id, JobStatus.CANCELLED, exit_code=process.returncode)
        elif process.returncode == 0:
            self._finish(job.id, JobStatus.SUCCEEDED, exit_code=0)
        else:
            self._finish(
                job.id,
                JobStatus.FAILED,
                exit_code=process.returncode,
                error_code="pipeline_exit_nonzero",
                error_message=f"pipeline exited with code {process.returncode}",
            )

    def _drain_output(
        self,
        job_id: str,
        output_queue: queue.Queue[str | None],
        log_file: Any,
        *,
        drain_all: bool = False,
    ) -> None:
        lines: list[str] = []
        while len(lines) < 1000:
            try:
                line = output_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            lines.append(_redact(line.rstrip("\r\n")))
        if not lines:
            return
        for line in lines:
            log_file.write(line + "\n")
            match = STAGE_LINE_PATTERN.match(line)
            if match:
                self.repository.append_event(
                    job_id,
                    EventType.STAGE_STATE,
                    {"status": match.group("status")},
                    stage=match.group("stage"),
                )
        log_file.flush()
        self.repository.append_event(job_id, EventType.LOG, {"lines": lines}, level="INFO")
        if drain_all and not output_queue.empty():
            self._drain_output(job_id, output_queue, log_file, drain_all=True)

    def _tail_pipeline_logs(self, job_id: str, tailers: list["LogTailer"], log_file: Any) -> None:
        for tailer in tailers:
            lines = [_redact(line) for line in tailer.read_new_lines()]
            if not lines:
                continue
            for line in lines:
                log_file.write(f"[{tailer.path.name}] {line}\n")
            log_file.flush()
            self.repository.append_event(
                job_id,
                EventType.LOG,
                {"source": str(tailer.path), "lines": lines},
            )

    def _emit_new_artifacts(
        self,
        job: JobRecord,
        previous: dict[str, tuple[int, int]],
    ) -> dict[str, tuple[int, int]]:
        current = _artifact_snapshot(Path(job.run_dir))
        changed = [path for path, stat in current.items() if previous.get(path) != stat]
        if changed:
            self.repository.append_event(
                job.id,
                EventType.ARTIFACT,
                {"paths": changed[:100], "changed_count": len(changed)},
            )
        return current

    def _poll_stage_validation(self, job: JobRecord) -> None:
        try:
            validated = _validated_stages(job)
            existing = {
                (stage.stage_key, stage.episode_key): stage.status
                for stage in self.repository.list_stages(job.id)
                if stage.attempt == job.attempt
            }
        except Exception as exc:
            if job.id not in self._validation_error_jobs:
                self._validation_error_jobs.add(job.id)
                self.repository.append_event(
                    job.id,
                    EventType.QA,
                    {"code": "stage_validation_poll_failed", "message": _redact(str(exc))[:1000]},
                    level="WARNING",
                )
            return
        self._validation_error_jobs.discard(job.id)
        active_job = self.repository.get_job(job.id)
        active = bool(active_job and active_job.status in {JobStatus.STARTING, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED})
        first_invalid = next((index for index, item in enumerate(validated) if not item[2]), None)
        for index, (stage_key, episode_key, valid) in enumerate(validated):
            desired = StageStatus.SUCCEEDED if valid else (
                StageStatus.RUNNING if active and index == first_invalid else StageStatus.PENDING
            )
            previous = existing.get((stage_key, episode_key))
            if previous == StageStatus.SUCCEEDED or previous == desired:
                continue
            now = _utc_now()
            record = JobStageRecord(
                job_id=job.id,
                stage_key=stage_key,
                episode_key=episode_key,
                attempt=job.attempt,
                status=desired,
                started_at=job.started_at if desired == StageStatus.SUCCEEDED else (now if desired == StageStatus.RUNNING else None),
                finished_at=now if desired == StageStatus.SUCCEEDED else None,
                validator_result={"valid": valid, "source": "pipeline_validator"},
            )
            self.repository.upsert_stage(record)
            self.repository.append_event(
                job.id,
                EventType.STAGE_STATE,
                {"status": desired, "episode_key": episode_key, "validated": valid},
                stage=stage_key,
            )

    def _cancel_was_requested(self, job_id: str) -> bool:
        job = self.repository.get_job(job_id)
        return bool(job and (job.cancel_requested or job.status == JobStatus.CANCEL_REQUESTED))

    def _finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        job = self.repository.update_job(
            job_id,
            status=status,
            exit_code=exit_code,
            error_code=error_code,
            error_message=error_message,
            finished_at=_utc_now(),
            heartbeat_at=_utc_now(),
        )
        self.repository.append_event(
            job_id,
            EventType.JOB_STATE,
            {"status": status, "exit_code": exit_code, "error_code": error_code, "error_message": error_message},
            level="ERROR" if status in {JobStatus.FAILED, JobStatus.BLOCKED} else "INFO",
        )
        stage_terminal = {
            JobStatus.SUCCEEDED: StageStatus.WARNING,
            JobStatus.FAILED: StageStatus.FAILED,
            JobStatus.BLOCKED: StageStatus.BLOCKED,
            JobStatus.CANCELLED: StageStatus.CANCELLED,
            JobStatus.INTERRUPTED: StageStatus.FAILED,
        }.get(status)
        if stage_terminal is not None:
            for stage in self.repository.list_stages(job_id):
                if stage.attempt != job.attempt or stage.status != StageStatus.RUNNING:
                    continue
                updated = stage.model_copy(
                    update={
                        "status": stage_terminal,
                        "finished_at": _utc_now(),
                        "exit_code": exit_code,
                        "error": error_message,
                    }
                )
                self.repository.upsert_stage(updated)
                self.repository.append_event(
                    job_id,
                    EventType.STAGE_STATE,
                    {"status": stage_terminal, "episode_key": stage.episode_key},
                    level="ERROR" if stage_terminal in {StageStatus.FAILED, StageStatus.BLOCKED} else "INFO",
                    stage=stage.stage_key,
                )
        self._emit_delivery_qa(job)

    def _emit_delivery_qa(self, job: JobRecord) -> None:
        try:
            from recap_ui.qa import build_delivery_qa

            report = build_delivery_qa(Path(job.run_dir), job.kind, config=job.config_snapshot)
            self.repository.append_event(
                job.id,
                EventType.QA,
                {
                    "status": report.status,
                    "checks": [{"code": check.code, "status": check.status} for check in report.checks],
                    "metrics": report.metrics,
                },
                level="ERROR" if report.status.value == "block" else "INFO",
            )
        except Exception as exc:
            self.repository.append_event(
                job.id,
                EventType.QA,
                {"status": "unknown", "code": "delivery_qa_failed", "message": _redact(str(exc))[:1000]},
                level="WARNING",
            )


class LogTailer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def read_new_lines(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                text = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []
        return text.splitlines()


def run_worker_process(*, database_path: Path, repo_root: Path, state_dir: Path, stop_event: Any) -> None:
    database = Database(database_path)
    database.initialize()
    worker = JobWorker(repository=Repository(database), repo_root=repo_root, state_dir=state_dir)
    worker.run_forever(stop_event)


def _read_output(stream: Iterable[str] | None, output_queue: queue.Queue[str | None]) -> None:
    if stream is not None:
        for line in stream:
            output_queue.put(line)
    output_queue.put(None)


def _identity_from_job(job: JobRecord) -> ProcessIdentity | None:
    if job.pid is None or job.process_create_time is None:
        return None
    return ProcessIdentity(pid=job.pid, create_time=job.process_create_time, command_hash=job.command_hash)


def _normalized_run_dir(run_dir: str) -> str:
    return str(Path(run_dir).resolve()).casefold()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(line: str) -> str:
    return SECRET_PATTERN.sub("[REDACTED]", line)


def _artifact_snapshot(run_dir: Path) -> dict[str, tuple[int, int]]:
    if not run_dir.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in run_dir.rglob("*"):
        if len(snapshot) >= 5000:
            break
        if not path.is_file() or any(part in {"temp_clips", "local_asr_chunks", "__pycache__"} for part in path.parts):
            continue
        try:
            stat = path.stat()
            snapshot[path.relative_to(run_dir).as_posix()] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    return snapshot


def _log_tailers(job: JobRecord) -> list[LogTailer]:
    run_dir = Path(job.run_dir)
    candidates = [run_dir / "run.log", run_dir / "series_recap.log", run_dir / "series_recap" / "series_recap.log"]
    return [LogTailer(path) for path in candidates]


def _outputs_complete(job: JobRecord) -> bool:
    try:
        if job.kind == JobKind.SERIES:
            from series_recap.__main__ import (
                build_paths,
                composer_outputs_valid,
                render_outputs_valid,
                series_match_outputs_valid,
                tts_outputs_valid,
                youtube_chapters_outputs_valid,
            )

            paths = build_paths(Path(job.run_dir))
            return all(
                (
                    composer_outputs_valid(paths),
                    tts_outputs_valid(paths),
                    youtube_chapters_outputs_valid(paths),
                    series_match_outputs_valid(paths),
                    render_outputs_valid(paths),
                )
            )

        from orchestrator.config import load_config
        from orchestrator.graph import build_paths
        from orchestrator.runner import outputs_valid

        paths = build_paths(Path(job.run_dir))
        config = load_config(Path(job.config_path))
        return outputs_valid(paths, "render", config=config)
    except (OSError, ValueError, KeyError):
        return False


def _validated_stages(job: JobRecord) -> list[tuple[str, str, bool]]:
    try:
        if job.kind == JobKind.SERIES:
            from orchestrator.config import load_config
            from series_recap.__main__ import (
                build_paths,
                composer_outputs_valid,
                episode_config_for,
                episode_stage_valid,
                manifest_episode_specs,
                render_outputs_valid,
                select_episodes,
                series_match_outputs_valid,
                tts_outputs_valid,
                youtube_chapters_outputs_valid,
            )

            manifest_arg = _argument_value(job.argv, "--manifest")
            if manifest_arg is None:
                return []
            episodes_arg = _argument_value(job.argv, "--episodes")
            _, all_specs = manifest_episode_specs(Path(manifest_arg))
            specs = select_episodes(all_specs, episodes_arg)
            config = load_config(Path(job.config_path))
            paths = build_paths(Path(job.run_dir))
            results: list[tuple[str, str, bool]] = []
            for spec in specs:
                episode_dir = Path(job.run_dir) / spec.episode_key
                generated_config = paths.config_dir / f"{spec.episode_key}.json"
                episode_config = (
                    load_config(generated_config)
                    if generated_config.is_file()
                    else episode_config_for(
                        base_config=config,
                        manifest_path=Path(manifest_arg),
                        spec=spec,
                        series_memory_dir=Path(job.run_dir) / "series_memory",
                    )
                )
                planner_valid = episode_stage_valid("episode_planner", episode_dir, spec.source_path, episode_config)
                shots_valid = episode_stage_valid("episode_shots", episode_dir, spec.source_path, episode_config)
                results.extend(
                    [
                        ("episode_planner", spec.episode_key, planner_valid),
                        ("episode_shots", spec.episode_key, shots_valid),
                    ]
                )
            results.extend(
                [
                    ("series_composer", "", composer_outputs_valid(paths)),
                    ("tts", "", tts_outputs_valid(paths)),
                    ("youtube_chapters", "", youtube_chapters_outputs_valid(paths)),
                    ("series_match", "", series_match_outputs_valid(paths)),
                    ("render", "", render_outputs_valid(paths)),
                ]
            )
            return results

        from orchestrator.config import load_config
        from orchestrator.graph import STAGES, build_paths, stage_range
        from orchestrator.runner import episode_planner_enabled, outputs_valid

        film_arg = _argument_value(job.argv, "--input")
        film = Path(film_arg) if film_arg else None
        config = load_config(Path(job.config_path))
        paths = build_paths(Path(job.run_dir))
        selected = stage_range(
            _argument_value(job.argv, "--from"),
            _argument_value(job.argv, "--to"),
            _argument_value(job.argv, "--only"),
        )
        if not config.get("preflight", {}).get("enabled", True):
            selected.discard("preflight")
        if not config.get("visual_index", {}).get("enabled", False):
            selected.discard("visual_index")
        if not episode_planner_enabled(config) and _argument_value(job.argv, "--only") != "episode_planner":
            selected.discard("episode_planner")
        return [
            (stage, "", outputs_valid(paths, stage, film=film, config=config))
            for stage in STAGES
            if stage in selected
        ]
    except (OSError, ValueError, KeyError):
        return []


def _argument_value(command: list[str], name: str) -> str | None:
    try:
        index = command.index(name)
    except ValueError:
        return None
    return command[index + 1] if index + 1 < len(command) else None


def _marker_owner_alive(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        process = psutil.Process(int(payload["pid"]))
        active = abs(process.create_time() - float(payload["create_time"])) <= 0.01
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, psutil.Error):
        active = False
    if not active:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return active


def _processes_using_profile(profile_dir: Path) -> list[int]:
    needle = str(profile_dir.resolve()).casefold()
    pids: list[int] = []
    for process in psutil.process_iter(("pid", "cmdline")):
        try:
            command = " ".join(process.info.get("cmdline") or []).casefold()
        except (psutil.Error, OSError):
            continue
        if needle in command:
            pids.append(int(process.info["pid"]))
    return pids
