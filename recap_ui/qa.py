from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from recap_ui.schemas import DeliveryStatus, JobKind, QaCheck, QaReport


MediaProbe = Callable[[Path], dict[str, Any]]


BLOCKING_COMPOSER_CODES = {
    "invalid_arc_json",
    "fallback_arc_failed",
    "deterministic_composer_fallback",
}


def build_delivery_qa(
    run_path: Path,
    kind: JobKind | str,
    config: dict[str, Any] | None = None,
    *,
    run_id: str | None = None,
    media_probe: MediaProbe | None = None,
) -> QaReport:
    resolved = run_path.expanduser().resolve()
    job_kind = kind if isinstance(kind, JobKind) else JobKind(kind)
    checks: list[QaCheck] = []
    metrics: dict[str, Any] = {}
    if job_kind == JobKind.SERIES:
        final_dir = resolved / "series_recap"
        checks.extend(_episode_timecode_checks(resolved, metrics))
        checks.extend(_composer_checks(final_dir, metrics))
        checks.extend(_series_timeline_checks(resolved, final_dir, metrics))
        output_video = final_dir / "series_recap.mp4"
        voiceover = final_dir / "voiceover.mp3"
        render_meta = final_dir / "render.meta.json"
        duration_targets = _series_duration_targets(final_dir, config)
        if duration_targets["minimum"] is not None:
            metrics["minimum_duration_s"] = duration_targets["minimum"]
        if duration_targets["maximum"] is not None:
            metrics["maximum_duration_s"] = duration_targets["maximum"]
        if duration_targets["hard_cap"] is not None:
            metrics["hard_cap_s"] = duration_targets["hard_cap"]
    else:
        final_dir = resolved
        checks.extend(_episode_timecode_checks(resolved, metrics, single=True))
        checks.extend(_single_timeline_checks(resolved, metrics))
        output_video = resolved / "recap.mp4"
        voiceover = resolved / "voiceover.mp3"
        render_meta = resolved / "render.meta.json"
        duration_targets = None
    checks.extend(
        _render_checks(
            output_video,
            voiceover,
            render_meta,
            metrics,
            duration_targets=duration_targets,
            media_probe=media_probe or probe_media,
        )
    )
    status = _aggregate_status(checks)
    return QaReport(
        run_id=run_id or _stable_id(resolved),
        kind=job_kind,
        status=status,
        checks=checks,
        metrics=metrics,
    )


def probe_media(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    duration = float(payload.get("format", {}).get("duration") or video.get("duration") or 0.0)
    rate = video.get("r_frame_rate") or video.get("avg_frame_rate") or "0/1"
    numerator, denominator = (rate.split("/", 1) + ["1"])[:2] if "/" in rate else (rate, "1")
    fps = float(numerator) / float(denominator) if float(denominator) else 0.0
    audio_durations = [float(item["duration"]) for item in audio if item.get("duration")]
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "duration": duration,
        "audio_streams": len(audio),
        "audio_duration": max(audio_durations) if audio_durations else duration if audio else 0.0,
    }


def _episode_timecode_checks(run_root: Path, metrics: dict[str, Any], *, single: bool = False) -> list[QaCheck]:
    directories = [run_root] if single else sorted(
        (item for item in run_root.iterdir() if item.is_dir() and (item / "film_map.meta.json").is_file()),
        key=lambda item: item.name,
    )
    if not directories:
        return [_check("episode_artifacts", "Episode artifacts", DeliveryStatus.BLOCK, "No episode film map metadata found")]
    translation_ratios: dict[str, float | None] = {}
    approximate: list[str] = []
    failed_translation: list[str] = []
    for directory in directories:
        meta = _read_json(directory / "film_map.meta.json")
        if not isinstance(meta, dict):
            approximate.append(directory.name)
            translation_ratios[directory.name] = None
            continue
        ratio = _optional_float(meta.get("translation_success_ratio"))
        required = bool(meta.get("translation_required", False))
        minimum = float(meta.get("translation_min_success_ratio") or 0.0)
        translation_ratios[directory.name] = ratio
        if required and (ratio is None or ratio < minimum):
            failed_translation.append(directory.name)
        if bool(meta.get("approximate_timecodes", False)):
            approximate.append(directory.name)
    metrics["translation_ratios"] = translation_ratios
    metrics["approximate_timecode_episodes"] = approximate
    translation_status = DeliveryStatus.BLOCK if failed_translation else DeliveryStatus.PASS
    translation_message = (
        f"Translation threshold failed for: {', '.join(failed_translation)}"
        if failed_translation
        else f"Translation thresholds pass for {len(directories)} episode(s)"
    )
    return [
        _check(
            "translation_ratio",
            "Translation coverage",
            translation_status,
            translation_message,
            "film_map.meta.json",
            failed=failed_translation,
        ),
        _check(
            "strict_timecodes",
            "Strict timecodes",
            DeliveryStatus.BLOCK if approximate else DeliveryStatus.PASS,
            f"Approximate timecodes found in: {', '.join(approximate)}" if approximate else "All episode timecodes are strict",
            "film_map.meta.json",
            episodes=approximate,
        ),
    ]


