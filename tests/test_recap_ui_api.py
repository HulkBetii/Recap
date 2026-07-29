from __future__ import annotations

import json
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

import recap_ui.app as app_module
from recap_ui.__main__ import _acquire_server_lock, _release_server_lock
from recap_ui.app import create_app


FORBIDDEN_BROWSER_KEYS = {
    "argv",
    "command",
    "command_hash",
    "config_path",
    "config_snapshot",
    "dry_run_command",
    "dry_run_output",
    "output_paths",
    "parent_job_id",
    "pid",
    "plan_id_internal",
    "process_create_time",
    "run_dir",
    "validator_result",
    "worker_id",
}


def assert_opaque_payload(payload: object, *, secret_paths: list[Path]) -> None:
    if isinstance(payload, dict):
        assert FORBIDDEN_BROWSER_KEYS.isdisjoint(payload)
        for value in payload.values():
            assert_opaque_payload(value, secret_paths=secret_paths)
    elif isinstance(payload, list):
        for value in payload:
            assert_opaque_payload(value, secret_paths=secret_paths)
    elif isinstance(payload, str):
        for path in secret_paths:
            assert str(path) not in payload


def make_client(tmp_path: Path) -> tuple[TestClient, str, Path]:
    repo_root = tmp_path / "repo"
    (repo_root / "runs").mkdir(parents=True)
    (repo_root / "config.movie.stable.yaml").write_text("tts:\n  voice_id: test\n", encoding="utf-8")
    app = create_app(repo_root=repo_root, state_dir=tmp_path / "state", token="test-startup-token")
    return TestClient(app), "test-startup-token", repo_root


def test_mutations_require_token_and_same_origin(tmp_path: Path) -> None:
    client, token, _ = make_client(tmp_path)
    roots = client.get("/api/fs/roots").json()
    runs_token = next(item["token"] for item in roots if item["id"] == "runs")

    assert client.post("/api/fs/child-token", json={"parent_token": runs_token, "name": "demo"}).status_code == 403
    response = client.post(
        "/api/fs/child-token",
        json={"parent_token": runs_token, "name": "demo"},
        headers={"X-Recap-Token": token, "Origin": "http://evil.example"},
    )
    assert response.status_code == 403

    response = client.post(
        "/api/fs/child-token",
        json={"parent_token": runs_token, "name": "demo"},
        headers={"X-Recap-Token": token},
    )
    assert response.status_code == 200
    assert response.json()["token"]
    assert client.get("/api/session", headers={"Host": "evil.example"}).status_code == 403


