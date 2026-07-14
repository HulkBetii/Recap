from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.integrity import file_hash, stable_hash
from common.media import MediaError
from common.schema import (
    CommentaryAudio,
    CommentaryAudioItem,
    CommentaryFitRequest,
    CommentaryFitRequests,
    CommentaryScript,
    CommentaryScriptSlot,
    CommentaryVoicePolicy,
    ReactionSource,
    validate_commentary_audio,
)
from reaction_remix.tts.asr import JapaneseAsrVerifier
from reaction_remix.tts.audio import (
    AudioMetrics,
    measure_audio,
    normalization_cache_signature,
    normalize_commentary_audio,
    pad_audio_tail,
)
from reaction_remix.tts.cache import CommentaryCacheEntry, CommentaryTtsCache, cache_payload
from reaction_remix.tts.japanese import normalize_japanese_tts_text
from tts.providers import ProviderResult, TtsProviderClient, TtsProviderError

SCHEMA_VERSION = "reaction-remix.v1"
AI33_VOICE_ID = "elevenlabs_QPtBgsg1dxKTQHNpHrHt"
AI33_MODEL = "eleven_multilingual_v2"


class CommentaryTtsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReactionTtsSettings:
    provider: str = "ai33"
    voice_id: str = AI33_VOICE_ID
    model: str = AI33_MODEL
    speed: float = 1.0
    concurrency: int = 1
    fallback_provider: str | None = None
    text_normalization: str = "ja_basic"
    trim_handle_ms: int = 80
    target_lufs: float = -14.0
    max_true_peak_db: float = -2.0
    min_asr_similarity: float = 0.90
    fit_tolerance_s: float = 0.10
    max_fit_iterations: int = 2

    def validate(self) -> None:
        locked = {
            "provider": (self.provider, "ai33"),
            "voice_id": (self.voice_id, AI33_VOICE_ID),
            "model": (self.model, AI33_MODEL),
            "speed": (self.speed, 1.0),
            "concurrency": (self.concurrency, 1),
            "fallback_provider": (self.fallback_provider, None),
            "text_normalization": (self.text_normalization, "ja_basic"),
        }
        for name, (actual, expected) in locked.items():
            if actual != expected:
                raise CommentaryTtsError(f"reaction TTS {name} is locked to {expected!r}")
        if self.fit_tolerance_s <= 0:
            raise CommentaryTtsError("fit_tolerance_s must be positive")
        if self.trim_handle_ms < 0:
            raise CommentaryTtsError("trim_handle_ms must be non-negative")
        if not 0 <= self.min_asr_similarity <= 1:
            raise CommentaryTtsError("min_asr_similarity must be between 0 and 1")
        if not 0 <= self.max_fit_iterations <= 2:
            raise CommentaryTtsError("max_fit_iterations must be between 0 and 2")


def frame_tolerance(source: ReactionSource, configured_s: float) -> float:
    frame_s = source.video.fps_den / source.video.fps_num
    return max(configured_s, frame_s)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temp)
    temp.replace(destination)


async def _synthesize_raw(
    provider_client: TtsProviderClient,
    *,
    text: str,
    output_path: Path,
    settings: ReactionTtsSettings,
) -> ProviderResult:
    result = await provider_client.synthesize(
        text=text,
        voice_id=settings.voice_id,
        genmax_voice_id=None,
        model=settings.model,
        speed=settings.speed,
        provider_mode="ai33",
        output_path=output_path,
    )
    if result.provider != "ai33" or result.voice_id != settings.voice_id:
        raise CommentaryTtsError("AI33 returned an unexpected provider or voice")
    return result


def _entry_to_metrics(entry: CommentaryCacheEntry) -> AudioMetrics:
    return AudioMetrics(entry.duration_s, entry.lufs_i, entry.true_peak_dbfs)


def _normalize_slot_audio(
    raw_path: Path,
    final_path: Path,
    *,
    slot: CommentaryScriptSlot,
    settings: ReactionTtsSettings,
    tolerance: float,
) -> AudioMetrics:
    normalize_commentary_audio(
        raw_path,
        final_path,
        trim_handle_ms=settings.trim_handle_ms,
        target_lufs=settings.target_lufs,
        max_true_peak_db=settings.max_true_peak_db,
    )
    metrics = measure_audio(final_path)
    if metrics.duration_s < slot.target_duration_s and slot.target_duration_s - metrics.duration_s <= tolerance:
        padded_path = final_path.with_suffix(".padded.mp3")
        pad_audio_tail(final_path, padded_path, slot.target_duration_s - metrics.duration_s)
        padded_path.replace(final_path)
        metrics = measure_audio(final_path)
    return metrics