def _composer_checks(final_dir: Path, metrics: dict[str, Any]) -> list[QaCheck]:
    qa = _read_json(final_dir / "series_composer.qa.json")
    arc_plan = _read_json(final_dir / "series_arc_plan.json")
    chapters = _read_json(final_dir / "series_chapters.json")
    if not isinstance(qa, dict) or not isinstance(arc_plan, dict) or not isinstance(chapters, list):
        return [_check("composer_artifacts", "Composer artifacts", DeliveryStatus.BLOCK, "Composer artifacts are missing or invalid")]
    qa_report = qa.get("qa_report") or []
    blocking = [
        item for item in qa_report
        if isinstance(item, dict) and (item.get("level") == "error" or item.get("code") in BLOCKING_COMPOSER_CODES)
    ]
    warnings = [item for item in qa_report if isinstance(item, dict) and item.get("level") == "warning"]
    arcs = arc_plan.get("arcs") or []
    expected_keys = [key for arc in arcs if isinstance(arc, dict) for key in arc.get("episode_keys", [])]
    actual_keys = [item.get("episode_key") for item in chapters if isinstance(item, dict) and item.get("episode_key")]
    metrics.update(
        {
            "composer_estimated_duration_s": qa.get("estimated_duration_s"),
            "composer_event_count": qa.get("n_events"),
            "composer_prompt_count": qa.get("prompt_count"),
            "composer_revision_count": qa.get("revision_count"),
            "composer_arc_count": qa.get("arc_count"),
            "chapter_count": len(actual_keys),
        }
    )
    qa_status = DeliveryStatus.BLOCK if blocking else DeliveryStatus.WARN if warnings else DeliveryStatus.PASS
    return [
        _check(
            "composer_qa",
            "Composer QA",
            qa_status,
            f"{len(blocking)} blocking and {len(warnings)} warning composer issue(s)",
            "series_composer.qa.json",
            issues=qa_report,
        ),
        _check(
            "chapter_order",
            "Episode chapter order",
            DeliveryStatus.PASS if actual_keys == expected_keys and expected_keys else DeliveryStatus.BLOCK,
            f"{len(actual_keys)} ordered episode chapters" if actual_keys == expected_keys and expected_keys else "Chapter order does not match the arc plan",
            "series_chapters.json",
            expected=expected_keys,
            actual=actual_keys,
        ),
        _check(
            "arc_order",
            "Arc order",
            DeliveryStatus.PASS if len(set(expected_keys)) == len(expected_keys) and expected_keys else DeliveryStatus.BLOCK,
            f"{len(arcs)} arcs have a unique episode order" if len(set(expected_keys)) == len(expected_keys) and expected_keys else "Arc plan contains missing or repeated episodes",
            "series_arc_plan.json",
        ),
    ]


