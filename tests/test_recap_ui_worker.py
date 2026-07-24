from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil
import recap_ui.worker as worker_module
from recap_ui.database import Database
from recap_ui.planning import PlanningService
from recap_ui.process_control import command_hash, identity_for_process, popen_command, process_matches, terminate_process_tree
from recap_ui.repository import Repository
from recap_ui.schemas import ExecutionPlan, JobKind, JobStatus
from recap_ui.worker import JobWorker


def make_plan(tmp_path: Path, command: list[str]) -> ExecutionPlan:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    return ExecutionPlan(
        plan_id=command_hash(command)[:24],
        kind=JobKind.SINGLE,
        command=command,
        dry_run_command=[*command, "--dry-run"],
        run_dir=str(tmp_path / "run"),
        config_path=str(config_path),
        config_snapshot={},
        dag=[],
        command_hash=command_hash(command),
    )


def test_command_hash_matches_planning_service() -> None:
    command = [sys.executable, "run.py", "--input", "film.mp4"]
    assert command_hash(command) == PlanningService._command_hash(command)


def test_process_identity_prevents_pid_only_cancellation(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    process = popen_command(command, cwd=tmp_path)
    identity = identity_for_process(process, command)
    assert process_matches(identity)
    assert terminate_process_tree(identity, grace_s=0.1)
    process.wait(timeout=5)
    assert not process_matches(identity)


def test_process_tree_cancellation_does_not_leave_child(tmp_path: Path) -> None:
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "print(child.pid, flush=True); time.sleep(60)"
    )
    command = [sys.executable, "-c", parent_code]
    process = popen_command(command, cwd=tmp_path)
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    identity = identity_for_process(process, command)

    assert terminate_process_tree(identity, grace_s=0.2)
    process.wait(timeout=5)
    deadline = time.monotonic() + 5.0
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid)


def test_worker_runs_job_and_redacts_secret(tmp_path: Path) -> None:
    repository = Repository(Database(tmp_path / "ui.db"))
    secret = "sk-this-secret-must-not-leak"
    command = [sys.executable, "-c", f"print('{secret}')"]
    job = repository.create_job(make_plan(tmp_path, command))
    claimed = repository.claim_next_job("test-worker")
    assert claimed is not None

    JobWorker(repository=repository, repo_root=tmp_path, state_dir=tmp_path / "state").run_job(claimed)

    completed = repository.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED
    events = repository.list_events(job.id)
    event_text = "\n".join(str(event.model_dump(mode="json")) for event in events)
    assert secret not in event_text
    assert "[REDACTED]" in event_text


def test_repository_claims_jobs_in_fifo_order(tmp_path: Path) -> None:
    repository = Repository(Database(tmp_path / "ui.db"))
    first = repository.create_job(make_plan(tmp_path / "first", [sys.executable, "-c", "pass"]))
    second = repository.create_job(make_plan(tmp_path / "second", [sys.executable, "-c", "pass"]))

    claimed = repository.claim_next_job("test-worker")
    assert claimed is not None
    assert claimed.id == first.id
    assert repository.claim_next_job("other-worker") is None

    repository.update_job(claimed.id, status=JobStatus.SUCCEEDED)
    next_claimed = repository.claim_next_job("test-worker")
    assert next_claimed is not None
    assert next_claimed.id == second.id


def test_validator_poll_persists_authoritative_stage_state(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repository = Repository(Database(tmp_path / "ui.db"))
    job = repository.create_job(make_plan(tmp_path, [sys.executable, "-c", "pass"]))
    monkeypatch.setattr(worker_module, "_validated_stages", lambda _job: [("render", "", True)])

    JobWorker(repository=repository, repo_root=tmp_path, state_dir=tmp_path / "state")._poll_stage_validation(job)

    stages = repository.list_stages(job.id)
    assert len(stages) == 1
    assert stages[0].stage_key == "render"
    assert stages[0].status.value == "succeeded"
    assert stages[0].validator_result == {"valid": True, "source": "pipeline_validator"}


def test_worker_blocks_when_external_process_uses_profile(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repository = Repository(Database(tmp_path / "ui.db"))
    marker = tmp_path / "should-not-exist.txt"
    command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    job = repository.create_job(make_plan(tmp_path, command))
    claimed = repository.claim_next_job("test-worker")
    assert claimed is not None
    monkeypatch.setattr(worker_module, "_processes_using_profile", lambda _profile: [12345])

    JobWorker(repository=repository, repo_root=tmp_path, state_dir=tmp_path / "state").run_job(claimed)

    completed = repository.get_job(job.id)
    assert completed is not None
    assert completed.status == JobStatus.BLOCKED
    assert completed.error_code == "profile_in_use"
    assert not marker.exists()


def test_validator_poll_tracks_running_and_pending_stages(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repository = Repository(Database(tmp_path / "ui.db"))
    queued = repository.create_job(make_plan(tmp_path, [sys.executable, "-c", "pass"]))
    job = repository.claim_next_job("test-worker")
    assert job is not None and job.id == queued.id
    repository.update_job(job.id, status=JobStatus.RUNNING)
    monkeypatch.setattr(
        worker_module,
        "_validated_stages",
        lambda _job: [("ingest", "", True), ("review", "", False), ("tts", "", False)],
    )
    worker = JobWorker(repository=repository, repo_root=tmp_path, state_dir=tmp_path / "state")

    worker._poll_stage_validation(job)

    statuses = {stage.stage_key: stage.status.value for stage in repository.list_stages(job.id)}
    assert statuses == {"ingest": "succeeded", "review": "running", "tts": "pending"}
    worker._finish(job.id, JobStatus.FAILED, exit_code=2, error_message="failed")
    statuses = {stage.stage_key: stage.status.value for stage in repository.list_stages(job.id)}
    assert statuses["review"] == "failed"
    assert any(event.event_type.value == "qa" for event in repository.list_events(job.id))
