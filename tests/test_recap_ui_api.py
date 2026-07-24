from __future__ import annotations

import json
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

import recap_ui.app as app_module
from recap_ui.__main__ import _acquire_server_lock, _release_server_lock
from recap_ui.app import create_app


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
