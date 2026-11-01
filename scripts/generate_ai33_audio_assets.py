from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from common.media import MediaError, probe_audio_stream_count, probe_duration, require_ffmpeg, run_command
from common.schema import AudioAsset, AudioAssetManifest


AI33_BASE_URL = "https://api.ai33.pro"
REPO_ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = (REPO_ROOT / "work").resolve()
DEFAULT_SPEC = REPO_ROOT / "examples" / "audio" / "ai33_anime_cinematic_pack.yaml"
DEFAULT_OUTPUT_DIR = Path("work/ai33-audio-library")
LICENSE_SOURCE = "AI33/Suno generated test asset; commercial rights not verified"
ATTRIBUTION = "Generated through AI33 for technical testing; commercial rights have not been verified."
RUNNING_STATUSES = {"pending", "queued", "processing", "running", "in_progress", "doing"}
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
LOOP_CROSSFADE_S = 1.5
LOOP_SEAM_MAX_DISCONTINUITY_RATIO = 3.0
SFX_CREDITS_PER_SECOND = 50
ASSET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_DOWNLOAD_REDIRECTS = 5


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _open_without_redirect(request: urllib.request.Request, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


class AudioGenerationError(RuntimeError):
    pass


class TransientAi33PollError(AudioGenerationError):
    pass


def redact_secrets(value: str, *secrets: str) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


class MusicSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    mood: Literal["default", "tension", "calm", "mystery", "suspense", "action", "emotional"]
    prompt: str = Field(min_length=1, max_length=500)

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        normalized = value.strip()
        if not ASSET_ID_RE.fullmatch(normalized):
            raise ValueError("asset_id must contain only lowercase letters, digits, hyphens, and underscores")
        return normalized

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()


class SfxSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    sfx_kind: Literal["whoosh", "impact"]
    prompt: str = Field(min_length=1, max_length=500)
    duration_s: float = Field(default=1.0, ge=1.0, le=5.0)
    prompt_influence: float = Field(default=0.3, ge=0, le=1)

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        normalized = value.strip()
        if not ASSET_ID_RE.fullmatch(normalized):
            raise ValueError("asset_id must contain only lowercase letters, digits, hyphens, and underscores")
        return normalized

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()


class AudioPackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    music: list[MusicSpec]
    sfx: list[SfxSpec]

    @model_validator(mode="after")
    def validate_pack(self) -> "AudioPackSpec":
        ids = [item.asset_id for item in [*self.music, *self.sfx]]
        if len(ids) != len(set(ids)):
            raise ValueError("audio pack asset IDs must be unique")
        if not any(item.mood == "default" for item in self.music):
            raise ValueError("audio pack requires default music")
        if not any(item.sfx_kind == "whoosh" for item in self.sfx):
            raise ValueError("audio pack requires a whoosh")
        if not any(item.sfx_kind == "impact" for item in self.sfx):
            raise ValueError("audio pack requires an impact")
        return self


AssetSpec = MusicSpec | SfxSpec


def load_spec(path: Path) -> AudioPackSpec:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AudioGenerationError(f"cannot read audio pack spec: {path}") from exc
    try:
        return AudioPackSpec.model_validate(payload)
    except ValidationError as exc:
        raise AudioGenerationError(f"invalid audio pack spec: {exc}") from exc


def ordered_assets(spec: AudioPackSpec) -> list[AssetSpec]:
    default_music = next(item for item in spec.music if item.mood == "default")
    first_whoosh = next(item for item in spec.sfx if item.sfx_kind == "whoosh")
    first_impact = next(item for item in spec.sfx if item.sfx_kind == "impact")
    remaining_music = [item for item in spec.music if item.asset_id != default_music.asset_id]
    leading_sfx = {first_whoosh.asset_id, first_impact.asset_id}
    remaining_sfx = [item for item in spec.sfx if item.asset_id not in leading_sfx]
    return [default_music, first_whoosh, first_impact, *remaining_music, *remaining_sfx]


class Ai33Client:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = AI33_BASE_URL,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        opener: Callable[..., Any] = _open_without_redirect,
    ) -> None:
        if not api_key.strip():
            raise AudioGenerationError("VIVOO_API_KEY env var is required")
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._jitter = jitter
        self._opener = opener

    def _request_json(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"User-Agent": "Mozilla/5.0", "xi-api-key": self._api_key, "Content-Type": "application/json"}
        for attempt in range(self.max_attempts):
            request = urllib.request.Request(f"{self.base_url}{path}", method=method, headers=headers, data=data)
            try:
                with self._opener(request, timeout=60) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise AudioGenerationError("AI33 returned a non-object JSON response")
                return payload
            except urllib.error.HTTPError as exc:
                raw_body = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code == 429 or (
                    exc.code == 503 and (method == "GET" or _error_code(raw_body) == "server_busy")
                )
                if not retryable or attempt == self.max_attempts - 1:
                    message = redact_secrets(_safe_error_message(raw_body), self._api_key)
                    if retryable and method == "GET":
                        raise TransientAi33PollError(f"AI33 polling HTTP {exc.code}: {message}") from exc
                    raise AudioGenerationError(f"AI33 HTTP {exc.code}: {message}") from exc
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
                delay = max(retry_after, float(2**attempt)) + self._jitter() * 0.25
                self._sleep(delay)
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                if method != "GET":
                    raise AudioGenerationError(
                        "AI33 create request outcome is unknown after a network error; refusing to resubmit"
                    ) from exc
                if attempt == self.max_attempts - 1:
                    raise TransientAi33PollError("AI33 polling network retries were exhausted") from exc
                self._sleep(float(2**attempt) + self._jitter() * 0.25)
            except json.JSONDecodeError as exc:
                raise AudioGenerationError("AI33 returned invalid JSON") from exc
        raise AssertionError("AI33 retry loop exhausted")

    def submit(self, asset: AssetSpec) -> dict[str, Any]:
        if isinstance(asset, MusicSpec):
            path = "/v1s/task/music-generation"
            body: dict[str, Any] = {
                "create_mode": "simple",
                "gpt_description_prompt": asset.prompt,
                "make_instrumental": True,
            }
        else:
            path = "/v1/task/sound-effect"
            body = {
                "text": asset.prompt,
                "duration_seconds": asset.duration_s,
                "prompt_influence": asset.prompt_influence,
                "loop": False,
                "model_id": "eleven_text_to_sound_v2",
            }
        payload = self._request_json(path, method="POST", body=body)
        task_id = payload.get("task_id") or payload.get("id")
        if not task_id:
            raise AudioGenerationError("AI33 create response has no task_id/id")
        return {"task_id": str(task_id), "response": payload, "endpoint": path}

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request_json(f"/v1/task/{urllib.parse.quote(task_id, safe='')}")

    def redact(self, value: str) -> str:
        return redact_secrets(value, self._api_key)


