from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

from common.media import extract_frame, run_command
from common.schema import EdlPlacement, validate_edl, write_json
from broll.schema import BrollManifestItem, BrollPlan, BrollQa, read_json

MOTION_PRESETS = ("zoom_in", "zoom_out", "pan_left", "pan_right")


def motion_preset(frame_id: str, duration_s: float | None = None) -> str:
    if duration_s is not None and 1.0 <= duration_s <= 1.5:
        return "still_soft_zoom"
    return MOTION_PRESETS[sum(frame_id.encode("utf-8")) % len(MOTION_PRESETS)]


def ken_burns_filter(*, width: int, height: int, fps: float, duration_s: float, preset: str) -> str:
    frames = max(1, round(duration_s * fps))
    base = f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,crop={width * 2}:{height * 2}"
    if preset == "still_soft_zoom":
        zoom = "z='min(1.025,1.0+0.025*on/{frames})'"
        x = "x='iw/2-(iw/zoom/2)'"
        y = "y='ih/2-(ih/zoom/2)'"
    elif preset == "zoom_out":
        zoom = "z='max(1.0,1.10-0.10*on/{frames})'"
        x = "x='iw/2-(iw/zoom/2)'"
        y = "y='ih/2-(ih/zoom/2)'"
    elif preset == "pan_left":
        zoom = "z='1.08'"
        x = "x='(iw-iw/zoom)*(1-on/{frames})'"
        y = "y='ih/2-(ih/zoom/2)'"
    elif preset == "pan_right":
        zoom = "z='1.08'"
        x = "x='(iw-iw/zoom)*on/{frames}'"
        y = "y='ih/2-(ih/zoom/2)'"
    else:
        zoom = "z='min(1.10,1.0+0.10*on/{frames})'"
        x = "x='iw/2-(iw/zoom/2)'"
        y = "y='ih/2-(ih/zoom/2)'"
    zoom = zoom.format(frames=frames)
    x = x.format(frames=frames)
    y = y.format(frames=frames)
    return f"{base},zoompan={zoom}:{x}:{y}:d={frames}:s={width}x{height}:fps={fps},trim=duration={duration_s:.6f},setpts=PTS-STARTPTS,format=yuv420p"


def render_ken_burns_clip(*, image_path: Path, output_path: Path, duration_s: float, width: int, height: int, fps: float, crf: int, preset_name: str, encoder_preset: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-t",
        f"{duration_s:.6f}",
        "-vf",
        ken_burns_filter(width=width, height=height, fps=fps, duration_s=duration_s, preset=preset_name),
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        encoder_preset,
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ])


def _placement_key(placement: EdlPlacement) -> tuple[int, float, float, int]:
    return (placement.beat_id, round(placement.tl_start, 3), round(placement.tl_end, 3), placement.shot_index)


def apply_broll_plan(
    *,
    edl_path: Path,
    plan_path: Path,
    film_path: Path,
    frame_dir: Path,
    clip_dir: Path,
    output_edl_path: Path,
    output_manifest_path: Path,
    output_qa_path: Path,
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    crf: int = 20,
    encoder_preset: str = "medium",
    force: bool = False,
) -> BrollQa:
    if not film_path.is_file():
        raise FileNotFoundError(f"film file does not exist: {film_path}")
    placements = [EdlPlacement.model_validate(item) for item in read_json(edl_path)]
    plan = BrollPlan.model_validate(read_json(plan_path))
    if force and clip_dir.exists():
        shutil.rmtree(clip_dir)
    if force and frame_dir.exists():
        shutil.rmtree(frame_dir)
    frame_dir.mkdir(parents=True, exist_ok=True)
    clip_dir.mkdir(parents=True, exist_ok=True)
    candidates = {(item.beat_id, round(item.tl_start, 3), round(item.tl_end, 3), item.shot_index): item for item in plan.candidates}
    manifest: list[BrollManifestItem] = []
    replaced = 0
    extracted = 0
    failed = 0
    fallback = 0
    output: list[EdlPlacement] = []
    warnings: list[str] = []
    for placement in placements:
        candidate = candidates.get(_placement_key(placement))
        if candidate is None:
            output.append(placement)
            continue
        preset_name = motion_preset(candidate.frame_id, candidate.duration_s)
        frame_path = (frame_dir / f"{candidate.frame_id}.jpg").resolve()
        clip_path = (clip_dir / f"{candidate.frame_id}.mp4").resolve()
        try:
            if force or not frame_path.is_file():
                extract_frame(film_path, candidate.frame_tc, frame_path)
            extracted += 1
            if force or not clip_path.is_file():
                render_ken_burns_clip(image_path=frame_path, output_path=clip_path, duration_s=candidate.duration_s, width=width, height=height, fps=fps, crf=crf, preset_name=preset_name, encoder_preset=encoder_preset)
        except Exception as exc:
            failed += 1
            fallback += 1
            warning = f"failed frame-from-film broll for {candidate.frame_id}: {exc}"
            warnings.append(warning)
            manifest.append(BrollManifestItem(frame_id=candidate.frame_id, frame_path=str(frame_path) if frame_path.is_file() else None, clip_path=None, source_tc=candidate.frame_tc, source_shot_index=candidate.frame_shot_index, status="failed", duration_s=candidate.duration_s, motion_preset=preset_name, warnings=[warning]))
            output.append(placement)
            continue
        replaced += 1
        manifest.append(BrollManifestItem(frame_id=candidate.frame_id, frame_path=str(frame_path), clip_path=str(clip_path), source_tc=candidate.frame_tc, source_shot_index=candidate.frame_shot_index, status="generated", duration_s=candidate.duration_s, motion_preset=preset_name))
        output.append(EdlPlacement(
            tl_start=placement.tl_start,
            tl_end=placement.tl_end,
            src=str(clip_path),
            src_in=0.0,
            src_out=round(placement.tl_end - placement.tl_start, 6),
            beat_id=placement.beat_id,
            shot_index=placement.shot_index,
            reused=placement.reused,
            speed=1.0,
        ))
    validate_edl(output)
    write_json(output_edl_path, output)
    write_json(output_manifest_path, [item.model_dump() for item in manifest])
    distance_distribution = Counter(str(candidate.frame_shot_distance_used) for candidate in plan.candidates)
    qa = BrollQa(
        enabled=True,
        source_edl=str(edl_path),
        output_edl=str(output_edl_path),
        n_placements=len(placements),
        n_planned=len(plan.candidates),
        n_replaced=replaced,
        n_skipped_short_duration=plan.n_skipped_short_duration,
        n_frame_keep_original_no_alternative=plan.n_frame_keep_original_no_alternative,
        frame_shot_distance_distribution=dict(sorted(distance_distribution.items())),
        n_extracted_frames=extracted,
        n_frame_fallbacks=fallback,
        n_failed_frames=failed,
        replacement_ratio=round(replaced / len(placements), 4) if placements else 0.0,
        original_footage_ratio_estimate=round(1.0 - (replaced / len(placements) if placements else 0.0), 4),
        warnings=warnings,
        manifest=manifest,
    )
    write_json(output_qa_path, qa)
    return qa
