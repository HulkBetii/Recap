from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Sequence

import psutil


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    command_hash: str


def command_hash(argv: Sequence[str]) -> str:
    payload = json.dumps(
        [str(item) for item in argv],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def popen_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdout: int | IO[str] | None = subprocess.PIPE,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    flags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(
        [str(item) for item in argv],
        cwd=str(cwd),
        env=env,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
        start_new_session=start_new_session,
    )


def identity_for_process(process: subprocess.Popen[str], argv: Sequence[str]) -> ProcessIdentity:
    return ProcessIdentity(
        pid=process.pid,
        create_time=psutil.Process(process.pid).create_time(),
        command_hash=command_hash(argv),
    )


def process_matches(identity: ProcessIdentity) -> bool:
    try:
        process = psutil.Process(identity.pid)
        if abs(process.create_time() - identity.create_time) > 0.01:
            return False
        return command_hash(process.cmdline()) == identity.command_hash
    except (psutil.Error, OSError):
        return False


def terminate_process_tree(identity: ProcessIdentity, *, grace_s: float = 15.0) -> bool:
    if not process_matches(identity):
        return False

    process = psutil.Process(identity.pid)
    targets = _process_tree(process)
    if os.name == "nt":
        try:
            os.kill(identity.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except OSError:
            pass
    else:
        try:
            os.killpg(identity.pid, signal.SIGINT)
        except OSError:
            pass

    _, alive = psutil.wait_procs(targets, timeout=max(0.0, grace_s))
    if not alive:
        return True

    for target in reversed(alive):
        try:
            target.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=min(5.0, max(0.1, grace_s)))
    for target in alive:
        try:
            target.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5.0)
    return not psutil.pid_exists(identity.pid)


def _process_tree(process: psutil.Process) -> list[psutil.Process]:
    try:
        children = process.children(recursive=True)
    except psutil.Error:
        children = []
    return [process, *children]
