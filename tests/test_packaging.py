from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGES = {
    "common*",
    "episode_planner*",
    "ingest*",
    "match*",
    "orchestrator*",
    "preflight*",
    "render*",
    "review*",
    "series_composer*",
    "series_match*",
    "series_recap*",
    "shots*",
    "storymap*",
    "tts*",
    "visual_index*",
}
EXCLUDED_TOP_LEVEL = {
    "tests*",
    "runs*",
    "work*",
    "data*",
    "broll*",
    "tts_align*",
    "__pycache__*",
    "build*",
    "dist*",
    "*.egg-info*",
}
QUALITY_DEV_TOOLS = {
    "ruff",
    "tach",
}


def load_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_setuptools_uses_explicit_runtime_package_allowlist() -> None:
    config = load_pyproject()["tool"]["setuptools"]
    discovery = config["packages"]["find"]

    assert config["py-modules"] == ["run"]
    assert set(discovery["include"]) == RUNTIME_PACKAGES
    assert set(discovery["exclude"]) == EXCLUDED_TOP_LEVEL
    assert discovery["namespaces"] is False


def test_movie_visual_extra_is_union_of_required_visual_runtime_groups() -> None:
    extras = load_pyproject()["project"]["optional-dependencies"]
    expected = set(extras["asr-align"]) | set(extras["semantic-embed"]) | set(extras["visual-index"])

    assert set(extras["movie-visual"]) == expected
    assert all("open-clip" not in dependency.lower() for dependency in extras["movie-visual"])

def test_dev_extra_includes_quality_gate_tools() -> None:
    extras = load_pyproject()["project"]["optional-dependencies"]
    dev_dependencies = {dependency.split(">=", 1)[0].split("==", 1)[0] for dependency in extras["dev"]}

    assert QUALITY_DEV_TOOLS <= dev_dependencies

def test_ruff_starts_with_check_only_critical_rules() -> None:
    config = load_pyproject()["tool"]["ruff"]

    assert config["target-version"] == "py311"
    assert config["lint"]["select"] == ["E9", "F63", "F7", "F82"]

def test_pytest_disables_tach_plugin_by_default() -> None:
    config = load_pyproject()["tool"]["pytest"]["ini_options"]

    assert "-p" in config["addopts"]
    assert "no:tach" in config["addopts"]

def test_tach_tracks_runtime_boundaries() -> None:
    with (ROOT / "tach.toml").open("rb") as handle:
        config = tomllib.load(handle)

    modules = {module["path"]: set(module.get("depends_on", [])) for module in config["modules"]}

    assert set(modules) == {item.removesuffix("*") for item in RUNTIME_PACKAGES}
    assert modules["common"] == set()
    assert modules["match"] == {"common", "visual_index"}
    assert modules["series_composer"] == {"common", "review"}
    assert modules["series_match"] == {"common"}
    assert modules["series_recap"] == {"common", "orchestrator"}
    assert {"ingest", "match", "review", "tts"} <= modules["orchestrator"]
