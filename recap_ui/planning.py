from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from common.integrity import atomic_write_json, stable_hash
from common.inputs import load_series_manifest
from common.media import probe_duration
from orchestrator.config import ConfigError, load_config
from orchestrator.graph import STAGES, build_paths as build_single_paths, stage_range
from orchestrator.runner import episode_planner_enabled, output_paths
from series_recap.__main__ import (
    EpisodeSpec,
    build_paths as build_series_paths,
    default_episode_key,
    numeric_episode,
    resolve_source_path,
    select_episodes,
    top_level_episode,
)

from recap_ui.schemas import (
    DeliveryStatus,
    ExecutionPlan,
    JobKind,
    PlanCheck,
    PlanDagNode,
    SeriesEpisodeInspection,
    SeriesPlanRequest,
    SeriesSourceInspection,
    SingleSourceInspection,
    SinglePlanRequest,
)
from recap_ui.security import PathAccessError, PathRegistry


class PlanningError(ValueError):
    pass


DryRunRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


_SAFE_OVERRIDE_KEYS: dict[str, set[str]] = {
    "orchestrator": {"log_level"},
    "review": {"target_ratio"},
    "tts": {"voice_id", "provider_mode", "vieneu_style", "speed", "concurrency"},
    "render": {"crf", "preset", "concurrency"},
    "series_recap": {
        "target_total_min_s",
        "target_total_max_s",
        "target_total_hard_cap_s",
        "arc_size",
    },
}
_INVALID_RUN_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def apply_safe_overrides(
    config: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
    *,
    kind: JobKind,
) -> dict[str, Any]:
    copied = json.loads(json.dumps(config))
    for section, values in overrides.items():
        if section not in _SAFE_OVERRIDE_KEYS or not isinstance(values, dict):
            raise PlanningError(f"unsafe or unknown override section: {section}")
        unknown = set(values) - _SAFE_OVERRIDE_KEYS[section]
        if unknown:
            raise PlanningError(f"unsafe override key(s) in {section}: {', '.join(sorted(unknown))}")
        if section == "series_recap" and kind != JobKind.SERIES:
            raise PlanningError("series_recap overrides are only valid for series jobs")
        copied.setdefault(section, {}).update(values)
    _validate_overrides(copied, overrides, kind=kind)
    return copied


