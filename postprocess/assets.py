from __future__ import annotations

import json
import math
from pathlib import Path

import yaml
from pydantic import ValidationError

from common.media import MediaError, probe_audio_stream_count, probe_duration
from common.schema import AudioAsset, AudioAssetManifest


class AudioAssetError(ValueError):
    pass


def load_audio_manifest(path: Path) -> tuple[AudioAssetManifest, Path]:
    manifest_path = path.expanduser().resolve()
    if not manifest_path.is_file():
        raise AudioAssetError(f"audio asset manifest does not exist: {manifest_path}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AudioAssetError(f"cannot read audio asset manifest: {manifest_path}") from exc
    if not isinstance(raw, dict):
        raise AudioAssetError("audio asset manifest root must be an object")
    try:
        manifest = AudioAssetManifest.model_validate(raw)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise AudioAssetError(f"invalid audio asset manifest: {exc}") from exc
    root = manifest_path.parent
    if manifest.base_dir:
        root = (root / manifest.base_dir).resolve()
    return manifest, root


def resolve_asset_path(asset: AudioAsset, root: Path) -> Path:
    root = root.expanduser().resolve()
    resolved = (root / asset.path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AudioAssetError(f"audio asset escapes its library root: {asset.asset_id}") from exc
    return resolved


def validate_audio_assets(path: Path, *, check_files: bool = True) -> AudioAssetManifest:
    manifest, root = load_audio_manifest(path)
    default_music = [
        asset for asset in manifest.assets if asset.kind == "music" and asset.mood == "default" and asset.loopable
    ]
    whoosh = [asset for asset in manifest.assets if asset.kind == "sfx" and asset.sfx_kind == "whoosh"]
    impact = [asset for asset in manifest.assets if asset.kind == "sfx" and asset.sfx_kind == "impact"]
    if not default_music:
        raise AudioAssetError("audio asset manifest requires loopable music with mood=default")
    if not whoosh:
        raise AudioAssetError("audio asset manifest requires at least one whoosh SFX")
    if not impact:
        raise AudioAssetError("audio asset manifest requires at least one impact SFX")
    for asset in manifest.assets:
        if not asset.license_source.strip():
            raise AudioAssetError(f"audio asset {asset.asset_id} requires license_source")
        if not check_files:
            continue
        asset_path = resolve_asset_path(asset, root)
        if not asset_path.is_file():
            raise AudioAssetError(f"audio asset file does not exist: {asset_path}")
        try:
            stream_count = probe_audio_stream_count(asset_path)
            duration_s = probe_duration(asset_path)
        except MediaError as exc:
            raise AudioAssetError(f"audio asset {asset.asset_id} is not decodable: {exc}") from exc
        if stream_count != 1:
            raise AudioAssetError(
                f"audio asset {asset.asset_id} must contain exactly one audio stream; found {stream_count}"
            )
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise AudioAssetError(f"audio asset {asset.asset_id} must have positive finite duration")
    return manifest