def _series_timeline_checks(run_root: Path, final_dir: Path, metrics: dict[str, Any]) -> list[QaCheck]:
    edl = _read_json(final_dir / "edl.json")
    timings = _read_json(final_dir / "beats_timing.json")
    edl_meta = _read_json(final_dir / "edl.meta.json")
    source_map = _read_json(final_dir / "edl.source_map.json")
    arc_plan = _read_json(final_dir / "series_arc_plan.json")
    checks = _timeline_checks(edl, timings, edl_meta, artifact_prefix="")
    expected_count = int(arc_plan.get("episode_count") or 0) if isinstance(arc_plan, dict) else 0
    sources = source_map.get("sources", {}) if isinstance(source_map, dict) else {}
    metrics["source_count"] = len(sources)
    checks.append(
        _check(
            "source_map",
            "Episode source map",
            DeliveryStatus.PASS if expected_count > 0 and len(sources) == expected_count else DeliveryStatus.BLOCK,
            f"Source map contains all {expected_count} episode sources" if expected_count > 0 and len(sources) == expected_count else f"Source map has {len(sources)} source(s), expected {expected_count}",
            "edl.source_map.json",
        )
    )
    invalid = _invalid_series_placements(run_root, edl if isinstance(edl, list) else [])
    checks.append(
        _check(
            "story_only_footage",
            "Story-only footage",
            DeliveryStatus.BLOCK if invalid else DeliveryStatus.PASS,
            f"{len(invalid)} placement(s) reference excluded shots" if invalid else "All placements reference usable story shots",
            "edl.json",
            invalid=invalid[:100],
        )
    )
    tts_meta = _read_json(final_dir / "tts_meta.json")
    render_meta = _read_json(final_dir / "render.meta.json")
    metrics["tts_duration_s"] = tts_meta.get("total_duration_s") if isinstance(tts_meta, dict) else None
    metrics["edl_duration_s"] = edl_meta.get("total_duration_s") if isinstance(edl_meta, dict) else None
    metrics["render_duration_s"] = render_meta.get("video_duration_s") if isinstance(render_meta, dict) else None
    return checks


def _single_timeline_checks(run_root: Path, metrics: dict[str, Any]) -> list[QaCheck]:
    edl = _read_json(run_root / "edl.json")
    timings = _read_json(run_root / "beats_timing.json")
    edl_meta = _read_json(run_root / "edl.meta.json")
    checks = _timeline_checks(edl, timings, edl_meta, artifact_prefix="")
    shots = _read_json(run_root / "shots.json")
    shot_lookup = {item.get("index"): item for item in shots if isinstance(item, dict)} if isinstance(shots, list) else {}
    invalid: list[dict[str, Any]] = []
    for index, placement in enumerate(edl if isinstance(edl, list) else []):
        if not isinstance(placement, dict):
            continue
        shot = shot_lookup.get(placement.get("shot_index"))
        if not _shot_allowed(shot):
            invalid.append({"placement": index, "shot_index": placement.get("shot_index")})
    checks.append(
        _check(
            "story_only_footage",
            "Story-only footage",
            DeliveryStatus.BLOCK if invalid else DeliveryStatus.PASS,
            f"{len(invalid)} placement(s) reference excluded shots" if invalid else "All placements reference usable story shots",
            "edl.json",
            invalid=invalid[:100],
        )
    )
    tts_meta = _read_json(run_root / "tts_meta.json")
    render_meta = _read_json(run_root / "render.meta.json")
    metrics["tts_duration_s"] = tts_meta.get("total_duration_s") if isinstance(tts_meta, dict) else None
    metrics["edl_duration_s"] = edl_meta.get("total_duration_s") if isinstance(edl_meta, dict) else None
    metrics["render_duration_s"] = render_meta.get("video_duration_s") if isinstance(render_meta, dict) else None
    return checks


def _timeline_checks(edl: Any, timings: Any, edl_meta: Any, *, artifact_prefix: str) -> list[QaCheck]:
    placements = edl if isinstance(edl, list) else []
    timing_items = timings if isinstance(timings, list) else []
    coverage_ok = bool(edl_meta.get("coverage_ok")) if isinstance(edl_meta, dict) else _timeline_is_tiled(placements, timing_items)
    timing_beats = {item.get("beat_id") for item in timing_items if isinstance(item, dict)}
    placement_beats = {item.get("beat_id") for item in placements if isinstance(item, dict)}
    missing_beats = sorted(value for value in timing_beats - placement_beats if isinstance(value, int))
    return [
        _check(
            "edl_coverage",
            "EDL timeline coverage",
            DeliveryStatus.PASS if coverage_ok else DeliveryStatus.BLOCK,
            "EDL covers the voiceover timeline" if coverage_ok else "EDL does not cover the complete voiceover timeline",
            artifact_prefix + "edl.json",
        ),
        _check(
            "voiceover_beat_coverage",
            "Voiceover beat coverage",
            DeliveryStatus.PASS if not missing_beats and timing_beats else DeliveryStatus.BLOCK,
            f"All {len(timing_beats)} voiceover beats have footage" if not missing_beats and timing_beats else f"Missing footage for {len(missing_beats)} beat(s)",
            artifact_prefix + "beats_timing.json",
            missing_beat_ids=missing_beats,
        ),
    ]