def test_session_and_health_do_not_expose_secret_values(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, token, _ = make_client(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-return-this-value")

    assert client.get("/api/session").json() == {"token": token}
    health_text = client.get("/api/health").text
    assert "sk-do-not-return-this-value" not in health_text
    assert json.loads(health_text)["providers"]["openai"] is True


def test_plan_job_and_health_responses_use_opaque_browser_dtos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from recap_ui.process_control import command_hash
    from recap_ui.schemas import (
        DeliveryStatus,
        EventType,
        ExecutionPlan,
        JobKind,
        JobStageRecord,
        JobStatus,
        PlanCheck,
        PlanDagNode,
        StageStatus,
    )

    client, token, repo_root = make_client(tmp_path)
    repository = client.app.state.repository
    run_dir = repo_root / "runs" / "opaque-demo"
    config_path = tmp_path / "private" / "config.snapshot.json"
    source_path = tmp_path / "private" / "episode 01.mp4"
    command = [
        str(tmp_path / "python.exe"),
        str(repo_root / "run.py"),
        "--input",
        str(source_path),
        "--run-dir",
        str(run_dir),
        "--config",
        str(config_path),
    ]
    plan = ExecutionPlan(
        plan_id="c" * 24,
        kind=JobKind.SINGLE,
        command=command,
        dry_run_command=[*command, "--dry-run"],
        run_dir=str(run_dir),
        config_path=str(config_path),
        config_snapshot={"private_path": str(config_path)},
        dag=[PlanDagNode(key="render", label="Render", outputs=[str(run_dir / "recap.mp4")])],
        output_paths=[str(run_dir / "recap.mp4")],
        checks=[
            PlanCheck(
                code="source",
                status=DeliveryStatus.PASS,
                message=f"Source is available at {source_path}",
                details={"missing": [str(source_path)]},
            )
        ],
        warnings=[f"Dry-run warning for {config_path}"],
        dry_run_output=f"stderr from {repo_root}",
        command_hash=command_hash(command),
        display_title="Opaque Demo",
    )
    monkeypatch.setattr(client.app.state.planning, "plan_single", lambda _request: plan)
    headers = {"X-Recap-Token": token}
    secret_paths = [repo_root, run_dir, config_path, source_path, tmp_path / "python.exe"]

    plan_response = client.post(
        "/api/plans/single",
        headers=headers,
        json={"source_token": "source", "run_dir_token": "run", "config_token": "config"},
    )
    assert plan_response.status_code == 200
    public_plan = plan_response.json()
    assert public_plan["run_name"] == "opaque-demo"
    assert public_plan["command_preview"] == [
        "python",
        "run.py",
        "--input",
        "<source>",
        "--run-dir",
        "<run-dir>",
        "--config",
        "<config>",
    ]
    assert public_plan["output_names"] == ["recap.mp4"]
    assert public_plan["dry_run_summary"] == "[planned] Render"
    assert_opaque_payload(public_plan, secret_paths=secret_paths)

    client.app.state.planning._persist_plan(plan.model_copy(update={"dag": []}))
    created_response = client.post("/api/jobs", headers=headers, json={"plan_id": plan.plan_id})
    assert created_response.status_code == 200
    created = created_response.json()
    job_id = created["id"]
    assert created["run_name"] == "opaque-demo"
    assert_opaque_payload(created, secret_paths=secret_paths)

    repository.upsert_stage(
        JobStageRecord(
            job_id=job_id,
            stage_key="render",
            status=StageStatus.FAILED,
            exit_code=9,
            validator_result={"path": str(run_dir / "recap.mp4")},
            error=f"failed at {run_dir}",
        )
    )
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert set(detail["stages"][0]).isdisjoint({"job_id", "exit_code", "validator_result"})
    assert_opaque_payload(detail, secret_paths=secret_paths)
    assert_opaque_payload(client.get("/api/jobs").json(), secret_paths=secret_paths)
    repository.append_event(
        job_id,
        EventType.HEARTBEAT,
        {
            "pid": 123,
            "worker_id": "worker-private",
            "source": str(config_path),
            "nested": {"run_dir": str(run_dir)},
            "lines": [f"processing {source_path}"],
        },
    )
    public_event = client.get(f"/api/jobs/{job_id}/events").json()[-1]
    assert "job_id" not in public_event
    assert set(public_event["payload"]).isdisjoint({"pid", "worker_id"})
    assert public_event["payload"]["nested"] == {}
    assert_opaque_payload(public_event, secret_paths=secret_paths)

    repository.update_job(job_id, status=JobStatus.STARTING)
    health = client.get("/api/health").json()
    assert health["active_job_id"] == job_id
    assert "active_job" not in health
    assert all("path" not in check.get("details", {}) for check in health["runtime"])
    assert_opaque_payload(health, secret_paths=secret_paths)

    repository.update_job(job_id, status=JobStatus.FAILED, error_message=f"failure under {run_dir}")
    resumed_response = client.post(f"/api/jobs/{job_id}/resume", headers=headers)
    assert resumed_response.status_code == 200
    resumed = resumed_response.json()
    assert_opaque_payload(resumed, secret_paths=secret_paths)
    resumed_internal = repository.get_job(resumed["id"])
    assert resumed_internal is not None
    assert resumed_internal.run_dir == str(run_dir)
    assert resumed_internal.config_snapshot == plan.config_snapshot
    assert resumed_internal.argv[resumed_internal.argv.index("--run-dir") + 1] == str(run_dir)
    assert resumed_internal.argv[resumed_internal.argv.index("--config") + 1] == resumed_internal.config_path

    repository.update_job(resumed["id"], status=JobStatus.FAILED)
    rerun_response = client.post(
        f"/api/jobs/{job_id}/rerun",
        headers=headers,
        json={"scope": "stage", "stage": "render"},
    )
    assert rerun_response.status_code == 200
    rerun = rerun_response.json()
    assert_opaque_payload(rerun, secret_paths=secret_paths)
    rerun_internal = repository.get_job(rerun["id"])
    assert rerun_internal is not None
    assert rerun_internal.run_dir == str(run_dir)
    assert rerun_internal.config_snapshot == plan.config_snapshot
    assert rerun_internal.argv[rerun_internal.argv.index("--config") + 1] == rerun_internal.config_path
    assert rerun_internal.argv[-2:] == ["--force-stage", "render"]


def test_spa_injects_startup_token_and_security_policy(tmp_path: Path) -> None:
    client, token, _ = make_client(tmp_path)
    response = client.get("/runs")
    assert response.status_code == 200
    assert f'<meta name="recap-token" content="{token}">' in response.text
    assert "Content-Security-Policy" in response.headers


def test_media_supports_ranges_and_opaque_artifact_ids(tmp_path: Path) -> None:
    client, _, repo_root = make_client(tmp_path)
    run_dir = repo_root / "runs" / "demo"
    run_dir.mkdir()
    payload = b"0123456789"
    (run_dir / "recap.mp4").write_bytes(payload)
    (run_dir / "summary.json").write_text("{}", encoding="utf-8")

    runs = client.get("/api/runs").json()
    assert len(runs) == 1
    run_id = runs[0]["id"]
    artifacts = client.get(f"/api/runs/{run_id}/artifacts").json()
    video = next(item for item in artifacts if item["name"] == "recap.mp4")

    response = client.get(
        f"/api/runs/{run_id}/media/{video['id']}",
        headers={"Range": "bytes=2-5"},
    )
    assert response.status_code == 206
    assert response.content == b"2345"
    assert client.get(
        f"/api/runs/{run_id}/media/{video['id']}",
        headers={"Range": "bytes=99-100"},
    ).status_code == 416
    summary = next(item for item in artifacts if item["name"] == "summary.json")
    assert client.get(f"/api/runs/{run_id}/media/{summary['id']}").status_code == 415
    assert client.get(f"/api/runs/{run_id}/media/../../AGENTS.md").status_code in {404, 422}


def test_job_events_can_be_replayed_after_sequence(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    repository = client.app.state.repository
    from recap_ui.process_control import command_hash
    from recap_ui.schemas import EventType, ExecutionPlan, JobKind

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    command = ["python", "-c", "pass"]
    plan = ExecutionPlan(
        plan_id="a" * 24,
        kind=JobKind.SINGLE,
        command=command,
        dry_run_command=[*command, "--dry-run"],
        run_dir=str(tmp_path / "run"),
        config_path=str(config_path),
        config_snapshot={},
        dag=[],
        command_hash=command_hash(command),
    )
    job = repository.create_job(plan)
    first = repository.append_event(job.id, EventType.LOG, {"lines": ["first"]})
    second = repository.append_event(job.id, EventType.LOG, {"lines": ["second"]})

    response = client.get(f"/api/jobs/{job.id}/events", params={"after_seq": first.seq})
    assert response.status_code == 200
    assert [item["seq"] for item in response.json()] == [second.seq]


def test_active_profile_smoke_never_sends(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, token, _ = make_client(tmp_path)

    async def fake_smoke(_profile_dir: Path) -> str:
        return "https://chatgpt.com/c/test"

    monkeypatch.setattr(app_module, "_test_profile_no_send", fake_smoke)
    response = client.post(
        "/api/health/chatgpt-profile",
        headers={"X-Recap-Token": token},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "pass",
        "composer_available": True,
        "url": "https://chatgpt.com/c/test",
        "sent": False,
    }


def test_profile_smoke_error_sanitizes_runtime_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, token, _ = make_client(tmp_path)
    profile_path = app_module.PROFILE_DIR

    async def failing_smoke(_profile_dir: Path) -> str:
        raise RuntimeError(f"profile failed at {profile_path}")

    monkeypatch.setattr(app_module, "_test_profile_no_send", failing_smoke)
    response = client.post(
        "/api/health/chatgpt-profile",
        headers={"X-Recap-Token": token},
    )

    assert response.status_code == 503
    assert str(profile_path) not in response.text


def test_server_lock_rejects_duplicate_process(tmp_path: Path) -> None:
    path = tmp_path / "server.lock"
    token = _acquire_server_lock(path, psutil)
    try:
        with pytest.raises(SystemExit, match="already running"):
            _acquire_server_lock(path, psutil)
    finally:
        _release_server_lock(path, token)
    replacement = _acquire_server_lock(path, psutil)
    _release_server_lock(path, replacement)


def test_enqueue_rejects_duplicate_nonterminal_run_dir(tmp_path: Path) -> None:
    client, token, _ = make_client(tmp_path)
    from recap_ui.process_control import command_hash
    from recap_ui.schemas import ExecutionPlan, JobKind

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    command = ["python", "-c", "pass"]
    plan = ExecutionPlan(
        plan_id="b" * 24,
        kind=JobKind.SINGLE,
        command=command,
        dry_run_command=[*command, "--dry-run"],
        run_dir=str(tmp_path / "same-run"),
        config_path=str(config_path),
        config_snapshot={},
        dag=[],
        command_hash=command_hash(command),
    )
    client.app.state.planning._persist_plan(plan)
    headers = {"X-Recap-Token": token}

    assert client.post("/api/jobs", json={"plan_id": plan.plan_id}, headers=headers).status_code == 200
    response = client.post("/api/jobs", json={"plan_id": plan.plan_id}, headers=headers)
    assert response.status_code == 409
    assert "nonterminal job" in response.json()["detail"]
