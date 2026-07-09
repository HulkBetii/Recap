from __future__ import annotations

import shutil
from pathlib import Path

from common.media import run_command
from common.schema import EdlPlacement, validate_edl, write_json
from broll.schema import BrollManifestItem, BrollPlan, BrollQa, read_json

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
MOTION_PRESETS = ("zoom_in", "zoom_out", "pan_left", "pan_right")


def find_asset(asset_dir: Path, asset_id: str) -> Path | None:
    for extension in IMAGE_EXTENSIONS:
        path = asset_dir / f"{asset_id}{extension}"
        if path.is_file():
            return path
    return None


def motion_preset(asset_id: str) -> str:
    return MOTION_PRESETS[sum(asset_id.encode("utf-8")) % len(MOTION_PRESETS)]


def ken_burns_filter(*, width: int, height: int, fps: float, duration_s: float, preset: str) -> str:
    frames = max(1, round(duration_s * fps))
    base = f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,crop={width * 2}:{height * 2}"
    if preset == "zoom_out":
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
    asset_dir: Path,
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
    placements = [EdlPlacement.model_validate(item) for item in read_json(edl_path)]
    plan = BrollPlan.model_validate(read_json(plan_path))
    if force and clip_dir.exists():
        shutil.rmtree(clip_dir)
    clip_dir.mkdir(parents=True, exist_ok=True)
    candidates = {(item.beat_id, round(item.tl_start, 3), round(item.tl_end, 3), item.shot_index): item for item in plan.candidates}
    manifest: list[BrollManifestItem] = []
    replaced = 0
    missing = 0
    output: list[EdlPlacement] = []
    warnings: list[str] = []
    for placement in placements:
        candidate = candidates.get(_placement_key(placement))
        if candidate is None:
            output.append(placement)
            continue
        asset = find_asset(asset_dir, candidate.asset_id)
        preset_name = motion_preset(candidate.asset_id)
        if asset is None:
            missing += 1
            warning = f"missing asset for {candidate.asset_id}"
            warnings.append(warning)
            manifest.append(BrollManifestItem(asset_id=candidate.asset_id, image_path=None, clip_path=None, status="missing_asset", duration_s=candidate.duration_s, motion_preset=preset_name, warnings=[warning]))
            output.append(placement)
            continue
        clip_path = (clip_dir / f"{candidate.asset_id}.mp4").resolve()
        if force or not clip_path.is_file():
            render_ken_burns_clip(image_path=asset, output_path=clip_path, duration_s=candidate.duration_s, width=width, height=height, fps=fps, crf=crf, preset_name=preset_name, encoder_preset=encoder_preset)
        replaced += 1
        manifest.append(BrollManifestItem(asset_id=candidate.asset_id, image_path=str(asset), clip_path=str(clip_path), status="generated", duration_s=candidate.duration_s, motion_preset=preset_name))
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
    qa = BrollQa(
        enabled=True,
        source_edl=str(edl_path),
        output_edl=str(output_edl_path),
        n_placements=len(placements),
        n_planned=len(plan.candidates),
        n_replaced=replaced,
        n_missing_assets=missing,
        replacement_ratio=round(replaced / len(placements), 4) if placements else 0.0,
        original_footage_ratio_estimate=round(1.0 - (replaced / len(placements) if placements else 0.0), 4),
        warnings=warnings,
        manifest=manifest,
    )
    write_json(output_qa_path, qa)
    return qa

