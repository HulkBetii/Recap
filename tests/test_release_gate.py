from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.check_secrets import scan_history, scan_text, scan_tracked_tree
from scripts.release_helpers import (
    RUNTIME_ROOTS,
    artifact_rebuilt,
    artifact_unchanged,
    inspect_wheel,
    resolve_release_work_dir,
    snapshot_ingest_cache,
)

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_secret_scanner_redacts_realistic_keys_and_ignores_placeholder() -> None:
    secret = "sk-" + "proj-" + ("A" * 24)
    findings = scan_text("fixture", f'key = "{secret}"\nexample = "sk-..."')

    assert len(findings) == 1
    assert findings[0].kind == "openai_key"
    assert secret not in findings[0].redacted
    assert findings[0].redacted.startswith("sk-proj")


def test_secret_scanner_flags_tracked_env(tmp_path: Path) -> None:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "release@example.com")
    git(tmp_path, "config", "user.name", "Release Test")
    (tmp_path / ".env").write_text("SAFE=value\n", encoding="utf-8")
    git(tmp_path, "add", ".env")

    findings = scan_tracked_tree(tmp_path)

    assert any(item.kind == "tracked_env" for item in findings)


def test_secret_scanner_finds_removed_key_in_history(tmp_path: Path) -> None:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "release@example.com")
    git(tmp_path, "config", "user.name", "Release Test")
    secret = "sk_" + ("B" * 30)
    path = tmp_path / "config.txt"
    path.write_text(secret + "\n", encoding="utf-8")
    git(tmp_path, "add", "config.txt")
    git(tmp_path, "commit", "-m", "add fixture")
    path.write_text("clean\n", encoding="utf-8")
    git(tmp_path, "add", "config.txt")
    git(tmp_path, "commit", "-m", "remove fixture")

    findings = scan_history(tmp_path)

    assert any(item.kind == "provider_key" for item in findings)
    assert all(secret not in item.redacted for item in findings)


def test_wheel_inspector_requires_runtime_roots_and_excludes_artifacts(tmp_path: Path) -> None:
    wheel = tmp_path / "recap-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("run.py", "")
        for root in RUNTIME_ROOTS:
            archive.writestr(f"{root}/__init__.py", "")
        archive.writestr("recap-0.1.0.dist-info/METADATA", "")

    report = inspect_wheel(wheel)

    assert report["entry_module"] == "run.py"
    assert set(report["runtime_roots"]) == RUNTIME_ROOTS


def test_wheel_inspector_rejects_tests_directory(tmp_path: Path) -> None:
    wheel = tmp_path / "recap-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("run.py", "")
        for root in RUNTIME_ROOTS:
            archive.writestr(f"{root}/__init__.py", "")
        archive.writestr("tests/test_bad.py", "")

    with pytest.raises(ValueError, match="excluded"):
        inspect_wheel(wheel)


def test_release_work_dir_must_stay_below_work(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "work").mkdir(parents=True)

    assert resolve_release_work_dir(Path("work/release-gate"), root=root) == (root / "work/release-gate").resolve()
    with pytest.raises(ValueError):
        resolve_release_work_dir(Path("outside"), root=root)


def test_cache_snapshot_helpers_detect_reuse_and_rebuild(tmp_path: Path) -> None:
    work = tmp_path / "ingest"
    work.mkdir()
    (work / "cache_manifest.json").write_text(json.dumps({"cache_version": "ingest-v1", "keys": {"audio": "a"}}), encoding="utf-8")
    (work / "audio.wav").write_bytes(b"first")
    first = snapshot_ingest_cache(work)
    second = snapshot_ingest_cache(work)
    assert artifact_unchanged(first, second, "audio.wav")

    (work / "audio.wav").write_bytes(b"second")
    third = snapshot_ingest_cache(work)
    assert artifact_rebuilt(second, third, "audio.wav")


def test_release_workflow_is_windows_offline_and_uses_full_history() -> None:
    workflow = (ROOT / ".github/workflows/release-gate.yml").read_text(encoding="utf-8")

    assert "runs-on: windows-latest" in workflow
    assert 'python-version: "3.11"' in workflow
    assert "fetch-depth: 0" in workflow
    assert "-SkipMediaSmoke" in workflow
    assert "secrets." not in workflow
    assert "playwright install" not in workflow.lower()


def test_project_version_matches_v1_0_2_release() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project["version"] == "1.0.2"