def _validate_overrides(
    config: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
    *,
    kind: JobKind,
) -> None:
    orchestrator = overrides.get("orchestrator", {})
    if "log_level" in orchestrator and orchestrator["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise PlanningError("orchestrator.log_level must be DEBUG, INFO, WARNING or ERROR")
    review = overrides.get("review", {})
    if "target_ratio" in review:
        value = review["target_ratio"]
        if value != "auto":
            try:
                ratio = float(value)
            except (TypeError, ValueError) as exc:
                raise PlanningError("review.target_ratio must be auto or a number") from exc
            if not 0.01 <= ratio <= 1.0:
                raise PlanningError("review.target_ratio must be between 0.01 and 1.0")
            config["review"]["target_ratio"] = ratio
    tts = overrides.get("tts", {})
    if "provider_mode" in tts and tts["provider_mode"] not in {"auto", "ai33", "genmax", "openai", "vieneu"}:
        raise PlanningError("tts.provider_mode is not supported")
    if "voice_id" in tts and (not isinstance(tts["voice_id"], str) or not tts["voice_id"].strip()):
        raise PlanningError("tts.voice_id cannot be empty")
    if "vieneu_style" in tts and tts["vieneu_style"] not in {"tu_nhien", "tin_tuc", "doc_truyen"}:
        raise PlanningError("tts.vieneu_style is not supported")
    if "speed" in tts and not 0.8 <= float(tts["speed"]) <= 1.2:
        raise PlanningError("tts.speed must be between 0.8 and 1.2")
    if "concurrency" in tts and not 1 <= int(tts["concurrency"]) <= 8:
        raise PlanningError("tts.concurrency must be between 1 and 8")
    render = overrides.get("render", {})
    if "crf" in render and not 16 <= int(render["crf"]) <= 28:
        raise PlanningError("render.crf must be between 16 and 28")
    if "preset" in render and render["preset"] not in {"slow", "medium", "fast"}:
        raise PlanningError("render.preset must be slow, medium or fast")
    if "concurrency" in render and not 1 <= int(render["concurrency"]) <= 8:
        raise PlanningError("render.concurrency must be between 1 and 8")
    if kind == JobKind.SERIES:
        series = config["series_recap"]
        minimum = float(series["target_total_min_s"])
        maximum = float(series["target_total_max_s"])
        hard_cap = float(series["target_total_hard_cap_s"])
        if not minimum <= maximum <= hard_cap:
            raise PlanningError("series duration bounds must satisfy min <= max <= hard cap")
        if not 1 <= int(series["arc_size"]) <= 6:
            raise PlanningError("series_recap.arc_size must be between 1 and 6")


class PlanningService:
    def __init__(
        self,
        repo_root: Path,
        state_dir: Path,
        path_registry: PathRegistry,
        *,
        runner: DryRunRunner | None = None,
    ) -> None:
        self.repo_root = repo_root.expanduser().resolve()
        self.state_dir = state_dir.expanduser().resolve()
        self.path_registry = path_registry
        self.runner = runner or self._default_runner
        self.plans_dir = self.state_dir / "plans"

    def plan_single(self, request: SinglePlanRequest) -> ExecutionPlan:
        source = self.path_registry.resolve(request.source_token)
        run_dir = self._resolve_run_dir(request.run_dir_token, request.run_parent_token, request.run_name)
        display_title = request.display_title or _human_title(source.stem)
        config_path = self.path_registry.resolve(request.config_token)
        checks = [self._file_check(source, "source", "Source video")]
        checks.append(self._file_check(config_path, "config", "Config preset"))
        try:
            config = apply_safe_overrides(load_config(config_path), request.overrides, kind=JobKind.SINGLE)
            selected = stage_range(request.from_stage, request.to_stage, request.only)
        except (ConfigError, ValueError, PlanningError) as exc:
            raise PlanningError(str(exc)) from exc
        if not config.get("preflight", {}).get("enabled", True):
            selected.discard("preflight")
        if not config.get("visual_index", {}).get("enabled", False):
            selected.discard("visual_index")
        if not episode_planner_enabled(config) and request.only != "episode_planner":
            selected.discard("episode_planner")
        identity = {
            "kind": JobKind.SINGLE.value,
            "source": str(source),
            "run_dir": str(run_dir),
            "config": config,
            "from": request.from_stage,
            "to": request.to_stage,
            "only": request.only,
            "display_title": display_title,
        }
        plan_id = stable_hash(identity)[:24]
        snapshot_path = self._write_plan_config(plan_id, config)
        command = [
            sys.executable,
            str(self.repo_root / "run.py"),
            "--input",
            str(source),
            "--run-dir",
            str(run_dir),
            "--config",
            str(snapshot_path),
        ]
        if request.from_stage:
            command.extend(["--from", request.from_stage])
        if request.to_stage:
            command.extend(["--to", request.to_stage])
        if request.only:
            command.extend(["--only", request.only])
        dry_run = [*command, "--dry-run"]
        completed = self.runner(dry_run, self.repo_root)
        warnings = self._dry_run_warnings(completed)
        paths = build_single_paths(run_dir)
        dag = self._single_dag(completed.stdout, selected, paths)
        outputs = [str(path) for stage in STAGES if stage in selected for path in output_paths(paths, stage)]
        can_start = all(check.status != DeliveryStatus.BLOCK for check in checks) and completed.returncode == 0
        return self._persist_plan(
            ExecutionPlan(
                plan_id=plan_id,
                kind=JobKind.SINGLE,
                command=command,
                dry_run_command=dry_run,
                run_dir=str(run_dir),
                config_path=str(snapshot_path),
                config_snapshot=config,
                dag=dag,
                output_paths=outputs,
                checks=checks,
                warnings=warnings,
                dry_run_output=self._combined_output(completed),
                can_start=can_start,
                command_hash=self._command_hash(command),
                display_title=display_title,
            )
        )

    def plan_series(self, request: SeriesPlanRequest) -> ExecutionPlan:
        manifest_path = self.path_registry.resolve(request.manifest_token)
        run_dir = self._resolve_run_dir(request.run_dir_token, request.run_parent_token, request.run_name)
        config_path = self.path_registry.resolve(request.config_token)
        checks = [
            self._file_check(manifest_path, "manifest", "Series manifest"),
            self._file_check(config_path, "config", "Config preset"),
        ]
        try:
            config = apply_safe_overrides(load_config(config_path), request.overrides, kind=JobKind.SERIES)
            manifest, all_specs, duplicate_sources = _manifest_specs_for_planning(manifest_path)
            specs = select_episodes(all_specs, request.episodes)
        except (ConfigError, ValueError, PlanningError) as exc:
            raise PlanningError(str(exc)) from exc
        if not specs:
            raise PlanningError("no series episodes selected")
        display_title = request.display_title or manifest.series_title or _human_title(manifest.series_id)
        missing = [str(spec.source_path) for spec in specs if not spec.source_path.is_file()]
        selected_sources = [str(spec.source_path).casefold() for spec in specs]
        selected_duplicate_sources = sorted(source for source in set(selected_sources) if selected_sources.count(source) > 1)
        duplicate_count = len(selected_duplicate_sources)
        checks.append(
            PlanCheck(
                code="episode_sources",
                status=DeliveryStatus.BLOCK if missing else DeliveryStatus.PASS,
                message=f"{len(specs) - len(missing)}/{len(specs)} episode sources available",
                details={"missing": missing, "count": len(specs)},
            )
        )
        checks.append(
            PlanCheck(
                code="duplicate_sources",
                status=DeliveryStatus.BLOCK if duplicate_count else DeliveryStatus.PASS,
                message="Duplicate episode sources detected" if duplicate_count else "Episode sources are unique",
                details={"duplicate_count": duplicate_count, "sources": selected_duplicate_sources or duplicate_sources},
            )
        )
        identity = {
            "kind": JobKind.SERIES.value,
            "manifest": str(manifest_path),
            "episodes": [spec.episode_key for spec in specs],
            "run_dir": str(run_dir),
            "config": config,
            "display_title": display_title,
        }
        plan_id = stable_hash(identity)[:24]
        snapshot_path = self._write_plan_config(plan_id, config)
        command = [
            sys.executable,
            "-m",
            "series_recap",
            "--manifest",
            str(manifest_path),
            "--config",
            str(snapshot_path),
            "--run-dir",
            str(run_dir),
        ]
        if request.episodes:
            command.extend(["--episodes", request.episodes])
        dry_run = [*command, "--dry-run"]
        completed = self.runner(dry_run, self.repo_root)
        warnings = self._dry_run_warnings(completed)
        paths = build_series_paths(run_dir)
        enhanced = bool(config.get("postprocess", {}).get("enabled", False))
        dag = self._series_dag(
            completed.stdout,
            [spec.episode_key for spec in specs],
            postprocess_enabled=enhanced,
        )
        outputs = [
            str(paths.event_bank),
            str(paths.series_arc_plan),
            str(paths.series_composer_qa),
            str(paths.series_review_script),
            str(paths.series_tts_script),
            str(paths.series_chapters),
            str(paths.voiceover),
            str(paths.beats_timing),
            str(paths.edl),
            str(paths.source_map),
            *(
                [
                    str(paths.edit_plan),
                    str(paths.edit_plan_meta),
                    str(paths.edit_plan_qa),
                    str(paths.audio_attribution),
                ]
                if enhanced
                else []
            ),
            str(paths.output_video),
        ]
        can_start = all(check.status != DeliveryStatus.BLOCK for check in checks) and completed.returncode == 0
        return self._persist_plan(
            ExecutionPlan(
                plan_id=plan_id,
                kind=JobKind.SERIES,
                command=command,
                dry_run_command=dry_run,
                run_dir=str(run_dir),
                config_path=str(snapshot_path),
                config_snapshot=config,
                dag=dag,
                output_paths=outputs,
                checks=checks,
                warnings=warnings,
                dry_run_output=self._combined_output(completed),
                can_start=can_start,
                command_hash=self._command_hash(command),
                display_title=display_title,
            )
        )

    def inspect_single(self, source_token: str) -> SingleSourceInspection:
        source = self.path_registry.resolve(source_token, expect="file")
        duration_s: float | None = None
        try:
            duration_s = round(float(probe_duration(source)), 3)
        except (OSError, RuntimeError, ValueError):
            pass
        display_title = _human_title(source.stem)
        return SingleSourceInspection(
            source_token=source_token,
            source_name=source.name,
            display_title=display_title,
            suggested_run_name=_safe_slug(source.stem, fallback="single-run"),
            media_valid=duration_s is not None and duration_s > 0,
            duration_s=duration_s,
        )

    def inspect_series(self, manifest_token: str, episodes: str | None = None) -> SeriesSourceInspection:
        manifest_path = self.path_registry.resolve(manifest_token, expect="file")
        try:
            manifest, all_specs, _duplicate_sources = _manifest_specs_for_planning(manifest_path)
            specs = select_episodes(all_specs, episodes)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PlanningError(str(exc)) from exc
        if not specs:
            raise PlanningError("no series episodes selected")

        normalized_sources = [str(spec.source_path.resolve()).casefold() for spec in specs]
        duplicate_values = {source for source in normalized_sources if normalized_sources.count(source) > 1}
        missing_keys: list[str] = []
        duplicate_keys: list[str] = []
        inspected_episodes: list[SeriesEpisodeInspection] = []
        durations: list[float] = []
        all_durations_available = True
        for spec, normalized_source in zip(specs, normalized_sources, strict=True):
            available = spec.source_path.is_file()
            duplicate = normalized_source in duplicate_values
            if not available:
                missing_keys.append(spec.episode_key)
                all_durations_available = False
            else:
                try:
                    durations.append(float(probe_duration(spec.source_path)))
                except (OSError, RuntimeError, ValueError):
                    all_durations_available = False
            if duplicate:
                duplicate_keys.append(spec.episode_key)
            inspected_episodes.append(
                SeriesEpisodeInspection(
                    episode_key=spec.episode_key,
                    episode_number=spec.episode_number,
                    title=spec.title,
                    arc=spec.arc,
                    source_available=available,
                    source_name=spec.source_path.name,
                    source_duplicate=duplicate,
                )
            )

        display_title = manifest.series_title or _human_title(manifest.series_id)
        arc_preview = list(dict.fromkeys(spec.arc for spec in specs if spec.arc))
        return SeriesSourceInspection(
            manifest_token=manifest_token,
            manifest_name=manifest_path.name,
            series_id=manifest.series_id,
            display_title=display_title,
            suggested_run_name=_safe_slug(display_title, fallback=manifest.series_id),
            episodes=inspected_episodes,
            missing_source_count=len(missing_keys),
            missing_source_episode_keys=missing_keys,
            duplicate_source_count=len(duplicate_keys),
            duplicate_source_episode_keys=duplicate_keys,
            total_duration_s=round(sum(durations), 3) if all_durations_available else None,
            arc_preview=arc_preview,
        )

    def get_plan(self, plan_id: str) -> ExecutionPlan | None:
        if not plan_id or any(char not in "0123456789abcdef" for char in plan_id):
            return None
        path = self.plans_dir / f"{plan_id}.plan.json"
        if not path.is_file():
            return None
        return ExecutionPlan.model_validate_json(path.read_text(encoding="utf-8"))

    def _resolve_run_dir(
        self,
        run_dir_token: str | None,
        run_parent_token: str | None,
        run_name: str | None,
    ) -> Path:
        if run_dir_token:
            return self.path_registry.resolve(run_dir_token)
        if not run_parent_token or not run_name:
            raise PlanningError("run directory target is incomplete")
        parent = self.path_registry.resolve(run_parent_token, expect="directory")
        normalized = run_name.strip()
        stem = normalized.split(".", 1)[0].upper()
        if (
            not normalized
            or len(normalized) > 80
            or normalized in {".", ".."}
            or normalized.endswith((" ", "."))
            or _INVALID_RUN_NAME_RE.search(normalized)
            or stem in _WINDOWS_RESERVED_NAMES
        ):
            raise PlanningError("run_name is not a valid Windows directory name")
        target = (parent / normalized).resolve()
        self.path_registry.token_for(target)
        return target

    def _persist_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        atomic_write_json(
            self.plans_dir / f"{plan.plan_id}.plan.json",
            plan.model_dump(mode="json"),
        )
        return plan

    def _write_plan_config(self, plan_id: str, config: dict[str, Any]) -> Path:
        path = self.plans_dir / f"{plan_id}.config.json"
        atomic_write_json(path, config)
        return path

    @staticmethod
    def _default_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )

    @staticmethod
    def _file_check(path: Path, code: str, label: str) -> PlanCheck:
        exists = path.is_file()
        return PlanCheck(
            code=code,
            status=DeliveryStatus.PASS if exists else DeliveryStatus.BLOCK,
            message=f"{label} is available" if exists else f"{label} does not exist",
            details={"name": path.name},
        )

    @staticmethod
    def _command_hash(command: list[str]) -> str:
        return hashlib.sha256(_json_bytes(command)).hexdigest()

    @staticmethod
    def _combined_output(completed: subprocess.CompletedProcess[str]) -> str:
        return "\n".join(value.strip() for value in (completed.stdout, completed.stderr) if value and value.strip())

    @classmethod
    def _dry_run_warnings(cls, completed: subprocess.CompletedProcess[str]) -> list[str]:
        if completed.returncode == 0:
            return []
        output = cls._combined_output(completed)
        return [output[-2000:] or f"dry-run failed with exit code {completed.returncode}"]

    @staticmethod
    def _parse_statuses(output: str) -> dict[str, str]:
        statuses: dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped.startswith("[") or "] " not in stripped:
                continue
            status, key = stripped[1:].split("] ", 1)
            statuses[key] = status
        return statuses

    def _single_dag(self, output: str, selected: set[str], paths: Any) -> list[PlanDagNode]:
        statuses = self._parse_statuses(output)
        return [
            PlanDagNode(
                key=stage,
                label=stage.replace("_", " ").title(),
                status=statuses.get(stage, "planned"),
                outputs=[str(path) for path in output_paths(paths, stage)],
            )
            for stage in STAGES
            if stage in selected
        ]

    def _series_dag(
        self,
        output: str,
        episode_keys: list[str],
        *,
        postprocess_enabled: bool = False,
    ) -> list[PlanDagNode]:
        statuses = self._parse_statuses(output)
        nodes: list[PlanDagNode] = []
        for episode_key in episode_keys:
            for stage in ("episode_planner", "episode_shots"):
                key = f"{episode_key}:{stage}"
                nodes.append(
                    PlanDagNode(
                        key=key,
                        label=f"{episode_key} {stage.replace('_', ' ')}",
                        status=statuses.get(key, "planned"),
                    )
                )
        final_stages = ["series_composer", "tts", "youtube_chapters", "series_match"]
        if postprocess_enabled:
            final_stages.append("postprocess")
        final_stages.append("render")
        for stage in final_stages:
            nodes.append(
                PlanDagNode(
                    key=stage,
                    label=stage.replace("_", " ").title(),
                    status=statuses.get(stage, "planned"),
                )
            )
        return nodes


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _human_title(value: str) -> str:
    normalized = re.sub(r"[_-]+", " ", value).strip()
    return " ".join(word if any(character.isupper() for character in word[1:]) else word.capitalize() for word in normalized.split()) or "Untitled run"


