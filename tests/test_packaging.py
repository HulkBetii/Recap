from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGES = {
    "common*",
    "ingest*",
    "match*",
    "orchestrator*",
    "preflight*",
    "reaction_remix*",
    "render*",
    "review*",
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


def load_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_setuptools_uses_explicit_runtime_package_allowlist() -> None:
    config = load_pyproject()["tool"]["setuptools"]
    discovery = config["packages"]["find"]

    assert config["py-modules"] == ["run", "run_reaction"]
    assert set(discovery["include"]) == RUNTIME_PACKAGES
    assert set(discovery["exclude"]) == EXCLUDED_TOP_LEVEL
    assert discovery["namespaces"] is False


def test_movie_visual_extra_is_union_of_required_visual_runtime_groups() -> None:
    extras = load_pyproject()["project"]["optional-dependencies"]
    expected = set(extras["asr-align"]) | set(extras["semantic-embed"]) | set(extras["visual-index"])

    assert set(extras["movie-visual"]) == expected
    assert all("open-clip" not in dependency.lower() for dependency in extras["movie-visual"])


def test_reaction_remix_extra_is_union_of_analysis_and_audio_groups() -> None:
    extras = load_pyproject()["project"]["optional-dependencies"]

    assert set(extras["reaction-remix"]) == set(extras["reaction-analysis"]) | set(extras["reaction-audio"])