def _error_code(raw_body: str) -> str | None:
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return str(payload.get("code")) if payload.get("code") else None


def _safe_error_message(raw_body: str) -> str:
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return "request failed"
    if not isinstance(payload, dict):
        return "request failed"
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "request failed")
    return str(payload.get("message") or payload.get("code") or "request failed")


def _retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 0.0


async def poll_task(client: Ai33Client, task_id: str, *, timeout_s: float = 1800, interval_s: float = 5.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            payload = await asyncio.to_thread(client.get_task, task_id)
        except TransientAi33PollError:
            await asyncio.sleep(interval_s)
            continue
        status = str(payload.get("status") or "").lower()
        if status == "done":
            return payload
        if status not in RUNNING_STATUSES:
            message = payload.get("error_message") or payload.get("error") or status or "unknown status"
            raise AudioGenerationError(f"AI33 task {task_id} failed: {client.redact(str(message))}")
        await asyncio.sleep(interval_s)
    raise AudioGenerationError(f"AI33 task {task_id} timed out")


def select_audio_url(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    candidates: list[Any] = [metadata.get("audio_url")]
    candidates.extend(metadata.get("all_audio_urls") or [])
    suno_result = metadata.get("suno_result") or {}
    candidates.extend(clip.get("audio_url") for clip in suno_result.get("clips") or [] if isinstance(clip, dict))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            try:
                _download_host_policy(candidate.strip())
            except AudioGenerationError:
                continue
            else:
                return candidate.strip()
    raise AudioGenerationError("AI33 completed task has no allowlisted final audio URL")


def _download_host_policy(url: str) -> Literal["authenticated", "public"]:
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise AudioGenerationError("audio download URL has an invalid port") from exc
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password or port not in {None, 443}:
        raise AudioGenerationError("audio download URL must be standard HTTPS")
    if hostname == "ai33.pro" or hostname.endswith(".ai33.pro"):
        return "authenticated"
    if hostname == "suno.ai" or hostname.endswith(".suno.ai"):
        return "public"
    raise AudioGenerationError(f"audio download host is not allowlisted: {hostname}")


def _download_request(url: str, api_key: str) -> urllib.request.Request:
    policy = _download_host_policy(url)
    headers = {"User-Agent": "Mozilla/5.0"}
    if policy == "authenticated":
        headers["xi-api-key"] = api_key
    return urllib.request.Request(url, headers=headers)


def download_audio(
    url: str,
    output_path: Path,
    api_key: str,
    *,
    max_attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    opener: Callable[..., Any] = _open_without_redirect,
) -> None:
    _download_host_policy(url)
    temp_path = output_path.with_name(f"{output_path.name}.download")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    current_url = url
    redirects = 0
    attempt = 0
    try:
        while attempt < max_attempts:
            request = _download_request(current_url, api_key)
            total = 0
            try:
                with opener(request, timeout=180) as response, temp_path.open("wb") as handle:
                    final_url = response.geturl()
                    _download_host_policy(final_url)
                    if final_url != current_url:
                        raise AudioGenerationError("audio opener followed a redirect outside the guarded redirect flow")
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise AudioGenerationError("audio download exceeds the 100 MiB safety limit")
                        handle.write(chunk)
                if total == 0:
                    raise AudioGenerationError("audio download was empty")
                os.replace(temp_path, output_path)
                return
            except urllib.error.HTTPError as exc:
                if exc.code in REDIRECT_STATUS_CODES:
                    location = exc.headers.get("Location")
                    if not location:
                        raise AudioGenerationError("audio download redirect has no Location header") from exc
                    redirects += 1
                    if redirects > MAX_DOWNLOAD_REDIRECTS:
                        raise AudioGenerationError("audio download exceeded the redirect limit") from exc
                    current_url = urllib.parse.urljoin(current_url, location)
                    _download_host_policy(current_url)
                    continue
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == max_attempts - 1:
                    raise AudioGenerationError(f"audio download HTTP {exc.code}") from exc
                delay = max(_retry_after_seconds(exc.headers.get("Retry-After")), float(2**attempt))
                sleep(delay + jitter() * 0.25)
                attempt += 1
            except (TimeoutError, urllib.error.URLError, ConnectionError, http.client.HTTPException) as exc:
                if attempt == max_attempts - 1:
                    raise AudioGenerationError("audio download network request failed") from exc
                sleep(float(2**attempt) + jitter() * 0.25)
                attempt += 1
            except OSError as exc:
                raise AudioGenerationError(f"cannot write downloaded audio: {output_path}") from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()
    raise AudioGenerationError("audio download retries were exhausted")


def normalize_generated_audio(raw_path: Path, output_path: Path, asset: AssetSpec, raw_duration_s: float) -> float | None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
    if isinstance(asset, MusicSpec):
        if raw_duration_s <= LOOP_CROSSFADE_S * 2:
            raise AudioGenerationError("generated music is too short for the 1.5s loop crossfade")
        filter_graph = (
            f"[0:a]asplit=2[main_in][head_in];"
            f"[main_in]atrim=start={LOOP_CROSSFADE_S},asetpts=PTS-STARTPTS[main];"
            f"[head_in]atrim=end={LOOP_CROSSFADE_S},asetpts=PTS-STARTPTS[head];"
            f"[main][head]acrossfade=d={LOOP_CROSSFADE_S}:c1=tri:c2=tri[looped]"
        )
        command = [
            "ffmpeg", "-y", "-i", str(raw_path), "-filter_complex", filter_graph, "-map", "[looped]",
            "-vn", "-ac", "2", "-ar", "48000", "-codec:a", "libmp3lame", "-q:a", "2", str(temp_path),
        ]
    else:
        command = [
            "ffmpeg", "-y", "-i", str(raw_path), "-vn", "-ac", "2", "-ar", "48000",
            "-codec:a", "pcm_s16le", str(temp_path),
        ]
    try:
        try:
            run_command(command)
        except MediaError as exc:
            raise AudioGenerationError(f"ffmpeg could not normalize generated audio: {exc}") from exc
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    if isinstance(asset, MusicSpec):
        seam_ratio = measure_loop_seam_ratio(output_path)
        if not math.isfinite(seam_ratio) or seam_ratio > LOOP_SEAM_MAX_DISCONTINUITY_RATIO:
            output_path.unlink(missing_ok=True)
            raise AudioGenerationError(f"loop seam validation failed with ratio {seam_ratio:.3f}")
        return seam_ratio
    return None


def measure_loop_seam_ratio(path: Path, *, window_s: float = 0.05) -> float:
    duration_s = probe_duration(path)
    if duration_s <= window_s * 2:
        raise AudioGenerationError("audio is too short for loop seam validation")
    head = _decode_pcm(path, start_s=0, duration_s=window_s)
    tail = _decode_pcm(path, start_s=max(0, duration_s - window_s), duration_s=window_s)
    if len(head) < 2 or len(tail) < 2:
        raise AudioGenerationError("could not decode loop seam samples")
    return boundary_discontinuity_ratio(tail, head)


def boundary_discontinuity_ratio(tail: array[int], head: array[int], *, sample_rate: int = 48000) -> float:
    if len(tail) < 4 or len(head) < 4 or len(tail) % 2 or len(head) % 2:
        raise AudioGenerationError("loop seam windows must contain stereo PCM frames")
    radius_frames = max(8, int(sample_rate * 0.0005))
    boundary_energy: list[float] = []
    reference_energy: list[float] = []
    for channel in range(2):
        samples = [*tail[channel::2], *head[channel::2]]
        differences = [float(samples[index] - samples[index - 1]) for index in range(1, len(samples))]
        boundary_index = len(tail[channel::2]) - 1
        band_start = max(0, boundary_index - radius_frames)
        band_end = min(len(differences), boundary_index + radius_frames + 1)
        boundary_energy.extend(value * value for value in differences[band_start:band_end])
        reference_energy.extend(value * value for value in differences[:band_start])
        reference_energy.extend(value * value for value in differences[band_end:])
    if not boundary_energy or not reference_energy:
        raise AudioGenerationError("loop seam windows are too short for discontinuity analysis")
    boundary_rms = math.sqrt(sum(boundary_energy) / len(boundary_energy))
    reference_rms = math.sqrt(sum(reference_energy) / len(reference_energy))
    return boundary_rms / max(reference_rms, 1.0)


def _decode_pcm(path: Path, *, start_s: float, duration_s: float) -> array[int]:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{start_s:.6f}", "-t", f"{duration_s:.6f}", "-i", str(path),
            "-f", "s16le", "-codec:a", "pcm_s16le", "-ac", "2", "-ar", "48000", "-",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AudioGenerationError("ffmpeg could not decode loop seam samples")
    samples = array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def inspect_audio(path: Path) -> tuple[int, float]:
    try:
        stream_count = probe_audio_stream_count(path)
        duration_s = probe_duration(path)
    except MediaError as exc:
        raise AudioGenerationError(f"generated audio is not decodable: {exc}") from exc
    if stream_count != 1:
        raise AudioGenerationError(f"generated audio must have exactly one audio stream; found {stream_count}")
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise AudioGenerationError("generated audio must have positive finite duration")
    return stream_count, duration_s


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_output_path(output_dir: Path, asset: AssetSpec) -> Path:
    if isinstance(asset, MusicSpec):
        return output_dir / "music" / f"{asset.asset_id}.mp3"
    return output_dir / "sfx" / f"{asset.asset_id}.wav"


def _new_record(asset: AssetSpec) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "kind": "music" if isinstance(asset, MusicSpec) else "sfx",
        "mood": asset.mood if isinstance(asset, MusicSpec) else None,
        "sfx_kind": asset.sfx_kind if isinstance(asset, SfxSpec) else None,
        "endpoint": "/v1s/task/music-generation" if isinstance(asset, MusicSpec) else "/v1/task/sound-effect",
        "prompt": asset.prompt,
        "status": "pending",
        "task_id": None,
        "credit_cost": None,
        "credit_cost_source": None,
        "audio_url": None,
        "path": None,
        "sha256": None,
        "duration_s": None,
        "stream_count": None,
        "loop_seam_ratio": None,
        "error": None,
    }


def _record_matches_asset(record: dict[str, Any], asset: AssetSpec) -> bool:
    expected = _new_record(asset)
    return all(record.get(field) == expected[field] for field in ("kind", "mood", "sfx_kind", "endpoint", "prompt"))


def _read_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioGenerationError(f"cannot resume invalid generation report: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
        raise AudioGenerationError(f"cannot resume invalid generation report: {path}")
    return payload


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def _completed_record_is_valid(record: dict[str, Any], output_path: Path, asset: AssetSpec) -> bool:
    if record.get("status") not in {"done", "cached"} or not output_path.is_file():
        return False
    expected_hash = record.get("sha256")
    if not expected_hash or file_sha256(output_path) != expected_hash:
        return False
    try:
        inspect_audio(output_path)
        if (
            isinstance(asset, MusicSpec)
            and measure_loop_seam_ratio(output_path) > LOOP_SEAM_MAX_DISCONTINUITY_RATIO
        ):
            return False
    except (AudioGenerationError, MediaError):
        return False
    return True


def _credit_cost(payload: dict[str, Any]) -> int | None:
    value = payload.get("credit_cost")
    try:
        return max(0, int(math.ceil(float(value)))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _forecast_cost(asset: AssetSpec, observed_music_cost: int | None) -> int | None:
    if isinstance(asset, MusicSpec):
        return observed_music_cost
    return max(SFX_CREDITS_PER_SECOND, int(math.ceil(asset.duration_s * SFX_CREDITS_PER_SECOND)))


def _relative_output_path(output_dir: Path, output_path: Path) -> str:
    return output_path.relative_to(output_dir).as_posix()


def resolve_output_dir(path: Path) -> Path:
    expanded = path.expanduser()
    resolved = (expanded if expanded.is_absolute() else REPO_ROOT / expanded).resolve()
    try:
        relative = resolved.relative_to(WORK_ROOT)
    except ValueError as exc:
        raise AudioGenerationError(f"--output-dir must stay under the repository work directory: {WORK_ROOT}") from exc
    if not relative.parts:
        raise AudioGenerationError("--output-dir must be a child directory under repository work/")
    return resolved


def write_manifest(output_dir: Path, spec: AudioPackSpec, records: dict[str, dict[str, Any]]) -> Path:
    assets: list[AudioAsset] = []
    for asset in ordered_assets(spec):
        record = records[asset.asset_id]
        if record.get("status") not in {"done", "cached"} or not record.get("path"):
            continue
        if isinstance(asset, MusicSpec):
            assets.append(
                AudioAsset(
                    asset_id=asset.asset_id,
                    path=str(record["path"]),
                    kind="music",
                    mood=asset.mood,
                    loopable=True,
                    license_source=LICENSE_SOURCE,
                    attribution=ATTRIBUTION,
                    attribution_required=False,
                )
            )
        else:
            assets.append(
                AudioAsset(
                    asset_id=asset.asset_id,
                    path=str(record["path"]),
                    kind="sfx",
                    sfx_kind=asset.sfx_kind,
                    loopable=False,
                    license_source=LICENSE_SOURCE,
                    attribution=ATTRIBUTION,
                    attribution_required=False,
                )
            )
    manifest = AudioAssetManifest(version=1, base_dir=".", assets=assets)
    manifest_path = output_dir / "audio_assets.test.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return manifest_path


def write_not_for_production(output_dir: Path) -> Path:
    path = output_dir / "NOT_FOR_PRODUCTION.txt"
    path.write_text(
        "TEST ASSETS ONLY - NOT FOR PRODUCTION\n\n"
        "These files were generated through AI33/Suno. Commercial-use and royalty-free rights have not been verified.\n"
        "Do not publish or deliver them until the applicable provider terms and license evidence are reviewed.\n",
        encoding="utf-8",
    )
    return path


def _minimum_ready(records: dict[str, dict[str, Any]]) -> bool:
    done = [record for record in records.values() if record.get("status") in {"done", "cached"}]
    return (
        any(record.get("kind") == "music" and record.get("mood") == "default" for record in done)
        and any(record.get("sfx_kind") == "whoosh" for record in done)
        and any(record.get("sfx_kind") == "impact" for record in done)
    )


def _has_unknown_task_cost(records: dict[str, dict[str, Any]]) -> bool:
    return any(
        record.get("task_id")
        and record.get("credit_cost") is None
        and record.get("status") in {"submitted", "done", "cached", "failed"}
        for record in records.values()
    )


def run_live(
    spec: AudioPackSpec,
    *,
    output_dir: Path,
    api_key: str,
    max_credit_spend: int,
    resume: bool,
    force_assets: set[str],
    client: Ai33Client | None = None,
) -> dict[str, Any]:
    report_path = output_dir / "generation_report.json"
    if report_path.exists() and not resume:
        raise AudioGenerationError("generation_report.json already exists; pass --resume to continue safely")
    output_dir.mkdir(parents=True, exist_ok=True)
    write_not_for_production(output_dir)
    previous = _read_report(report_path) if report_path.exists() else None
    previous_records = {
        str(record.get("asset_id")): record for record in (previous or {}).get("assets", []) if isinstance(record, dict)
    }
    ordered = ordered_assets(spec)
    ordered_ids = {asset.asset_id for asset in ordered}
    unknown_force = force_assets - {asset.asset_id for asset in ordered}
    if unknown_force:
        raise AudioGenerationError(f"unknown --force-asset IDs: {', '.join(sorted(unknown_force))}")
    historical_credit_spend = int((previous or {}).get("historical_credit_spend") or 0)
    historical_credit_spend += sum(
        int(record.get("credit_cost") or 0)
        for asset_id, record in previous_records.items()
        if asset_id not in ordered_ids
    )
    records: dict[str, dict[str, Any]] = {}
    for asset in ordered:
        previous_record = previous_records.get(asset.asset_id)
        record = previous_record or _new_record(asset)
        reset_record = previous_record is not None and (
            not _record_matches_asset(record, asset) or asset.asset_id in force_assets
        )
        if reset_record:
            historical_credit_spend += int(record.get("credit_cost") or 0)
            record = _new_record(asset)
        elif asset.asset_id in force_assets:
            record = _new_record(asset)
        records[asset.asset_id] = record
    observed_music_credit_cost = (previous or {}).get("observed_music_credit_cost")
    report: dict[str, Any] = {
        "version": 1,
        "status": "running",
        "max_credit_spend": max_credit_spend,
        "historical_credit_spend": historical_credit_spend,
        "credits_spent": historical_credit_spend,
        "credit_spend_complete": True,
        "observed_music_credit_cost": observed_music_credit_cost,
        "minimum_pack_ready": False,
        "assets": [records[asset.asset_id] for asset in ordered],
    }
    ai33 = client or Ai33Client(api_key)
    budget_unknown = _has_unknown_task_cost(records)
    report["credit_spend_complete"] = not budget_unknown

    for asset in ordered:
        record = records[asset.asset_id]
        output_path = _asset_output_path(output_dir, asset)
        if asset.asset_id not in force_assets and _completed_record_is_valid(record, output_path, asset):
            record["status"] = "cached"
            continue
        try:
            if record.get("task_id") and asset.asset_id not in force_assets:
                task_id = str(record["task_id"])
            else:
                observed_music_cost = report.get("observed_music_credit_cost")
                forecast = _forecast_cost(asset, int(observed_music_cost) if observed_music_cost is not None else None)
                spent = historical_credit_spend + sum(int(item.get("credit_cost") or 0) for item in records.values())
                missing_safe_forecast = forecast is None and spent > 0
                if (
                    budget_unknown
                    or missing_safe_forecast
                    or (forecast is not None and spent + forecast > max_credit_spend)
                ):
                    record.update(status="skipped_budget", error="credit cap prevents another task submission")
                    _write_report(report_path, report)
                    continue
                submitted = ai33.submit(asset)
                task_id = submitted["task_id"]
                record.update(status="submitted", task_id=task_id, endpoint=submitted["endpoint"], error=None)
                _write_report(report_path, report)
            payload = asyncio.run(poll_task(ai33, task_id))
            cost = _credit_cost(payload)
            cost_source = "api" if cost is not None else None
            if cost is None and isinstance(asset, SfxSpec):
                cost = _forecast_cost(asset, None)
                cost_source = "documented_sfx_rate"
            record["credit_cost"] = cost
            record["credit_cost_source"] = cost_source
            if isinstance(asset, MusicSpec):
                if cost is None:
                    budget_unknown = True
                    report["credit_spend_complete"] = False
                elif asset.mood == "default" or report.get("observed_music_credit_cost") is None:
                    report["observed_music_credit_cost"] = cost
            budget_unknown = _has_unknown_task_cost(records)
            report["credit_spend_complete"] = not budget_unknown
            audio_url = select_audio_url(payload)
            raw_path = output_dir / "raw" / f"{asset.asset_id}{Path(urllib.parse.urlparse(audio_url).path).suffix or '.bin'}"
            download_audio(audio_url, raw_path, api_key)
            _, raw_duration = inspect_audio(raw_path)
            seam_ratio = normalize_generated_audio(raw_path, output_path, asset, raw_duration)
            stream_count, duration_s = inspect_audio(output_path)
            record.update(
                status="done",
                audio_url=audio_url,
                path=_relative_output_path(output_dir, output_path),
                sha256=file_sha256(output_path),
                duration_s=duration_s,
                stream_count=stream_count,
                loop_seam_ratio=seam_ratio,
                error=None,
            )
        except (AudioGenerationError, MediaError) as exc:
            record.update(status="failed", error=redact_secrets(str(exc), api_key))
            if record.get("task_id") and record.get("credit_cost") is None:
                budget_unknown = True
                report["credit_spend_complete"] = False
            elif isinstance(asset, MusicSpec) and report.get("observed_music_credit_cost") is None:
                budget_unknown = True
                report["credit_spend_complete"] = False
        report["credits_spent"] = historical_credit_spend + sum(
            int(item.get("credit_cost") or 0) for item in records.values()
        )
        report["minimum_pack_ready"] = _minimum_ready(records)
        _write_report(report_path, report)

    report["credits_spent"] = historical_credit_spend + sum(
        int(item.get("credit_cost") or 0) for item in records.values()
    )
    report["minimum_pack_ready"] = _minimum_ready(records)
    report["status"] = "complete" if all(item.get("status") in {"done", "cached"} for item in records.values()) else "partial"
    write_manifest(output_dir, spec, records)
    _write_report(report_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a test-only local music/SFX pack through AI33")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-credit-spend", type=int, default=10000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force-asset", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_credit_spend <= 0:
        raise AudioGenerationError("--max-credit-spend must be positive")
    spec = load_spec(args.spec.expanduser().resolve())
    output_dir = resolve_output_dir(args.output_dir)
    preview = {
        "spec": args.spec.name,
        "output_dir": str(output_dir),
        "max_credit_spend": args.max_credit_spend,
        "assets": [asset.asset_id for asset in ordered_assets(spec)],
        "note": "The first music canary has unknown cost; no request is made during dry-run.",
    }
    if args.dry_run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_live:
        raise AudioGenerationError("live AI33 generation requires --confirm-live")
    api_key = os.getenv("VIVOO_API_KEY", "").strip()
    if not api_key:
        raise AudioGenerationError("VIVOO_API_KEY env var is required for live generation")
    require_ffmpeg()
    report = run_live(
        spec,
        output_dir=output_dir,
        api_key=api_key,
        max_credit_spend=args.max_credit_spend,
        resume=args.resume,
        force_assets=set(args.force_asset),
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "credits_spent": report["credits_spent"],
                "minimum_pack_ready": report["minimum_pack_ready"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AudioGenerationError, MediaError) as exc:
        raise SystemExit(f"error: {exc}") from exc