def _safe_slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    if not slug:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", fallback.strip()).strip("-").lower()
    return slug[:80].rstrip("-") or "recap-run"


def _manifest_specs_for_planning(manifest_path: Path):  # type: ignore[no-untyped-def]
    manifest = load_series_manifest(manifest_path)
    raw_episodes = list(manifest.episodes)
    current = top_level_episode(manifest)
    if current is not None:
        raw_episodes.insert(0, current)
    if not raw_episodes:
        raise PlanningError("series manifest has no episodes")
    specs: list[EpisodeSpec] = []
    for episode in raw_episodes:
        source_path = resolve_source_path(episode.source_path, manifest_path)
        episode_key = default_episode_key(manifest, episode, source_path)
        specs.append(
            EpisodeSpec(
                episode_key=episode_key,
                episode_number=episode.episode_number if episode.episode_number is not None else numeric_episode(episode_key),
                title=episode.title,
                source_path=source_path,
                arc=episode.arc,
                spoiler_limit_episode=episode.spoiler_limit_episode,
            )
        )
    keys = [spec.episode_key for spec in specs]
    duplicate_keys = sorted(key for key in set(keys) if keys.count(key) > 1)
    if duplicate_keys:
        raise PlanningError(f"duplicate episode_key in manifest: {duplicate_keys}")
    sources = [str(spec.source_path).casefold() for spec in specs]
    duplicate_sources = sorted(source for source in set(sources) if sources.count(source) > 1)
    return manifest, specs, duplicate_sources
