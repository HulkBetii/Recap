from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from common.media import run_command
from common.schema import AudioAsset, AudioAssetManifest, EditPlan, MusicCue, SfxCue
from render.cache import RenderCache, file_identity, stable_hash


@dataclass(frozen=True)
class ResolvedAudioAsset:
    spec: AudioAsset
    path: Path


@dataclass(frozen=True)
class EnhancedAudioResult:
    master_path: Path
    duration_s: float


def resolve_audio_assets(manifest: AudioAssetManifest, manifest_path: Path) -> dict[str, ResolvedAudioAsset]:
    base_dir = manifest_path.parent
    if manifest.base_dir is not None:
        base_dir = base_dir / manifest.base_dir
    resolved: dict[str, ResolvedAudioAsset] = {}
    for asset in manifest.assets:
        path = (base_dir / asset.path).resolve()
        if not path.is_file():
            raise ValueError(f"audio asset file does not exist: {asset.asset_id} -> {path}")
        resolved[asset.asset_id] = ResolvedAudioAsset(spec=asset, path=path)
    return resolved


def validate_audio_cues(plan: EditPlan, assets: dict[str, ResolvedAudioAsset]) -> None:
    if not plan.music_cues:
        raise ValueError("enhanced render requires at least one music cue")
    for cue in plan.music_cues:
        asset = assets.get(cue.asset_id)
        if asset is None:
            raise ValueError(f"music cue references unknown asset: {cue.asset_id}")
        if asset.spec.kind != "music":
            raise ValueError(f"music cue references non-music asset: {cue.asset_id}")
        if not asset.spec.loopable:
            raise ValueError(f"music cue requires a loopable asset: {cue.asset_id}")
    for cue in plan.sfx_cues:
        asset = assets.get(cue.asset_id)
        if asset is None:
            raise ValueError(f"SFX cue references unknown asset: {cue.asset_id}")
        if asset.spec.kind != "sfx" or asset.spec.sfx_kind != cue.kind:
            raise ValueError(f"SFX cue kind does not match asset: {cue.asset_id}")


def _atomic_audio_path(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.stem}.{uuid4().hex}.partial{output_path.suffix}")


def _finish_atomic_audio(partial_path: Path, output_path: Path) -> None:
    partial_path.replace(output_path)


def _music_identity(plan: EditPlan, assets: dict[str, ResolvedAudioAsset], duration_s: float) -> str:
    used_ids = sorted({cue.asset_id for cue in plan.music_cues})
    return stable_hash(
        {
            "kind": "music-v1",
            "duration_s": round(duration_s, 6),
            "cues": [cue.model_dump(mode="json") for cue in plan.music_cues],
            "music_lufs": plan.audio_mix.music_lufs,
            "assets": {asset_id: file_identity(assets[asset_id].path) for asset_id in used_ids},
        }
    )


def _sfx_identity(plan: EditPlan, assets: dict[str, ResolvedAudioAsset], duration_s: float) -> str:
    used_ids = sorted({cue.asset_id for cue in plan.sfx_cues})
    return stable_hash(
        {
            "kind": "sfx-v1",
            "duration_s": round(duration_s, 6),
            "cues": [cue.model_dump(mode="json") for cue in plan.sfx_cues],
            "assets": {asset_id: file_identity(assets[asset_id].path) for asset_id in used_ids},
        }
    )


def _master_identity(
    *,
    voiceover_path: Path,
    music_path: Path,
    sfx_path: Path,
    plan: EditPlan,
    duration_s: float,
    audio_delay_s: float,
) -> str:
    return stable_hash(
        {
            "kind": "master-v1",
            "voiceover": file_identity(voiceover_path),
            "music": file_identity(music_path),
            "sfx": file_identity(sfx_path),
            "mix": plan.audio_mix.model_dump(mode="json"),
            "duration_s": round(duration_s, 6),
            "audio_delay_s": round(audio_delay_s, 6),
        }
    )


def _music_window(cues: list[MusicCue], index: int, duration_s: float) -> tuple[float, float, float, float]:
    cue = cues[index]
    fade_in = min(cue.crossfade_s, cue.tl_end - cue.tl_start) if index > 0 else 0.0
    fade_out = min(cues[index + 1].crossfade_s, cue.tl_end - cue.tl_start) if index + 1 < len(cues) else 0.0
    start = max(0.0, cue.tl_start - fade_in / 2)
    end = min(duration_s, cue.tl_end + fade_out / 2)
    return start, end, fade_in, fade_out


def build_music_bed(
    *,
    plan: EditPlan,
    assets: dict[str, ResolvedAudioAsset],
    output_path: Path,
    duration_s: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y"]
    for cue in plan.music_cues:
        asset = assets[cue.asset_id]
        if asset.spec.loopable:
            command += ["-stream_loop", "-1"]
        command += ["-i", str(asset.path)]
    filters: list[str] = []
    labels: list[str] = []
    for index, cue in enumerate(plan.music_cues):
        start, end, fade_in, fade_out = _music_window(plan.music_cues, index, duration_s)
        cue_duration = end - start
        relative_gain_db = cue.gain_db - plan.audio_mix.music_lufs
        chain = (
            f"[{index}:a]atrim=duration={cue_duration:.6f},asetpts=PTS-STARTPTS,"
            "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"loudnorm=I={plan.audio_mix.music_lufs:g}:TP=-2:LRA=11,"
            f"volume={relative_gain_db:.6f}dB"
        )
        if fade_in > 0:
            chain += f",afade=t=in:st=0:d={fade_in:.6f}"
        if fade_out > 0:
            chain += f",afade=t=out:st={max(0.0, cue_duration - fade_out):.6f}:d={fade_out:.6f}"
        chain += f",adelay={round(start * 1000)}:all=1[music{index}]"
        filters.append(chain)
        labels.append(f"[music{index}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,"
        f"apad,atrim=duration={duration_s:.6f}[music]"
    )
    partial_path = _atomic_audio_path(output_path)
    try:
        command += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[music]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(partial_path),
        ]
        run_command(command)
        _finish_atomic_audio(partial_path, output_path)
    finally:
        partial_path.unlink(missing_ok=True)


