from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

CHATGPT_PLAYWRIGHT_PROFILE_DIR = Path(
    r"D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1"
)
PROJECT_DISTRIBUTION = "recap"


def dependency_version_violations(distribution: str = PROJECT_DISTRIBUTION) -> list[str]:
    """List installed packages whose version falls outside the project's pinned ranges.

    A shared interpreter can be upgraded by other projects (for example transformers 5 or
    OpenCV 5), which breaks stages silently; absent optional packages are left to module probes.
    """
    try:
        requirements = metadata.requires(distribution) or []
    except metadata.PackageNotFoundError:
        return [f"{distribution} is not installed in this interpreter; run pip install -e ."]
    violations: list[str] = []
    checked: set[str] = set()
    for raw in requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            continue
        name = requirement.name.lower()
        if name in checked or not requirement.specifier:
            continue
        checked.add(name)
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            violations.append(f"{requirement.name} {installed} (requires {requirement.specifier})")
    return violations
