from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import threading
import uuid
import webbrowser
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Recap local web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    open_group = parser.add_mutually_exclusive_group()
    open_group.add_argument("--open", dest="open_browser", action="store_true")
    open_group.add_argument("--no-open", dest="open_browser", action="store_false")
    parser.set_defaults(open_browser=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("recap_ui only supports loopback hosts in V1")

    import uvicorn
    import psutil

    from recap_ui.app import create_app
    from recap_ui.security import startup_token
    from recap_ui.worker import run_worker_process

    repo_root = args.repo_root.expanduser().resolve()
    state_dir = (args.state_dir or repo_root / "data" / "recap_ui").expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    database_path = state_dir / "recap_ui.db"
    token = startup_token(state_dir)
    server_lock_path = state_dir / "server.lock"
    server_lock_token = _acquire_server_lock(server_lock_path, psutil)

    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    worker = context.Process(
        target=run_worker_process,
        kwargs={
            "database_path": database_path,
            "repo_root": repo_root,
            "state_dir": state_dir,
            "stop_event": stop_event,
        },
        name="recap-ui-worker",
    )
    try:
        worker.start()

        if args.open_browser:
            url = f"http://{args.host}:{args.port}/runs"
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()

        application = create_app(
            repo_root=repo_root,
            state_dir=state_dir,
            host=args.host,
            port=args.port,
            token=token,
        )
        uvicorn.run(application, host=args.host, port=args.port, log_level="info")
    finally:
        stop_event.set()
        if worker.pid is not None:
            worker.join(timeout=10.0)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5.0)
        _release_server_lock(server_lock_path, server_lock_token)
    return 0


def _acquire_server_lock(path: Path, psutil_module: object) -> str:
    lock_token = uuid.uuid4().hex
    current_process = psutil_module.Process(os.getpid())  # type: ignore[attr-defined]
    payload = {
        "pid": os.getpid(),
        "create_time": current_process.create_time(),
        "token": lock_token,
    }
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                process = psutil_module.Process(int(existing["pid"]))  # type: ignore[attr-defined]
                active = abs(process.create_time() - float(existing["create_time"])) <= 0.01
            except (OSError, ValueError, KeyError, json.JSONDecodeError, psutil_module.Error):  # type: ignore[attr-defined]
                active = False
            if active:
                raise SystemExit(f"Recap UI is already running (PID {existing['pid']})")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return lock_token
    raise SystemExit(f"Could not acquire Recap UI server lock: {path}")


def _release_server_lock(path: Path, lock_token: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    if payload.get("token") != lock_token:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