def build_sfx_bed(
    *,
    cues: list[SfxCue],
    assets: dict[str, ResolvedAudioAsset],
    output_path: Path,
    duration_s: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = _atomic_audio_path(output_path)
    try:
        if not cues:
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=48000:cl=stereo",
                    "-t",
                    f"{duration_s:.6f}",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(partial_path),
                ]
            )
        else:
            command = ["ffmpeg", "-y"]
            for cue in cues:
                command += ["-i", str(assets[cue.asset_id].path)]
            filters: list[str] = []
            labels: list[str] = []
            for index, cue in enumerate(cues):
                remaining = max(0.05, duration_s - cue.tl_start)
                filters.append(
                    f"[{index}:a]atrim=duration={remaining:.6f},asetpts=PTS-STARTPTS,"
                    "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"volume={cue.gain_db:.6f}dB,adelay={round(cue.tl_start * 1000)}:all=1[sfx{index}]"
                )
                labels.append(f"[sfx{index}]")
            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,"
                f"apad,atrim=duration={duration_s:.6f}[sfx]"
            )
            command += [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[sfx]",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(partial_path),
            ]
            run_command(command)
        _finish_atomic_audio(partial_path, output_path)
    finally:
        partial_path.unlink(missing_ok=True)


def build_master_mix(
    *,
    voiceover_path: Path,
    music_path: Path,
    sfx_path: Path,
    output_path: Path,
    plan: EditPlan,
    duration_s: float,
    audio_delay_s: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    delay_ms = round(audio_delay_s * 1000)
    ratio = max(2.0, 1.0 + plan.audio_mix.duck_db)
    limiter = min(1.0, math.pow(10.0, plan.audio_mix.true_peak_db / 20.0))
    voice_delay = f"adelay={delay_ms}:all=1," if delay_ms > 0 else ""
    filter_text = (
        f"[0:a]{voice_delay}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"apad,atrim=duration={duration_s:.6f},asplit=2[voice][sidechain];"
        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[music];"
        f"[music][sidechain]sidechaincompress=threshold=0.02:ratio={ratio:.6f}:"
        "attack=20:release=400[ducked];"
        "[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[sfx];"
        "[voice][ducked][sfx]amix=inputs=3:duration=longest:normalize=0,"
        f"loudnorm=I={plan.audio_mix.master_lufs:g}:TP={plan.audio_mix.true_peak_db:g}:"
        f"LRA={plan.audio_mix.loudness_range:g},alimiter=limit={limiter:.6f},"
        f"atrim=duration={duration_s:.6f}[master]"
    )
    partial_path = _atomic_audio_path(output_path)
    try:
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(voiceover_path),
                "-i",
                str(music_path),
                "-i",
                str(sfx_path),
                "-filter_complex",
                filter_text,
                "-map",
                "[master]",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(partial_path),
            ]
        )
        _finish_atomic_audio(partial_path, output_path)
    finally:
        partial_path.unlink(missing_ok=True)


def render_enhanced_audio(
    *,
    voiceover_path: Path,
    plan: EditPlan,
    manifest: AudioAssetManifest,
    manifest_path: Path,
    cache: RenderCache,
    duration_s: float,
    audio_delay_s: float,
) -> EnhancedAudioResult:
    assets = resolve_audio_assets(manifest, manifest_path)
    validate_audio_cues(plan, assets)
    music_key = _music_identity(plan, assets, duration_s)
    music_path = cache.get_cached_audio("music", music_key) or cache.audio_path("music", music_key)
    if not music_path.is_file():
        build_music_bed(plan=plan, assets=assets, output_path=music_path, duration_s=duration_s)
    sfx_key = _sfx_identity(plan, assets, duration_s)
    sfx_path = cache.get_cached_audio("sfx", sfx_key) or cache.audio_path("sfx", sfx_key)
    if not sfx_path.is_file():
        build_sfx_bed(cues=plan.sfx_cues, assets=assets, output_path=sfx_path, duration_s=duration_s)
    master_key = _master_identity(
        voiceover_path=voiceover_path,
        music_path=music_path,
        sfx_path=sfx_path,
        plan=plan,
        duration_s=duration_s,
        audio_delay_s=audio_delay_s,
    )
    master_path = cache.get_cached_audio("master", master_key) or cache.audio_path("master", master_key)
    if not master_path.is_file():
        build_master_mix(
            voiceover_path=voiceover_path,
            music_path=music_path,
            sfx_path=sfx_path,
            output_path=master_path,
            plan=plan,
            duration_s=duration_s,
            audio_delay_s=audio_delay_s,
        )
    return EnhancedAudioResult(master_path=master_path, duration_s=duration_s)
