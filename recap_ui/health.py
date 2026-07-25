from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from recap_ui.schemas import DeliveryStatus, JobRecord, RuntimeCheck, RuntimeHealth


def collect_runtime_health(
    repo_root: Path,
    *,
    profile_dir: Path | None = None,
    active_job: JobRecord | None = None,
    config: dict[str, Any] | None = None,
) -> RuntimeHealth:
    repo = repo_root.expanduser().resolve()
    profile = (profile_dir or CHATGPT_PLAYWRIGHT_PROFILE_DIR).expanduser().resolve()
    checks = [
        _binary_check("python", sys.executable),
        _binary_check("ffmpeg", shutil.which("ffmpeg")),
        _binary_check("ffprobe", shutil.which("ffprobe")),
        _module_check("torch"),
        _module_check("whisperx", optional=True),
        _profile_check(profile),
        _disk_check(repo),
    ]
    torch_available = any(check.code == "torch" and check.status == DeliveryStatus.PASS for check in checks)
    checks.append(_cuda_check() if torch_available else RuntimeCheck(
        code="cuda",
        status=DeliveryStatus.WARN,
        message="CUDA cannot be checked because Torch is unavailable",
    ))
    if config is not None:
        tts = config.get("tts", {})
        mode = str(tts.get("provider_mode") or "auto")
        if mode == "vieneu":
            checks.extend([_module_check("vieneu"), _module_check("onnxruntime"), _module_check("soundfile")])
        configured = {
            "ai33": bool(tts.get("voice_id")),
            "genmax": bool(tts.get("genmax_voice_id")),
            "openai": bool(tts.get("openai_voice")),
            "vieneu": bool(tts.get("voice_id")),
            "auto": bool(tts.get("voice_id") or tts.get("genmax_voice_id") or tts.get("openai_voice")),
        }.get(mode, False)
        checks.append(
            RuntimeCheck(
                code="tts_voice",
                status=DeliveryStatus.PASS if configured else DeliveryStatus.BLOCK,
                message=f"TTS voice is configured for provider mode {mode}" if configured else f"TTS voice is missing for provider mode {mode}",
                details={"configured": configured, "mode": mode},
            )
        )
    status = _health_status(checks)
    return RuntimeHealth(
        status=status,
        runtime=checks,
        providers={
            "openai": bool(os.getenv("OPENAI_API_KEY")),
            "vivoo": bool(os.getenv("VIVOO_API_KEY")),
            "genmax": bool(os.getenv("GENMAX_API_KEY")),
        },
        active_job=active_job,
    )


def _binary_check(code: str, path: str | None) -> RuntimeCheck:
    return RuntimeCheck(
        code=code,
        status=DeliveryStatus.PASS if path else DeliveryStatus.BLOCK,
        message=f"{code} is available" if path else f"{code} was not found",
        details={"available": bool(path), "path": path},
    )


def _module_check(module: str, *, optional: bool = False) -> RuntimeCheck:
    available = importlib.util.find_spec(module) is not None
    return RuntimeCheck(
        code=module,
        status=DeliveryStatus.PASS if available else DeliveryStatus.WARN if optional else DeliveryStatus.BLOCK,
        message=f"Python module {module} is available" if available else f"Python module {module} is not installed",
        details={"available": available, "optional": optional},
    )


def _cuda_check() -> RuntimeCheck:
    try:
        import torch

        available = bool(torch.cuda.is_available())
        device = torch.cuda.get_device_name(0) if available else None
    except (ImportError, RuntimeError):
        available = False
        device = None
    return RuntimeCheck(
        code="cuda",
        status=DeliveryStatus.PASS if available else DeliveryStatus.WARN,
        message=f"CUDA is available: {device}" if available else "CUDA is not available to Torch",
        details={"available": available, "device": device},
    )


def _profile_check(profile: Path) -> RuntimeCheck:
    exists = profile.is_dir()
    in_use = _profile_in_use(profile) if exists else False
    status = DeliveryStatus.WARN if in_use else DeliveryStatus.PASS if exists else DeliveryStatus.BLOCK
    message = "ChatGPT profile is currently in use" if in_use else "ChatGPT profile is available" if exists else "ChatGPT profile directory is missing"
    return RuntimeCheck(
        code="chatgpt_profile",
        status=status,
        message=message,
        details={"exists": exists, "in_use": in_use, "profile_name": profile.name},
    )


def _profile_in_use(profile: Path) -> bool:
    try:
        import psutil
    except ImportError:
        return any((profile / name).exists() for name in ("SingletonLock", "SingletonCookie"))
    needle = str(profile).casefold()
    for process in psutil.process_iter(["cmdline"]):
        try:
            command = " ".join(process.info.get("cmdline") or []).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if needle in command:
            return True
    return False


def _disk_check(path: Path) -> RuntimeCheck:
    usage = shutil.disk_usage(path)
    free_gib = usage.free / (1024 ** 3)
    status = DeliveryStatus.BLOCK if free_gib < 10 else DeliveryStatus.WARN if free_gib < 50 else DeliveryStatus.PASS
    return RuntimeCheck(
        code="disk",
        status=status,
        message=f"{free_gib:.1f} GiB free on the workspace volume",
        details={"free_bytes": usage.free, "total_bytes": usage.total},
    )


def _health_status(checks: list[RuntimeCheck]) -> DeliveryStatus:
    if any(check.status == DeliveryStatus.BLOCK for check in checks):
        return DeliveryStatus.BLOCK
    if any(check.status == DeliveryStatus.WARN for check in checks):
        return DeliveryStatus.WARN
    return DeliveryStatus.PASS
