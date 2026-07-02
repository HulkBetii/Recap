from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from common.media import MediaError, normalize_audio, probe_duration, require_ffmpeg
from common.schema import ReviewBeat, TtsManifestEntry, TtsMeta, validate_review_script, write_json
from tts.cache import TtsCache, build_cache_key, stable_hash
from tts.concat import concat_voiceover
from tts.cost import estimate_cost, real_ratio, total_chars
from tts.providers import ProviderMode, ProviderResult, TtsProviderClient, TtsProviderError
from tts.sanitize import sanitize_tts_text
from tts.timing import build_timings

DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_PROVIDER_MODE: ProviderMode = "auto"
DEFAULT_INTER_BEAT_PAUSE = 0.15
DEFAULT_CONCURRENCY = 3
DEFAULT_SPEED = 1.0
DURATION_TOLERANCE_S = 0.1


class TtsError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 3 TTS: review_script.json -> voiceover.mp3 + beats_timing.json")
    parser.add_argument("--review-script", required=True, type=Path)
    parser.add_argument("--output-audio", required=True, type=Path)
    parser.add_argument("--output-timing", required=True, type=Path)
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--provider-mode", choices=["auto", "ai33", "genmax"], default=DEFAULT_PROVIDER_MODE)
    parser.add_argument("--genmax-voice-id", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--speed", default=DEFAULT_SPEED, type=float)
    parser.add_argument("--inter-beat-pause", default=DEFAULT_INTER_BEAT_PAUSE, type=float)
    parser.add_argument("--concurrency", default=DEFAULT_CONCURRENCY, type=int)
    parser.add_argument("--film-meta", default=None, type=Path)
    parser.add_argument("--no-normalize", action="store_true")
    parser.add_argument("--work-dir", default=Path("work/tts"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cost-per-1k-chars", default=0.0, type=float)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def load_review_script(path: Path) -> list[ReviewBeat]:
    if not path.is_file():
        raise TtsError(f"review_script.json does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise TtsError("review_script.json must be a JSON array")
    beats = [ReviewBeat.model_validate(item) for item in data]
    return sorted(beats, key=lambda item: item.beat_id)


def load_film_duration(path: Path | None) -> tuple[float | None, list[str]]:
    if path is None:
        return None, ["No --film-meta provided; real_ratio is null"]
    if not path.is_file():
        return None, [f"Film meta file not found: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("duration_s", payload.get("duration"))
        if raw is None:
            return None, [f"Film meta has no duration_s or duration: {path}"]
        duration = float(raw)
        if duration <= 0:
            return None, [f"Film meta duration is not positive: {path}"]
        return duration, []
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return None, [f"Could not parse film duration from {path}: {exc}"]


async def synthesize_one(
    *,
    beat: ReviewBeat,
    cache: TtsCache,
    manifest: dict[str, TtsManifestEntry],
    provider_client: TtsProviderClient,
    voice_id: str,
    provider_mode: ProviderMode,
    genmax_voice_id: str | None,
    model: str,
    speed: float,
    normalize: bool,
    semaphore: asyncio.Semaphore,
) -> tuple[Path, TtsManifestEntry]:
    text = sanitize_tts_text(beat.narration)
    target_provider = provider_mode if provider_mode != "auto" else "auto"
    selected_voice_id = genmax_voice_id if provider_mode == "genmax" and genmax_voice_id else voice_id
    cache_key = build_cache_key(
        provider=target_provider,
        voice_id=selected_voice_id,
        model=model,
        speed=speed,
        narration=text,
        normalized=normalize,
    )
    cached = cache.get_cached(manifest, beat.beat_id, cache_key)
    if cached is not None:
        return cached, manifest[str(beat.beat_id)]

    raw_path = cache.raw_path(beat.beat_id)
    final_path = cache.audio_path(beat.beat_id)
    async with semaphore:
        result = await provider_client.synthesize(
            text=text,
            voice_id=voice_id,
            genmax_voice_id=genmax_voice_id,
            model=model,
            speed=speed,
            provider_mode=provider_mode,
            output_path=raw_path,
        )
    if normalize:
        normalize_audio(raw_path, final_path)
    else:
        final_path.write_bytes(raw_path.read_bytes())
    entry = TtsManifestEntry(
        beat_id=beat.beat_id,
        cache_key=cache_key,
        narration_hash=stable_hash(text),
        provider=result.provider,
        voice_id=result.voice_id,
        model=model,
        speed=speed,
        normalized=normalize,
        audio_path=final_path.relative_to(cache.work_dir).as_posix(),
    )
    manifest[str(beat.beat_id)] = entry
    return final_path, entry


async def run_tts_with_client(args: argparse.Namespace, provider_client: TtsProviderClient) -> tuple[list, TtsMeta]:  # type: ignore[no-untyped-def]
    logger = logging.getLogger("tts")
    review_script = args.review_script.expanduser().resolve()
    output_audio = args.output_audio.expanduser().resolve()
    output_timing = args.output_timing.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    if args.speed <= 0:
        raise TtsError("--speed must be > 0")
    if args.inter_beat_pause < 0:
        raise TtsError("--inter-beat-pause must be >= 0")
    if args.concurrency <= 0:
        raise TtsError("--concurrency must be > 0")

    require_ffmpeg()
    beats = load_review_script(review_script)
    if not beats:
        raise TtsError("review_script.json is empty")
    cache = TtsCache(work_dir, force=args.force)
    cache.prepare()
    manifest = cache.load_manifest()
    semaphore = asyncio.Semaphore(args.concurrency)
    normalize = not args.no_normalize

    logger.info("[1/4] Synthesizing %d beats", len(beats))
    tasks = [
        synthesize_one(
            beat=beat,
            cache=cache,
            manifest=manifest,
            provider_client=provider_client,
            voice_id=args.voice_id,
            provider_mode=args.provider_mode,
            genmax_voice_id=args.genmax_voice_id,
            model=args.model,
            speed=args.speed,
            normalize=normalize,
            semaphore=semaphore,
        )
        for beat in beats
    ]
    results = await asyncio.gather(*tasks)
    cached_audio_paths = [path for path, _entry in results]
    cache.save_manifest(manifest)

    public_audio_dir = output_timing.parent / "audio"
    public_audio_dir.mkdir(parents=True, exist_ok=True)
    audio_paths: list[Path] = []
    for beat, cached_path in zip(beats, cached_audio_paths, strict=True):
        public_path = public_audio_dir / f"{beat.beat_id}.mp3"
        if cached_path.resolve() != public_path.resolve():
            shutil.copyfile(cached_path, public_path)
        audio_paths.append(public_path)

    logger.info("[2/4] Measuring beat durations")
    durations = [probe_duration(path) for path in audio_paths]
    timing_audio_paths = [Path("audio") / f"{beat.beat_id}.mp3" for beat in beats]
    timings = build_timings([beat.beat_id for beat in beats], timing_audio_paths, durations, args.inter_beat_pause)

    logger.info("[3/4] Concatenating voiceover")
    concat_voiceover(audio_paths, args.inter_beat_pause, work_dir, output_audio)
    voiceover_duration = probe_duration(output_audio)
    expected_total = timings[-1].tl_end if timings else 0.0
    warnings: list[str] = []
    if abs(voiceover_duration - expected_total) > DURATION_TOLERANCE_S:
        warnings.append(
            f"voiceover duration {voiceover_duration:.3f}s differs from last timing {expected_total:.3f}s"
        )
    film_duration_s, duration_warnings = load_film_duration(args.film_meta.expanduser().resolve() if args.film_meta else None)
    warnings.extend(duration_warnings)

    logger.info("[4/4] Writing timing and meta")
    write_json(output_timing, timings)
    chars = total_chars(beats)
    meta = TtsMeta(
        voice_id=args.voice_id,
        provider_mode=args.provider_mode,
        model=args.model,
        speed=args.speed,
        inter_beat_pause_s=args.inter_beat_pause,
        total_duration_s=expected_total,
        film_duration_s=film_duration_s,
        real_ratio=real_ratio(expected_total, film_duration_s),
        total_chars=chars,
        est_cost=estimate_cost(chars, args.cost_per_1k_chars),
        created_at=datetime.now(timezone.utc),
        cache_hits=cache.cache_hits,
        warnings=warnings,
    )
    write_json(output_timing.with_name("tts_meta.json"), meta)
    return timings, meta


async def run_tts(args: argparse.Namespace) -> int:
    await run_tts_with_client(args, TtsProviderClient())
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run_tts(args))
    except (TtsError, TtsProviderError, MediaError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"tts: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