def _invalid_series_placements(run_root: Path, placements: list[Any]) -> list[dict[str, Any]]:
    lookups: dict[str, dict[int, dict[str, Any]]] = {}
    invalid: list[dict[str, Any]] = []
    for index, placement in enumerate(placements):
        if not isinstance(placement, dict):
            continue
        source = str(placement.get("src") or "").replace("\\", "/")
        episode_key = source.split("/", 1)[0]
        if episode_key not in lookups:
            shots = _read_json(run_root / episode_key / "shots.json")
            lookups[episode_key] = {
                item.get("index"): item for item in shots if isinstance(item, dict) and isinstance(item.get("index"), int)
            } if isinstance(shots, list) else {}
        shot = lookups[episode_key].get(placement.get("shot_index"))
        if not _shot_allowed(shot):
            invalid.append({"placement": index, "episode_key": episode_key, "shot_index": placement.get("shot_index")})
    return invalid


def _shot_allowed(shot: Any) -> bool:
    return bool(
        isinstance(shot, dict)
        and shot.get("is_story", True)
        and shot.get("is_usable", False)
        and not shot.get("is_end_credit", False)
    )


def _render_checks(
    output_video: Path,
    voiceover: Path,
    render_meta_path: Path,
    metrics: dict[str, Any],
    *,
    duration_targets: dict[str, float | None] | None,
    media_probe: MediaProbe,
) -> list[QaCheck]:
    if not output_video.is_file():
        return [_check("render_output", "Rendered output", DeliveryStatus.BLOCK, "Final video is missing")]
    try:
        media = media_probe(output_video)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return [_check("render_probe", "Rendered media probe", DeliveryStatus.BLOCK, f"ffprobe failed: {exc}")]
    meta = _read_json(render_meta_path)
    voiceover_meta = None
    try:
        voiceover_meta = media_probe(voiceover) if voiceover.is_file() else None
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        voiceover_meta = None
    width = int(media.get("width") or 0)
    height = int(media.get("height") or 0)
    fps = float(media.get("fps") or 0.0)
    duration = float(media.get("duration") or 0.0)
    audio_streams = int(media.get("audio_streams") or 0)
    audio_duration = float(media.get("audio_duration") or 0.0)
    voiceover_duration = float((voiceover_meta or {}).get("duration") or 0.0)
    if not voiceover_duration and isinstance(meta, dict):
        voiceover_duration = float(meta.get("audio_duration_s") or 0.0)
    metrics.update(
        {
            "width": width,
            "height": height,
            "fps": fps,
            "render_duration_s": duration,
            "audio_streams": audio_streams,
            "audio_duration_s": audio_duration,
            "voiceover_duration_s": voiceover_duration,
        }
    )
    duration_delta = abs(audio_duration - voiceover_duration) if voiceover_duration else None
    checks = [
        _check(
            "render_format",
            "Render format",
            DeliveryStatus.PASS if width == 1920 and height == 1080 and abs(fps - 30.0) <= 0.01 else DeliveryStatus.BLOCK,
            f"{width}x{height} at {fps:.3f} fps",
            output_video.name,
        ),
        _check(
            "voiceover_stream",
            "Voiceover-only audio stream",
            DeliveryStatus.PASS if audio_streams == 1 else DeliveryStatus.BLOCK,
            "Output contains one voiceover audio stream" if audio_streams == 1 else f"Output contains {audio_streams} audio streams",
            output_video.name,
        ),
        _check(
            "voiceover_duration",
            "Voiceover duration match",
            DeliveryStatus.PASS if duration_delta is not None and duration_delta <= 0.25 else DeliveryStatus.BLOCK,
            f"Audio/voiceover duration delta is {duration_delta:.3f}s" if duration_delta is not None else "Voiceover duration could not be verified",
            output_video.name,
            delta_s=duration_delta,
        ),
        _check(
            "render_duration_match",
            "Video/audio duration match",
            DeliveryStatus.PASS if abs(duration - audio_duration) <= 0.25 else DeliveryStatus.BLOCK,
            f"Video/audio duration delta is {abs(duration - audio_duration):.3f}s",
            output_video.name,
        ),
    ]
    if duration_targets is not None:
        minimum = duration_targets["minimum"]
        maximum = duration_targets["maximum"]
        hard_cap = duration_targets["hard_cap"]
        if minimum is not None:
            below_minimum = duration + 0.05 < minimum
            checks.append(
                _check(
                    "minimum_duration",
                    "Minimum final duration",
                    DeliveryStatus.BLOCK if below_minimum else DeliveryStatus.PASS,
                    (
                        f"Final duration {duration:.3f}s is below the required minimum {minimum:.3f}s"
                        if below_minimum
                        else f"Final duration {duration:.3f}s meets the minimum {minimum:.3f}s"
                    ),
                    output_video.name,
                )
            )
        if maximum is not None:
            checks.append(
                _check(
                    "maximum_duration",
                    "Target maximum duration",
                    DeliveryStatus.PASS,
                    (
                        f"Final duration {duration:.3f}s exceeds target maximum {maximum:.3f}s; overage is accepted"
                        if duration > maximum + 0.05
                        else f"Final duration {duration:.3f}s is within target maximum {maximum:.3f}s"
                    ),
                    output_video.name,
                )
            )
        if hard_cap is not None:
            checks.append(
                _check(
                    "hard_cap",
                    "Configured hard cap",
                    DeliveryStatus.PASS,
                    (
                        f"Final duration {duration:.3f}s exceeds configured hard cap {hard_cap:.3f}s; overage is accepted by delivery policy"
                        if duration > hard_cap + 0.05
                        else f"Final duration {duration:.3f}s is within configured hard cap {hard_cap:.3f}s"
                    ),
                    output_video.name,
                )
            )
    return checks


