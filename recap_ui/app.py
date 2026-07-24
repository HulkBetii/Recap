from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Annotated, Any

import psutil
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from common.media import MediaError, probe_duration
from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from orchestrator.graph import STAGES

from recap_ui.database import Database
from recap_ui.health import collect_runtime_health
from recap_ui.planning import PlanningError, PlanningService
from recap_ui.process_control import command_hash
from recap_ui.repository import Repository
from recap_ui.runs import RunService
from recap_ui.schemas import (
    DeliveryStatus,
    ExecutionPlan,
    JobKind,
    JobRecord,
    JobStatus,
    RerunRequest,
    SeriesPlanRequest,
    SinglePlanRequest,
    RuntimeCheck,
)
from recap_ui.security import PathAccessError, PathRegistry, same_origin_allowed, startup_token, token_matches

TERMINAL_JOB_STATUSES = {
    JobStatus.CANCELLED,
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.INTERRUPTED,
    JobStatus.ORPHANED,
    JobStatus.BLOCKED,
}
PROFILE_DIR = CHATGPT_PLAYWRIGHT_PROFILE_DIR
RUN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def create_app(
    *,
    repo_root: Path | None = None,
    state_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> FastAPI:
    root = (repo_root or Path.cwd()).resolve()
    state = (state_dir or root / "data" / "recap_ui").resolve()
    state.mkdir(parents=True, exist_ok=True)
    database = Database(state / "recap_ui.db")
    database.initialize()
    repository = Repository(database)
    paths = PathRegistry.default(root, state)
    planning = PlanningService(root, state, paths)
    runs = RunService(root, repository, paths)
    mutation_token = token or startup_token(state)

    app = FastAPI(title="Recap Local UI", version="1.0.0")
    app.state.repo_root = root
    app.state.state_dir = state
    app.state.repository = repository
    app.state.paths = paths
    app.state.planning = planning
    app.state.runs = runs
    app.state.mutation_token = mutation_token
    app.state.job_creation_lock = threading.Lock()
    bracketed = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
    app.state.allowed_hosts = {
        f"{host}:{port}".casefold(),
        bracketed.casefold(),
        host.casefold(),
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
        "127.0.0.1",
        "localhost",
        "::1",
        "testserver",
    }

    @app.middleware("http")
    async def reject_untrusted_host(request: Request, call_next: Any) -> Any:
        if request.headers.get("host", "").casefold() not in app.state.allowed_hosts:
            return JSONResponse(status_code=403, content={"detail": "host is not allowed"})
        return await call_next(request)

    def require_mutation(request: Request, x_recap_token: Annotated[str | None, Header()] = None) -> None:
        request_host = request.headers.get("host", "")
        if request_host.casefold() not in app.state.allowed_hosts:
            raise HTTPException(status_code=403, detail="host is not allowed")
        if not same_origin_allowed(
            origin=request.headers.get("origin"),
            host=request_host,
            allowed_host=request_host,
        ):
            raise HTTPException(status_code=403, detail="cross-origin mutation is not allowed")
        if not token_matches(app.state.mutation_token, x_recap_token):
            raise HTTPException(status_code=403, detail="invalid startup token")

    mutation_guard = Depends(require_mutation)

    @app.exception_handler(PathAccessError)
    async def path_access_error_handler(_request: Request, exc: PathAccessError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(PlanningError)
    async def planning_error_handler(_request: Request, exc: PlanningError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(subprocess.TimeoutExpired)
    async def planning_timeout_handler(_request: Request, _exc: subprocess.TimeoutExpired) -> JSONResponse:
        return JSONResponse(status_code=504, content={"detail": "pipeline dry-run timed out"})

    @app.get("/api/session")
    def get_session() -> dict[str, str]:
        return {"token": app.state.mutation_token}

    @app.get("/api/health")
    def get_health() -> dict[str, Any]:
        report = collect_runtime_health(root, profile_dir=PROFILE_DIR)
        return {
            "runtime": report.runtime,
            "providers": report.providers,
            "status": report.status,
            "generated_at": report.generated_at,
            "active_job": repository.active_job(),
        }

    @app.post("/api/health/chatgpt-profile", dependencies=[mutation_guard])
    async def test_chatgpt_profile() -> dict[str, Any]:
        if repository.active_job() is not None or repository.get_lock("profile:PROFILE_GPT_1") is not None:
            raise HTTPException(status_code=409, detail="the ChatGPT profile is reserved by a pipeline job")
        lock_path = state / "profile_test.lock"
        descriptor = _acquire_marker(lock_path)
        try:
            if repository.active_job() is not None:
                raise HTTPException(status_code=409, detail="a pipeline job started before the profile test")
            users = _processes_using_profile(PROFILE_DIR)
            if users:
                raise HTTPException(status_code=409, detail={"message": "the ChatGPT profile is already in use", "pids": users})
            try:
                url = await _test_profile_no_send(PROFILE_DIR)
                return {"status": "pass", "composer_available": True, "url": url, "sent": False}
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": str(exc),
                        "code": "profile_smoke_failed",
                        "composer_available": False,
                        "sent": False,
                    },
                ) from exc
        finally:
            _release_marker(lock_path, descriptor)

    @app.get("/api/presets")
    def get_presets() -> list[dict[str, Any]]:
        presets: list[dict[str, Any]] = []
        for path in sorted(root.glob("config*.yaml")):
            presets.append(
                {
                    "id": path.stem,
                    "name": path.name,
                    "token": paths.token_for(path),
                    "kind": "series" if ".series" in path.name else "single",
                }
            )
        return presets

    @app.get("/api/fs/roots")
    def get_filesystem_roots() -> list[Any]:
        return paths.roots()

    @app.get("/api/fs/entries")
    def get_filesystem_entries(
        token: str | None = None,
        root_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> list[Any]:
        return paths.list_entries(token, root_id=root_id, limit=limit)

    @app.post("/api/fs/child-token", dependencies=[mutation_guard])
    def create_child_path_token(payload: dict[str, str] = Body(...)) -> dict[str, str]:
        parent = paths.resolve(payload.get("parent_token", ""), expect="directory")
        name = payload.get("name", "").strip()
        if not RUN_NAME_PATTERN.fullmatch(name) or name in {".", ".."}:
            raise HTTPException(status_code=422, detail="run name contains unsupported characters")
        child = parent / name
        if child.exists() and not child.is_dir():
            raise HTTPException(status_code=409, detail="run path already exists as a file")
        return {"token": paths.token_for(child), "name": name}

    @app.post("/api/plans/single", dependencies=[mutation_guard])
    def plan_single(request: SinglePlanRequest) -> ExecutionPlan:
        return planning.plan_single(request)

    @app.post("/api/plans/series", dependencies=[mutation_guard])
    def plan_series(request: SeriesPlanRequest) -> ExecutionPlan:
        return planning.plan_series(request)

    @app.post("/api/preflight", dependencies=[mutation_guard])
    def preflight(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        request_payload = dict(payload)
        kind = str(request_payload.pop("kind", "single"))
        if kind == JobKind.SINGLE:
            plan = planning.plan_single(SinglePlanRequest.model_validate(request_payload))
        elif kind == JobKind.SERIES:
            plan = planning.plan_series(SeriesPlanRequest.model_validate(request_payload))
        else:
            raise HTTPException(status_code=422, detail="kind must be single or series")
        health = collect_runtime_health(
            root,
            profile_dir=PROFILE_DIR,
            active_job=repository.active_job(),
            config=plan.config_snapshot,
        )
        checks: list[Any] = [*plan.checks, *_runtime_checks_for_plan(health.runtime, plan)]
        checks.extend(_provider_checks(plan.config_snapshot, health.providers, plan))
        checks.extend(_media_duration_checks(plan))
        can_start = plan.can_start and not any(check.status.value == "block" for check in checks)
        return {
            "checks": checks,
            "warnings": plan.warnings,
            "providers": health.providers,
            "can_start": can_start,
            "plan_id": plan.plan_id,
        }

    @app.get("/api/jobs")
    def list_jobs(limit: int = Query(default=100, ge=1, le=1000)) -> list[JobRecord]:
        return repository.list_jobs(limit=limit)

    @app.post("/api/jobs", dependencies=[mutation_guard])
    def create_job(payload: dict[str, str] = Body(...)) -> JobRecord:
        plan_id = payload.get("plan_id", "")
        plan = planning.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan does not exist")
        if not plan.can_start:
            raise HTTPException(status_code=409, detail="plan has blocking preflight checks")
        if plan.dag:
            health = collect_runtime_health(root, profile_dir=PROFILE_DIR, config=plan.config_snapshot)
            runtime_checks: list[Any] = [
                *_runtime_checks_for_plan(health.runtime, plan),
                *_provider_checks(plan.config_snapshot, health.providers, plan),
            ]
            blockers = [check.code for check in runtime_checks if check.status.value == "block"]
            if blockers:
                raise HTTPException(status_code=409, detail={"message": "runtime preflight is blocked", "checks": blockers})
        with app.state.job_creation_lock:
            _ensure_run_not_pending(repository, plan.run_dir)
            return repository.create_job(plan, config_path=plan.config_path)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = _require_job(repository, job_id)
        return {"job": job, "stages": repository.list_stages(job_id)}

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[mutation_guard])
    def cancel_job(job_id: str) -> JobRecord:
        _require_job(repository, job_id)
        return repository.request_cancel(job_id)

    @app.post("/api/jobs/{job_id}/resume", dependencies=[mutation_guard])
    def resume_job(job_id: str) -> JobRecord:
        job = _require_job(repository, job_id)
        if job.status not in {JobStatus.FAILED, JobStatus.INTERRUPTED, JobStatus.CANCELLED, JobStatus.BLOCKED}:
            raise HTTPException(status_code=409, detail="job is not resumable")
        command = _without_force_flags(job.argv)
        plan = _plan_from_job(job, command)
        with app.state.job_creation_lock:
            _ensure_run_not_pending(repository, job.run_dir, exclude_job_id=job.id)
            return repository.create_job(plan, config_path=job.config_path, parent_job_id=job.id)

    @app.post("/api/jobs/{job_id}/rerun", dependencies=[mutation_guard])
    def rerun_job(job_id: str, request: RerunRequest) -> JobRecord:
        job = _require_job(repository, job_id)
        command = _without_force_flags(job.argv)
        if job.kind == JobKind.SINGLE:
            if request.scope != "stage" or not request.stage:
                raise HTTPException(status_code=422, detail="single rerun requires scope=stage and a stage")
            if request.stage not in STAGES:
                raise HTTPException(status_code=422, detail="unknown single-run stage")
            command.extend(["--force-stage", request.stage])
        elif request.scope == "final":
            command.append("--force-final")
        elif request.scope == "all":
            command.append("--force")
        else:
            raise HTTPException(status_code=422, detail="series rerun scope must be final or all")
        plan = _plan_from_job(job, command)
        with app.state.job_creation_lock:
            _ensure_run_not_pending(repository, job.run_dir, exclude_job_id=job.id)
            return repository.create_job(plan, config_path=job.config_path, parent_job_id=job.id)

    @app.get("/api/jobs/{job_id}/events")
    def get_events(
        job_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> list[Any]:
        _require_job(repository, job_id)
        return repository.list_events(job_id, after_seq=after_seq, limit=limit)

    @app.get("/api/jobs/{job_id}/events/stream")
    async def stream_events(request: Request, job_id: str, after_seq: int = Query(default=0, ge=0)) -> StreamingResponse:
        _require_job(repository, job_id)
        header_cursor = request.headers.get("last-event-id")
        if header_cursor and header_cursor.isdigit():
            after_seq = max(after_seq, int(header_cursor))

        async def event_stream() -> Any:
            cursor = after_seq
            idle_ticks = 0
            while not await request.is_disconnected():
                events = repository.list_events(job_id, after_seq=cursor, limit=1000)
                if events:
                    idle_ticks = 0
                    for event in events:
                        cursor = event.seq
                        payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {event.seq}\nevent: {event.event_type.value}\ndata: {payload}\n\n"
                else:
                    idle_ticks += 1
                    if idle_ticks % 15 == 0:
                        yield ": keepalive\n\n"
                    await asyncio.sleep(1.0)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs")
    def list_runs() -> list[Any]:
        return runs.discover_runs()

    @app.post("/api/runs/register", dependencies=[mutation_guard])
    def register_run(payload: dict[str, str] = Body(...)) -> Any:
        run_path = paths.resolve(payload.get("path_token", ""), expect="directory")
        kind_text = payload.get("kind")
        kind = JobKind(kind_text) if kind_text else None
        repository.register_run(run_path, kind)
        for run in runs.discover_runs():
            if paths.resolve(run.path_token, expect="directory") == run_path:
                return run
        raise HTTPException(status_code=422, detail="directory does not contain a recognizable run")

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> Any:
        return _require_run(runs, run_id)

    @app.get("/api/runs/{run_id}/episodes")
    def get_run_episodes(run_id: str) -> list[Any]:
        _require_run(runs, run_id)
        return runs.episodes(run_id)

    @app.get("/api/runs/{run_id}/episodes/{episode_key}")
    def get_episode_detail(run_id: str, episode_key: str) -> Any:
        _require_run(runs, run_id)
        try:
            return runs.get_episode_detail(run_id, episode_key)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="episode does not exist") from exc

    @app.get("/api/runs/{run_id}/artifacts")
    def get_run_artifacts(run_id: str) -> list[Any]:
        _require_run(runs, run_id)
        return runs.list_artifacts(run_id)

    @app.get("/api/runs/{run_id}/qa")
    def get_run_qa(run_id: str) -> Any:
        _require_run(runs, run_id)
        return runs.qa(run_id)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def get_artifact(run_id: str, artifact_id: str) -> FileResponse:
        artifact = _resolve_artifact(runs, run_id, artifact_id)
        return FileResponse(artifact, filename=artifact.name)

    @app.get("/api/runs/{run_id}/media/{artifact_id}")
    def get_media(run_id: str, artifact_id: str) -> FileResponse:
        artifact = _resolve_artifact(runs, run_id, artifact_id)
        if artifact.suffix.lower() not in {".mp4", ".mp3", ".wav", ".jpg", ".jpeg", ".png", ".webp"}:
            raise HTTPException(status_code=415, detail="artifact type cannot be served inline")
        return FileResponse(artifact, filename=artifact.name, content_disposition_type="inline")

    _mount_frontend(app, mutation_token)
    return app


def _require_job(repository: Repository, job_id: str) -> JobRecord:
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job does not exist")
    return job


def _provider_checks(
    config: dict[str, Any],
    providers: dict[str, bool],
    plan: ExecutionPlan,
) -> list[RuntimeCheck]:
    checks = [
        RuntimeCheck(
            code=f"{name}_api_key",
            status=DeliveryStatus.PASS if available else DeliveryStatus.WARN,
            message=f"{name.upper()} API key is configured" if available else f"{name.upper()} API key is not configured",
            details={"configured": available},
        )
        for name, available in sorted(providers.items())
    ]
    if not _plan_needs_tts(plan):
        return checks

    tts = config.get("tts", {})
    mode = str(tts.get("provider_mode") or "auto")
    ai33_ready = bool(providers.get("vivoo") and tts.get("voice_id"))
    genmax_ready = bool(providers.get("genmax") and tts.get("genmax_voice_id"))
    openai_ready = bool(providers.get("openai") and tts.get("openai_voice"))
    ready = {
        "ai33": ai33_ready,
        "genmax": genmax_ready,
        "openai": openai_ready,
        "auto": ai33_ready or genmax_ready or openai_ready,
    }.get(mode, False)
    checks.append(
        RuntimeCheck(
            code="tts_provider_ready",
            status=DeliveryStatus.PASS if ready else DeliveryStatus.BLOCK,
            message=f"TTS provider mode {mode} has a configured provider and voice" if ready else f"TTS provider mode {mode} is not ready",
            details={
                "mode": mode,
                "ai33_ready": ai33_ready,
                "genmax_ready": genmax_ready,
                "openai_ready": openai_ready,
            },
        )
    )
    return checks


def _runtime_checks_for_plan(checks: list[RuntimeCheck], plan: ExecutionPlan) -> list[RuntimeCheck]:
    if _plan_needs_tts(plan):
        return checks
    return [check for check in checks if check.code != "tts_voice"]


def _plan_needs_tts(plan: ExecutionPlan) -> bool:
    tts_nodes = [node for node in plan.dag if node.key == "tts" or node.key.endswith(":tts")]
    return any(node.status.casefold() not in {"skip", "skipped", "skipped_valid"} for node in tts_nodes)


def _media_duration_checks(plan: ExecutionPlan) -> list[RuntimeCheck]:
    try:
        if plan.kind == JobKind.SINGLE:
            source = _argument_path(plan.command, "--input")
            sources = [source] if source is not None else []
        else:
            from series_recap.__main__ import manifest_episode_specs, select_episodes

            manifest = _argument_path(plan.command, "--manifest")
            if manifest is None:
                sources = []
            else:
                _, all_specs = manifest_episode_specs(manifest)
                selected = select_episodes(all_specs, _argument_value(plan.command, "--episodes"))
                sources = [spec.source_path for spec in selected]
    except (OSError, RuntimeError, ValueError):
        return [
            RuntimeCheck(
                code="media_durations",
                status=DeliveryStatus.WARN,
                message="Media durations could not be collected because source validation is already blocked",
            )
        ]

    if not sources:
        return [RuntimeCheck(code="media_durations", status=DeliveryStatus.BLOCK, message="No source media was selected")]
    durations: list[dict[str, Any]] = []
    failures: list[str] = []
    for source in sources:
        try:
            durations.append({"name": source.name, "duration_s": round(probe_duration(source), 3)})
        except (MediaError, OSError):
            failures.append(source.name)
    return [
        RuntimeCheck(
            code="media_durations",
            status=DeliveryStatus.BLOCK if failures else DeliveryStatus.PASS,
            message=f"Probed {len(durations)}/{len(sources)} source media duration(s)",
            details={"sources": durations, "failed": failures},
        )
    ]


def _argument_value(command: list[str], option: str) -> str | None:
    try:
        index = command.index(option)
    except ValueError:
        return None
    return command[index + 1] if index + 1 < len(command) else None


def _argument_path(command: list[str], option: str) -> Path | None:
    value = _argument_value(command, option)
    return Path(value) if value else None


def _ensure_run_not_pending(repository: Repository, run_dir: str, *, exclude_job_id: str | None = None) -> None:
    pending = {
        JobStatus.QUEUED,
        JobStatus.STARTING,
        JobStatus.RUNNING,
        JobStatus.CANCEL_REQUESTED,
    }
    target = str(Path(run_dir).resolve()).casefold()
    for job in repository.list_jobs(limit=1000, statuses=pending):
        if job.id == exclude_job_id:
            continue
        if str(Path(job.run_dir).resolve()).casefold() == target:
            raise HTTPException(status_code=409, detail=f"run directory already has a nonterminal job: {job.id}")


def _require_run(runs: RunService, run_id: str) -> Any:
    try:
        return runs.get_run(run_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="run does not exist") from exc


def _resolve_artifact(runs: RunService, run_id: str, artifact_id: str) -> Path:
    try:
        return runs.resolve_artifact(run_id, artifact_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="artifact does not exist") from exc


def _acquire_marker(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "create_time": psutil.Process(os.getpid()).create_time()},
        separators=(",", ":"),
    ).encode("utf-8")
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if _marker_owner_alive(path):
                raise HTTPException(status_code=409, detail="ChatGPT profile test is already running")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        os.write(descriptor, payload)
        return descriptor
    raise HTTPException(status_code=409, detail="could not acquire the ChatGPT profile test lock")


def _release_marker(path: Path, descriptor: int) -> None:
    os.close(descriptor)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


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


def _marker_owner_alive(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        process = psutil.Process(int(payload["pid"]))
        return abs(process.create_time() - float(payload["create_time"])) <= 0.01
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, psutil.Error):
        return False


async def _test_profile_no_send(profile_dir: Path) -> str:
    from playwright.async_api import async_playwright

    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (profile_dir / lock_name).unlink()
        except FileNotFoundError:
            pass
    playwright = await async_playwright().start()
    context = None
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        composer = page.locator("#prompt-textarea, div[contenteditable='true'][data-lexical-editor='true']").first
        await composer.wait_for(state="visible", timeout=15_000)
        return str(page.url)
    finally:
        if context is not None:
            await context.close()
        await playwright.stop()


def _without_force_flags(command: list[str]) -> list[str]:
    stripped: list[str] = []
    skip_next = False
    for item in command:
        if skip_next:
            skip_next = False
            continue
        if item == "--force-stage":
            skip_next = True
            continue
        if item in {"--force", "--force-final"}:
            continue
        stripped.append(item)
    return stripped


def _plan_from_job(job: JobRecord, command: list[str]) -> ExecutionPlan:
    plan_id = uuid.uuid4().hex
    dry_run = [*_without_force_flags(command), "--dry-run"]
    return ExecutionPlan(
        plan_id=plan_id,
        kind=job.kind,
        command=command,
        dry_run_command=dry_run,
        run_dir=job.run_dir,
        config_path=job.config_path,
        config_snapshot=job.config_snapshot,
        dag=[],
        output_paths=[],
        checks=[],
        warnings=[],
        can_start=True,
        command_hash=command_hash(command),
    )


def _mount_frontend(app: FastAPI, token: str) -> None:
    static_dir = Path(__file__).resolve().parent / "static"
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str) -> HTMLResponse:
        if spa_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route does not exist")
        index_path = static_dir / "index.html"
        if not index_path.is_file():
            return HTMLResponse(
                "<html><head><title>Recap UI</title></head><body><h1>Recap UI assets are not built.</h1>"
                "<p>Run the Vite production build before using the daily launcher.</p></body></html>",
                status_code=503,
            )
        html = index_path.read_text(encoding="utf-8")
        meta = f'<meta name="recap-token" content="{token}">'
        html = html.replace("</head>", meta + "</head>", 1)
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                    "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
                )
            },
        )


app = create_app()
