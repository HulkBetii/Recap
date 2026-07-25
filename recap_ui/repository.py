from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from recap_ui.database import Database
from recap_ui.schemas import (
    EventType,
    ExecutionPlan,
    JobEvent,
    JobKind,
    JobRecord,
    JobStageRecord,
    JobStatus,
    RegisteredRunRecord,
    ResourceLock,
    StageStatus,
    utc_now,
)


ACTIVE_STATUSES = (
    JobStatus.STARTING.value,
    JobStatus.RUNNING.value,
    JobStatus.CANCEL_REQUESTED.value,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _replace_option(command: list[str], option: str, value: str) -> list[str]:
    updated = list(command)
    if option in updated:
        index = updated.index(option)
        if index + 1 >= len(updated):
            raise ValueError(f"command option is missing a value: {option}")
        updated[index + 1] = value
    return updated


def _command_hash(command: list[str]) -> str:
    payload = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def create_job(
        self,
        plan: ExecutionPlan,
        config_path: Path | str | None = None,
        parent_job_id: str | None = None,
    ) -> JobRecord:
        job_id = uuid.uuid4().hex
        created_at = utc_now()
        attempt = 1
        if parent_job_id is not None:
            parent = self.get_job(parent_job_id)
            if parent is None:
                raise KeyError(f"parent job does not exist: {parent_job_id}")
            attempt = parent.attempt + 1
        snapshot_path = self.database.path.parent / "configs" / f"{job_id}.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(plan.config_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        resolved_config = str(snapshot_path.resolve())
        argv = _replace_option(plan.command, "--config", resolved_config)
        command_hash = _command_hash(argv)
        display_title = plan.display_title or Path(plan.run_dir).name or f"Run {job_id[:8]}"
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs (
                    id, plan_id, kind, status, argv_json, command_hash, run_dir,
                    config_path, config_snapshot_json, display_title, parent_job_id, attempt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    plan.plan_id,
                    plan.kind.value,
                    JobStatus.QUEUED.value,
                    _json(argv),
                    command_hash,
                    str(Path(plan.run_dir).expanduser().resolve()),
                    resolved_config,
                    _json(plan.config_snapshot),
                    display_title,
                    parent_job_id,
                    attempt,
                    created_at.isoformat(),
                ),
            )
            connection.commit()
        job = self.get_job(job_id)
        assert job is not None
        self.append_event(
            job_id,
            EventType.JOB_STATE,
            {"status": JobStatus.QUEUED.value},
            message="Job queued",
        )
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_jobs(self, *, limit: int = 100, statuses: Iterable[JobStatus | str] | None = None) -> list[JobRecord]:
        limit = max(1, min(limit, 1000))
        params: list[object] = []
        where = ""
        if statuses is not None:
            values = [status.value if isinstance(status, JobStatus) else str(status) for status in statuses]
            if not values:
                return []
            where = "WHERE status IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
        params.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def active_job(self) -> JobRecord | None:
        jobs = self.list_jobs(limit=1, statuses=ACTIVE_STATUSES)
        return jobs[0] if jobs else None

    def claim_next_job(self, worker_id: str) -> JobRecord | None:
        now = utc_now().isoformat()
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?, ?) LIMIT 1",
                ACTIVE_STATUSES,
            ).fetchone()
            if active is not None:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at, id LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            result = connection.execute(
                """
                UPDATE jobs SET status = ?, worker_id = ?, started_at = ?, heartbeat_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.STARTING.value,
                    worker_id,
                    now,
                    now,
                    row["id"],
                    JobStatus.QUEUED.value,
                ),
            )
            if result.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
        job = self.get_job(str(row["id"]))
        assert job is not None
        self.append_event(
            job.id,
            EventType.JOB_STATE,
            {"status": JobStatus.STARTING.value, "worker_id": worker_id},
            message="Worker claimed job",
        )
        return job

    def update_job(self, job_id: str, **fields: Any) -> JobRecord:
        allowed = {
            "status",
            "worker_id",
            "pid",
            "process_create_time",
            "heartbeat_at",
            "cancel_requested",
            "exit_code",
            "error_code",
            "error_message",
            "started_at",
            "finished_at",
            "argv",
            "config_path",
            "display_title",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported job field(s): {', '.join(sorted(unknown))}")
        if not fields:
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            return job
        columns: list[str] = []
        values: list[object] = []
        for key, value in fields.items():
            column = "argv_json" if key == "argv" else key
            if isinstance(value, (JobStatus,)):
                value = value.value
            elif isinstance(value, datetime):
                value = value.isoformat()
            elif key == "argv":
                value = _json(value)
            elif key == "cancel_requested":
                value = int(bool(value))
            columns.append(f"{column} = ?")
            values.append(value)
        values.append(job_id)
        with self.database.connection() as connection:
            result = connection.execute(f"UPDATE jobs SET {', '.join(columns)} WHERE id = ?", values)
            if result.rowcount != 1:
                raise KeyError(job_id)
            connection.commit()
        job = self.get_job(job_id)
        assert job is not None
        return job

    def request_cancel(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status == JobStatus.QUEUED:
            updated = self.update_job(
                job_id,
                status=JobStatus.CANCELLED,
                cancel_requested=True,
                finished_at=utc_now(),
            )
        elif job.status in {JobStatus.STARTING, JobStatus.RUNNING}:
            updated = self.update_job(
                job_id,
                status=JobStatus.CANCEL_REQUESTED,
                cancel_requested=True,
            )
        else:
            return job
        self.append_event(
            job_id,
            EventType.JOB_STATE,
            {"status": updated.status.value},
            message="Cancellation requested",
        )
        return updated

    def heartbeat(self, job_id: str) -> JobRecord:
        return self.update_job(job_id, heartbeat_at=utc_now())

    def append_event(
        self,
        job_id: str,
        event_type: EventType | str,
        payload: dict[str, Any] | None = None,
        *,
        level: str = "INFO",
        stage: str | None = None,
        message: str = "",
        timestamp: datetime | None = None,
    ) -> JobEvent:
        now = timestamp or utc_now()
        event_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
        with self.database.connection() as connection:
            result = connection.execute(
                """
                INSERT INTO job_events (job_id, timestamp, event_type, level, stage, message, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, now.isoformat(), event_value, level, stage, message, _json(payload or {})),
            )
            seq = int(result.lastrowid)
            connection.commit()
        return JobEvent(
            seq=seq,
            job_id=job_id,
            timestamp=now,
            event_type=EventType(event_value),
            level=level,
            stage=stage,
            message=message,
            payload=payload or {},
        )

    def append_events(self, events: Iterable[dict[str, Any]]) -> list[JobEvent]:
        created: list[JobEvent] = []
        for event in events:
            created.append(self.append_event(**event))
        return created

    def list_events(self, job_id: str, *, after_seq: int = 0, limit: int = 1000) -> list[JobEvent]:
        limit = max(1, min(limit, 5000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (job_id, max(0, after_seq), limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def upsert_stage(self, stage: JobStageRecord) -> JobStageRecord:
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO job_stages (
                    job_id, stage_key, episode_key, attempt, status, started_at,
                    finished_at, exit_code, validator_result_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, stage_key, episode_key, attempt) DO UPDATE SET
                    status=excluded.status,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    exit_code=excluded.exit_code,
                    validator_result_json=excluded.validator_result_json,
                    error=excluded.error
                """,
                (
                    stage.job_id,
                    stage.stage_key,
                    stage.episode_key,
                    stage.attempt,
                    stage.status.value,
                    _timestamp(stage.started_at),
                    _timestamp(stage.finished_at),
                    stage.exit_code,
                    _json(stage.validator_result),
                    stage.error,
                ),
            )
            connection.commit()
        return stage

    def list_stages(self, job_id: str) -> list[JobStageRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM job_stages WHERE job_id = ? ORDER BY episode_key, stage_key, attempt",
                (job_id,),
            ).fetchall()
        return [
            JobStageRecord(
                job_id=row["job_id"],
                stage_key=row["stage_key"],
                episode_key=row["episode_key"],
                attempt=row["attempt"],
                status=StageStatus(row["status"]),
                started_at=_parse_datetime(row["started_at"]),
                finished_at=_parse_datetime(row["finished_at"]),
                exit_code=row["exit_code"],
                validator_result=json.loads(row["validator_result_json"]),
                error=row["error"],
            )
            for row in rows
        ]

    def acquire_lock(self, resource: str, job_id: str) -> bool:
        try:
            with self.database.connection() as connection:
                connection.execute(
                    "INSERT INTO resource_locks (resource, job_id, acquired_at) VALUES (?, ?, ?)",
                    (resource, job_id, utc_now().isoformat()),
                )
                connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_lock(self, resource: str) -> ResourceLock | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM resource_locks WHERE resource = ?", (resource,)).fetchone()
        if row is None:
            return None
        return ResourceLock(
            resource=row["resource"],
            job_id=row["job_id"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
        )

    def release_locks(self, job_id: str) -> int:
        with self.database.connection() as connection:
            result = connection.execute("DELETE FROM resource_locks WHERE job_id = ?", (job_id,))
            connection.commit()
        return int(result.rowcount)

    def register_run(
        self,
        path: Path | str,
        kind: JobKind | str | None = None,
        display_title: str | None = None,
    ) -> RegisteredRunRecord:
        resolved = str(Path(path).expanduser().resolve())
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"recap-run:{resolved.casefold()}").hex
        kind_value = kind.value if isinstance(kind, JobKind) else kind
        created_at = utc_now()
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO registered_runs (id, path, kind, display_title, created_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    kind=COALESCE(excluded.kind, registered_runs.kind),
                    display_title=COALESCE(excluded.display_title, registered_runs.display_title)
                """,
                (run_id, resolved, kind_value, display_title, created_at.isoformat()),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM registered_runs WHERE path = ?", (resolved,)).fetchone()
        assert row is not None
        return RegisteredRunRecord(
            id=row["id"],
            path=row["path"],
            kind=JobKind(row["kind"]) if row["kind"] else None,
            display_title=row["display_title"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_registered_runs(self) -> list[RegisteredRunRecord]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM registered_runs ORDER BY created_at DESC").fetchall()
        return [
            RegisteredRunRecord(
                id=row["id"],
                path=row["path"],
                kind=JobKind(row["kind"]) if row["kind"] else None,
                display_title=row["display_title"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            plan_id=row["plan_id"],
            kind=JobKind(row["kind"]),
            status=JobStatus(row["status"]),
            argv=json.loads(row["argv_json"]),
            command_hash=row["command_hash"],
            run_dir=row["run_dir"],
            config_path=row["config_path"],
            config_snapshot=json.loads(row["config_snapshot_json"]),
            display_title=row["display_title"] or Path(row["run_dir"]).name or f"Run {str(row['id'])[:8]}",
            parent_job_id=row["parent_job_id"],
            attempt=row["attempt"],
            worker_id=row["worker_id"],
            pid=row["pid"],
            process_create_time=row["process_create_time"],
            heartbeat_at=_parse_datetime(row["heartbeat_at"]),
            cancel_requested=bool(row["cancel_requested"]),
            exit_code=row["exit_code"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=_parse_datetime(row["started_at"]),
            finished_at=_parse_datetime(row["finished_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            seq=row["seq"],
            job_id=row["job_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            event_type=EventType(row["event_type"]),
            level=row["level"],
            stage=row["stage"],
            message=row["message"],
            payload=json.loads(row["payload_json"]),
        )