def _series_duration_targets(final_dir: Path, config: dict[str, Any] | None) -> dict[str, float | None]:
    config_section = config.get("series_recap", {}) if config else {}
    qa = _read_json(final_dir / "series_composer.qa.json")
    plan = _read_json(final_dir / "series_arc_plan.json")
    qa_section = qa if isinstance(qa, dict) else {}
    plan_section = plan if isinstance(plan, dict) else {}
    return {
        "minimum": _first_float(config_section, qa_section, plan_section, key="target_total_min_s"),
        "maximum": _first_float(config_section, qa_section, plan_section, key="target_total_max_s", aliases=("target_video_s",)),
        "hard_cap": _first_float(config_section, qa_section, plan_section, key="target_total_hard_cap_s"),
    }


def _first_float(*sources: dict[str, Any], key: str, aliases: tuple[str, ...] = ()) -> float | None:
    for source in sources:
        for candidate in (key, *aliases):
            value = source.get(candidate)
            if value is not None:
                return float(value)
    return None


def _timeline_is_tiled(placements: list[Any], timings: list[Any]) -> bool:
    if not placements or not timings:
        return False
    cursor = 0.0
    for placement in placements:
        if not isinstance(placement, dict):
            return False
        start = float(placement.get("tl_start") or 0.0)
        end = float(placement.get("tl_end") or 0.0)
        if abs(start - cursor) > 0.02 or end <= start:
            return False
        cursor = end
    total = max(float(item.get("tl_end") or 0.0) for item in timings if isinstance(item, dict))
    return abs(cursor - total) <= 0.02


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _aggregate_status(checks: list[QaCheck]) -> DeliveryStatus:
    if any(check.status == DeliveryStatus.BLOCK for check in checks):
        return DeliveryStatus.BLOCK
    if any(check.status in {DeliveryStatus.WARN, DeliveryStatus.UNKNOWN} for check in checks):
        return DeliveryStatus.WARN
    return DeliveryStatus.PASS


def _check(
    code: str,
    label: str,
    status: DeliveryStatus,
    message: str,
    artifact: str | None = None,
    **details: Any,
) -> QaCheck:
    return QaCheck(code=code, label=label, status=status, message=message, artifact=artifact, details=details)


def _stable_id(path: Path) -> str:
    import hashlib

    return hashlib.sha256(str(path).casefold().encode("utf-8")).hexdigest()[:24]
