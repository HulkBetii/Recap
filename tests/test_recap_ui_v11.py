from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recap_ui.app import create_app
from recap_ui.database import Database
from recap_ui.planning import PlanningService
from recap_ui.repository import Repository
from recap_ui.runs import RunService
from recap_ui.schemas import ExecutionPlan, JobKind, JobStatus, SeriesPlanRequest, SinglePlanRequest
from recap_ui.security import PathRegistry


def _plan(tmp_path: Path, *, run_dir: Path, display_title: str | None = None) -> ExecutionPlan:
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    command = ["python", "run.py", "--run-dir", str(run_dir), "--config", str(config)]
    return ExecutionPlan(
        plan_id="a" * 24,
        kind=JobKind.SINGLE,
        command=command,
        dry_run_command=[*command, "--dry-run"],
        run_dir=str(run_dir),
        config_path=str(config),
        config_snapshot={},
        dag=[],
        command_hash=PlanningService._command_hash(command),
        display_title=display_title,
    )


def _client(tmp_path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> tuple[TestClient, str, Path]:
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    (repo / "config.movie.stable.yaml").write_text(
        "ingest:\n  source_language: ko\n  translate_mode: ko-en\n"
        "review:\n  llm_backend: chatgpt_playwright\n"
        "tts:\n  provider_mode: auto\n  voice_id: configured\n"
        "render:\n  width: 1920\n  height: 1080\n  fps: 30\n",
        encoding="utf-8",
    )
    token = "v11-token"
    app = create_app(repo_root=repo, state_dir=tmp_path / "state", host=host, port=port, token=token)
    return TestClient(app), token, repo


def test_database_migrates_v1_titles_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "ui.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                argv_json TEXT NOT NULL, command_hash TEXT NOT NULL, run_dir TEXT NOT NULL,
                config_path TEXT NOT NULL, config_snapshot_json TEXT NOT NULL,
                parent_job_id TEXT, attempt INTEGER NOT NULL DEFAULT 1, worker_id TEXT, pid INTEGER,
                process_create_time REAL, heartbeat_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                exit_code INTEGER, error_code TEXT, error_message TEXT, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT
            );
            CREATE TABLE registered_runs (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, kind TEXT, created_at TEXT NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        run_dir = tmp_path / "legacy-run"
        connection.execute(
            """
            INSERT INTO jobs (
                id, plan_id, kind, status, argv_json, command_hash, run_dir, config_path,
                config_snapshot_json, attempt, cancel_requested, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "legacy-plan",
                "single",
                "succeeded",
                "[]",
                "hash",
                str(run_dir),
                str(tmp_path / "config.json"),
                "{}",
                1,
                0,
                "2026-07-24T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO registered_runs (id, path, kind, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-run", str(run_dir), "single", "2026-07-24T00:00:00+00:00"),
        )

    database = Database(path)
    database.initialize()
    repository = Repository(database)

    assert repository.get_job("legacy-job").display_title == "legacy-run"  # type: ignore[union-attr]
    assert repository.list_registered_runs()[0].display_title is None
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_titles_persist_and_run_management_is_explicit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run = repo / "runs" / "demo"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"title": "Artifact Summary"}), encoding="utf-8")
    database = Database(tmp_path / "ui.db")
    repository = Repository(database)
    registry = PathRegistry({"repo": ("Repo", repo)}, b"r" * 32)
    service = RunService(repo, repository, registry)

    repository.register_run(run, display_title="Registered Title")
    artifact_only = service.discover_runs()[0]
    assert artifact_only.display_title == "Registered Title"
    assert artifact_only.management_mode == "artifact_only"
    assert artifact_only.available_actions == ["view"]
    assert artifact_only.read_only_reason

    job = repository.create_job(_plan(tmp_path, run_dir=run, display_title="Managed Title"))
    managed = service.discover_runs()[0]
    assert managed.display_title == "Managed Title"
    assert managed.management_mode == "managed"
    assert managed.available_actions == ["view", "cancel"]

    repository.update_job(job.id, status=JobStatus.SUCCEEDED)
    assert service.discover_runs()[0].available_actions == ["view", "rerun"]
    repository.update_job(job.id, status=JobStatus.FAILED)
    assert service.discover_runs()[0].available_actions == ["view", "resume"]

    restarted = Repository(Database(tmp_path / "ui.db"))
    assert restarted.get_job(job.id).display_title == "Managed Title"  # type: ignore[union-attr]
    assert restarted.list_registered_runs()[0].display_title == "Registered Title"


def test_plans_preserve_normalized_display_titles(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    runs = repo / "runs"
    runs.mkdir(parents=True)
    source = repo / "episode-one.mp4"
    source.write_bytes(b"video")
    config = repo / "config.yaml"
    config.write_text("tts:\n  voice_id: test\n", encoding="utf-8")
    registry = PathRegistry({"repo": ("Repo", repo)}, b"p" * 32)
    service = PlanningService(
        repo,
        repo / "state",
        registry,
        runner=lambda command, cwd: subprocess.CompletedProcess(command, 0, "[planned] render\n", ""),
    )

    plan = service.plan_single(
        SinglePlanRequest(
            source_token=registry.token_for(source),
            run_parent_token=registry.token_for(runs),
            run_name="episode-one",
            display_title="  Episode   One  ",
            config_token=registry.token_for(config),
        )
    )

    assert plan.display_title == "Episode One"
    assert service.get_plan(plan.plan_id).display_title == "Episode One"  # type: ignore[union-attr]


def test_run_name_rejects_more_than_eighty_characters() -> None:
    with pytest.raises(ValueError, match="at most 80 characters"):
        SinglePlanRequest(
            source_token="source",
            run_parent_token="runs",
            run_name="a" * 81,
            config_token="config",
        )


def test_filesystem_listing_exposes_safe_current_and_parent_tokens(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "season"
    nested.mkdir(parents=True)
    (nested / "episode.mp4").write_bytes(b"video")
    registry = PathRegistry({"media": ("Media", root)}, b"f" * 32)

    root_listing = registry.listing(root_id="media")
    assert root_listing.current.at_root
    assert root_listing.current.name == "Media"
    assert root_listing.current.parent_token is None
    nested_token = next(entry.token for entry in root_listing.entries if entry.name == "season")

    nested_listing = registry.listing(nested_token)
    assert not nested_listing.current.at_root
    assert nested_listing.current.name == "season"
    assert registry.resolve(nested_listing.current.token, expect="directory") == nested.resolve()
    assert registry.resolve(nested_listing.current.parent_token, expect="directory") == root.resolve()  # type: ignore[arg-type]


def test_inspection_endpoints_use_opaque_tokens_and_report_series_issues(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, token, repo = _client(tmp_path)
    first = repo / "first-episode.mp4"
    first.write_bytes(b"video")
    manifest = repo / "series_manifest.yaml"
    manifest.write_text(
        "series_id: demo-season\nseries_title: Demo Season\nepisodes:\n"
        "  - episode_key: s01e01\n    episode_number: 1\n    title: Start\n    arc: Arrival\n    source_path: first-episode.mp4\n"
        "  - episode_key: s01e02\n    episode_number: 2\n    title: Middle\n    arc: Arrival\n    source_path: first-episode.mp4\n"
        "  - episode_key: s01e03\n    episode_number: 3\n    title: End\n    arc: Finale\n    source_path: missing.mp4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("recap_ui.planning.probe_duration", lambda _path: 120.5)
    paths = client.app.state.paths
    headers = {"X-Recap-Token": token}

    single = client.post(
        "/api/inspect/single",
        json={"source_token": paths.token_for(first)},
        headers=headers,
    )
    assert single.status_code == 200
    assert single.json()["source_name"] == "first-episode.mp4"
    assert single.json()["duration_s"] == 120.5
    assert single.json()["media_valid"] is True
    assert str(repo.resolve()) not in single.text

    series = client.post(
        "/api/inspect/series",
        json={"manifest_token": paths.token_for(manifest), "episodes": "1-3"},
        headers=headers,
    )
    assert series.status_code == 200
    payload = series.json()
    assert payload["display_title"] == "Demo Season"
    assert payload["missing_source_episode_keys"] == ["s01e03"]
    assert payload["duplicate_source_episode_keys"] == ["s01e01", "s01e02"]
    assert payload["arc_preview"] == ["Arrival", "Finale"]
    assert payload["total_duration_s"] is None
    assert str(repo.resolve()) not in series.text


def test_presets_server_metadata_and_registration_are_safe(tmp_path: Path) -> None:
    client, token, repo = _client(tmp_path, host="::1", port=9876)
    config = repo / "config.secret.series.yaml"
    config.write_text(
        "review:\n  chatgpt_profile_dir: D:/private/PROFILE_GPT_1\n  llm_backend: chatgpt_playwright\n"
        "tts:\n  voice_id: secret-voice-id\n  provider_mode: auto\n"
        "postprocess:\n  enabled: true\n  profile: dynamic_anime\n  audio_assets: D:/private/audio_assets.yaml\n"
        "series_recap:\n  format: episode_arc_chaptered\n  detail_level: detailed\n  arc_size: 3\n",
        encoding="utf-8",
    )

    meta = client.get("/api/meta").json()
    assert meta["origin"] == "http://[::1]:9876"
    presets = client.get("/api/presets")
    assert presets.status_code == 200
    assert isinstance(presets.json(), list)
    assert "D:/private" not in presets.text
    # Voice IDs are non-secret configuration and are needed by the UI to show
    # which preset voice will be used; profile paths must remain redacted.
    assert "secret-voice-id" in presets.text
    assert any(item["summary"]["voice_configured"] is True for item in presets.json())

    preset_payload = presets.json()
    secret_summary = next(item["summary"] for item in preset_payload if item["name"] == "config.secret.series.yaml")
    assert secret_summary["tts"]["voice_id"] == "secret-voice-id"
    assert secret_summary["postprocess"]["enabled"] is True
    assert secret_summary["postprocess"]["audio_assets_ready"] is False
    assert secret_summary["postprocess"]["manifest_name"] == "audio_assets.yaml"
    assert "D:/private/audio_assets" not in presets.text
    assert "chatgpt_profile_dir" not in presets.text

    invalid = repo / "runs" / "empty"
    invalid.mkdir()
    headers = {"X-Recap-Token": token}
    response = client.post(
        "/api/runs/register",
        json={"path_token": client.app.state.paths.token_for(invalid)},
        headers=headers,
    )
    assert response.status_code == 422
    assert client.app.state.repository.list_registered_runs() == []

    (invalid / "summary.json").write_text("{}", encoding="utf-8")
    response = client.post(
        "/api/runs/register",
        json={"path_token": client.app.state.paths.token_for(invalid), "display_title": "Imported Run"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["display_title"] == "Imported Run"
    assert response.json()["available_actions"] == ["view"]


def test_fs_entries_remains_a_plain_array(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    runs_root = next(root for root in client.get("/api/fs/roots").json() if root["id"] == "runs")
    assert isinstance(client.get("/api/fs/entries", params={"root_id": "runs"}).json(), list)
    listing = client.get("/api/fs/listing", params={"token": runs_root["token"]}).json()
    assert listing["current"]["token"] == runs_root["token"]


def test_series_request_accepts_legacy_title_alias() -> None:
    request = SeriesPlanRequest.model_validate(
        {
            "manifest_token": "manifest",
            "run_dir_token": "run",
            "config_token": "config",
            "title": "  Season   One ",
        }
    )
    assert request.display_title == "Season One"