async def synthesize_commentary(
    script: CommentaryScript,
    source: ReactionSource,
    *,
    output_path: Path,
    fit_request_path: Path,
    work_dir: Path,
    provider_client: TtsProviderClient | None = None,
    asr_verifier: JapaneseAsrVerifier | None = None,
    settings: ReactionTtsSettings | None = None,
    previous_fit_requests: CommentaryFitRequests | None = None,
    script_hash: str | None = None,
    force: bool = False,
) -> tuple[CommentaryAudio, CommentaryFitRequests]:
    settings = settings or ReactionTtsSettings()
    settings.validate()
    if script.source_hash != source.input_hash:
        raise CommentaryTtsError("commentary script and reaction source hashes do not match")
    script_hash = script_hash or stable_hash(script.model_dump(mode="json"))
    provider_client = provider_client or TtsProviderClient()
    asr_verifier = asr_verifier or JapaneseAsrVerifier()
    cache = CommentaryTtsCache(work_dir, force=force)
    cache.prepare()
    manifest = cache.load()
    previous_attempts = {
        item.slot_id: item.attempt for item in (previous_fit_requests.requests if previous_fit_requests else [])
    }
    if previous_fit_requests is not None and previous_fit_requests.source_hash != script.source_hash:
        raise CommentaryTtsError("previous fit request source hash does not match the commentary script")
    tolerance = frame_tolerance(source, settings.fit_tolerance_s)
    public_audio_dir = output_path.parent / "audio"
    audio_items: list[CommentaryAudioItem] = []
    fit_items: list[CommentaryFitRequest] = []
    failures: list[str] = []

    for slot in script.slots:
        normalized_text = normalize_japanese_tts_text(slot.text_ja)
        text_hash = stable_hash(normalized_text)
        cache_key = stable_hash(
            cache_payload(
                slot_id=slot.slot_id,
                text_hash=text_hash,
                provider=settings.provider,
                voice_id=settings.voice_id,
                model=settings.model,
                speed=settings.speed,
                normalization=settings.text_normalization,
                trim_handle_ms=settings.trim_handle_ms,
                target_lufs=settings.target_lufs,
                max_true_peak_db=settings.max_true_peak_db,
                audio_normalization=normalization_cache_signature(),
                asr_model=asr_verifier.model_name,
            )
        )
        entry = cache.get_valid(manifest, cache_key)
        try:
            if entry is not None:
                metrics = _entry_to_metrics(entry)
                if (
                    metrics.lufs_i is None
                    or not -16.0 <= metrics.lufs_i <= -12.0
                    or metrics.true_peak_dbfs is None
                    or metrics.true_peak_dbfs > settings.max_true_peak_db
                ):
                    raw_path = work_dir / entry.raw_path
                    final_path = work_dir / entry.audio_path
                    metrics = _normalize_slot_audio(
                        raw_path,
                        final_path,
                        slot=slot,
                        settings=settings,
                        tolerance=tolerance,
                    )
                    entry = entry.model_copy(
                        update={
                            "audio_sha256": file_hash(final_path) or "",
                            "duration_s": metrics.duration_s,
                            "lufs_i": metrics.lufs_i,
                            "true_peak_dbfs": metrics.true_peak_dbfs,
                            "asr_text_match": asr_verifier.similarity(final_path, normalized_text),
                        }
                    )
                    manifest[cache_key] = entry
                    cache.save(manifest)
            if entry is None:
                raw_path = cache.raw_path(cache_key)
                final_path = cache.audio_path(cache_key)
                await _synthesize_raw(provider_client, text=normalized_text, output_path=raw_path, settings=settings)
                if not raw_path.is_file() or raw_path.stat().st_size == 0:
                    raise CommentaryTtsError("AI33 produced empty audio")
                metrics = _normalize_slot_audio(
                    raw_path,
                    final_path,
                    slot=slot,
                    settings=settings,
                    tolerance=tolerance,
                )
                asr_match = asr_verifier.similarity(final_path, normalized_text)
                entry = CommentaryCacheEntry(
                    slot_id=slot.slot_id,
                    cache_key=cache_key,
                    raw_path=raw_path.relative_to(work_dir).as_posix(),
                    audio_path=final_path.relative_to(work_dir).as_posix(),
                    raw_sha256=file_hash(raw_path) or "",
                    audio_sha256=file_hash(final_path) or "",
                    duration_s=metrics.duration_s,
                    lufs_i=metrics.lufs_i,
                    true_peak_dbfs=metrics.true_peak_dbfs,
                    asr_text_match=asr_match,
                    requested_model=settings.model,
                    # AI33 v3 does not currently return a model identifier in task metadata.
                    actual_model=None,
                )
                manifest[cache_key] = entry
                cache.save(manifest)
            metrics = _entry_to_metrics(entry)
            if metrics.lufs_i is None or not -16.0 <= metrics.lufs_i <= -12.0:
                raise CommentaryTtsError(f"loudness {metrics.lufs_i} LUFS is outside -16..-12")
            if metrics.true_peak_dbfs is None or metrics.true_peak_dbfs > settings.max_true_peak_db:
                raise CommentaryTtsError(
                    f"true peak {metrics.true_peak_dbfs} dBFS exceeds {settings.max_true_peak_db}"
                )
            fit_direction: str | None = None
            fit_reason: str | None = None
            if entry.asr_text_match is None or entry.asr_text_match < settings.min_asr_similarity:
                fit_direction = "clarify"
                fit_reason = (
                    f"Japanese ASR similarity {entry.asr_text_match} is below "
                    f"{settings.min_asr_similarity}; rewrite with clearer TTS-friendly wording"
                )
            elif metrics.duration_s < slot.target_duration_s - tolerance or metrics.duration_s > slot.max_duration_s + tolerance:
                fit_direction = "lengthen" if metrics.duration_s < slot.target_duration_s - tolerance else "shorten"
                fit_reason = (
                    "AI33 commentary is shorter than the planned target"
                    if fit_direction == "lengthen"
                    else "AI33 commentary exceeds the available visual capacity"
                )
            if fit_direction is not None:
                attempt = previous_attempts.get(slot.slot_id, 0) + 1
                if attempt > settings.max_fit_iterations:
                    raise CommentaryTtsError(f"fit repair limit exceeded for {slot.slot_id}")
                fit_items.append(
                    CommentaryFitRequest(
                        slot_id=slot.slot_id,
                        actual_duration_s=metrics.duration_s,
                        target_duration_s=slot.target_duration_s,
                        max_duration_s=slot.max_duration_s,
                        tolerance_s=tolerance,
                        direction=fit_direction,
                        attempt=attempt,
                        reason=fit_reason or "commentary quality repair is required",
                    )
                )
            cached_audio = work_dir / entry.audio_path
            public_path = public_audio_dir / f"{slot.slot_id}.mp3"
            _atomic_copy(cached_audio, public_path)
            audio_items.append(
                CommentaryAudioItem(
                    slot_id=slot.slot_id,
                    audio_path=public_path.relative_to(output_path.parent).as_posix(),
                    duration_s=metrics.duration_s,
                    provider="ai33",
                    voice_id=settings.voice_id,
                    model=settings.model,
                    speed=settings.speed,
                    text_hash=text_hash,
                    cache_key=cache_key,
                    normalized=True,
                    lufs_i=metrics.lufs_i,
                    true_peak_dbfs=metrics.true_peak_dbfs,
                    asr_text_match=entry.asr_text_match,
                    audio_sha256=file_hash(public_path),
                    requested_model=entry.requested_model or settings.model,
                    actual_model=entry.actual_model,
                    warnings=[],
                )
            )
        except (CommentaryTtsError, TtsProviderError, MediaError, OSError, ValueError) as exc:
            failures.append(f"{slot.slot_id}: {exc}")

    fit_requests = CommentaryFitRequests(
        schema_version=SCHEMA_VERSION,
        source_hash=script.source_hash,
        script_hash=script_hash,
        requests=fit_items,
        created_at=datetime.now(timezone.utc),
        warnings=[],
    )
    from common.integrity import atomic_write_json

    atomic_write_json(fit_request_path, fit_requests.model_dump(mode="json"))
    if failures:
        raise CommentaryTtsError("commentary TTS failed: " + "; ".join(failures))
    audio = CommentaryAudio(
        schema_version=SCHEMA_VERSION,
        source_hash=script.source_hash,
        script_hash=script_hash,
        voice_policy=CommentaryVoicePolicy(
            provider="ai33",
            voice_id=settings.voice_id,
            model=settings.model,
            speed=settings.speed,
            fallback_provider=None,
            text_normalization=settings.text_normalization,
        ),
        items=audio_items,
        total_commentary_duration_s=sum(item.duration_s for item in audio_items),
        created_at=datetime.now(timezone.utc),
        warnings=["commentary duration repair is required"] if fit_items else [],
    )
    validate_commentary_audio(audio, script)
    from common.integrity import atomic_write_json

    atomic_write_json(output_path, audio.model_dump(mode="json"))
    return audio, fit_requests
