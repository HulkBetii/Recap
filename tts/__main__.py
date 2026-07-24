from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from common.media import MediaError, normalize_audio, probe_duration, require_ffmpeg
from common.integrity import file_hash
from common.narration_qa import BLOCKING_NARRATION_QA_CODES, analyze_narration_content
from common.schema import ReviewBeat, TtsManifestEntry, TtsMeta, write_json
from tts.cache import TtsCache, build_cache_key, stable_hash
from tts.concat import concat_voiceover
from tts.cost import estimate_cost, real_ratio
from tts.providers import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_VOICE,
    ProviderMode,
    TtsProviderClient,
    TtsProviderError,
    resolve_provider_order,
)
from tts.pronunciation_qa import analyze_pronunciation_risks
from tts.sanitize import normalize_tts_script
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
    parser.add_argument("--provider-mode", choices=["auto", "ai33", "genmax", "openai"], default=DEFAULT_PROVIDER_MODE)
    parser.add_argument("--genmax-voice-id", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--openai-voice", default=DEFAULT_OPENAI_VOICE)
    parser.add_argument("--speed", default=DEFAULT_SPEED, type=float)
    parser.add_argument("--inter-beat-pause", default=DEFAULT_INTER_BEAT_PAUSE, type=float)
    parser.add_argument("--concurrency", default=DEFAULT_CONCURRENCY, type=int)
    parser.add_argument("--film-meta", default=None, type=Path)
    parser.add_argument("--no-normalize", action="store_true")
    parser.add_argument("--work-dir", default=Path("work/tts"), type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cost-per-1k-chars", default=0.0, type=float)
    parser.add_argument("--tts-text-normalization", choices=["off", "basic", "vi"], default="vi")
    parser.add_argument("--tts-pronunciation-lexicon", default=None, type=Path)
    parser.add_argument("--tts-normalized-script-output", default=None, type=Path)
    parser.add_argument("--tts-normalization-report", default=None, type=Path)
    parser.add_argument("--pronunciation-qa", dest="pronunciation_qa", action="store_true", default=True)
    parser.add_argument("--no-pronunciation-qa", dest="pronunciation_qa", action="store_false")
    parser.add_argument("--pronunciation-qa-output", default=None, type=Path)
    parser.add_argument("--pronunciation-suggest-backend", choices=["off", "chatgpt_playwright", "openai_api"], default="off")
    parser.add_argument("--lexicon-candidates-output", default=None, type=Path)
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

def validate_review_script_content(beats: list[ReviewBeat]) -> None:
    issues = analyze_narration_content(beats)
    blocking = [
        issue
        for issue in issues
        if issue.get("level") == "error" or issue.get("code") in BLOCKING_NARRATION_QA_CODES
    ]
    if not blocking:
        return
    details = "; ".join(
        f"{item.get('code')}: {item.get('message')} (beats={item.get('beat_ids', [])})"
        for item in blocking[:8]
    )
    raise TtsError(f"review_script content QA failed: {details}")


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
    text: str,
    cache_salt: str,
    cache: TtsCache,
    manifest: dict[str, TtsManifestEntry],
    provider_client: TtsProviderClient,
    voice_id: str,
    provider_mode: ProviderMode,
    genmax_voice_id: str | None,
    model: str,
    openai_model: str,
    openai_voice: str,
    provider_config: dict[str, object],
    speed: float,
    normalize: bool,
    semaphore: asyncio.Semaphore,
) -> tuple[Path, TtsManifestEntry]:
    cache_key = build_cache_key(
        provider=provider_mode,
        voice_id=voice_id,
        model=model,
        speed=speed,
        narration=text + "\n__tts_cache_salt__=" + cache_salt,
        normalized=normalize,
        provider_config=provider_config,
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
            openai_model=openai_model,
            openai_voice=openai_voice,
            speed=speed,
            provider_mode=provider_mode,
            output_path=raw_path,
        )
    if len(result.attempted_providers) > 1:
        logging.getLogger("tts").warning(
            "Beat %s used %s after provider fallback chain %s",
            beat.beat_id,
            result.provider,
            " -> ".join(result.attempted_providers),
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
        model=result.model or model,
        speed=speed,
        normalized=normalize,
        audio_path=final_path.relative_to(cache.work_dir).as_posix(),
    )
    manifest[str(beat.beat_id)] = entry
    cache.save_manifest(manifest)
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

    openai_model = getattr(args, "openai_model", DEFAULT_OPENAI_MODEL)
    openai_voice = getattr(args, "openai_voice", DEFAULT_OPENAI_VOICE)
    provider_order = resolve_provider_order(
        args.provider_mode,
        voice_id=args.voice_id,
        genmax_voice_id=args.genmax_voice_id,
    )
    provider_config: dict[str, object] = {
        "mode": args.provider_mode,
        "order": provider_order,
        "ai33_voice_id": args.voice_id,
        "genmax_voice_id": args.genmax_voice_id,
        "provider_model": args.model,
        "openai_model": openai_model,
        "openai_voice": openai_voice,
    }

    require_ffmpeg()
    beats = load_review_script(review_script)
    if not beats:
        raise TtsError("review_script.json is empty")
    validate_review_script_content(beats)
    review_script_hash = file_hash(review_script)
    text_normalization = getattr(args, "tts_text_normalization", "vi")
    lexicon_path = getattr(args, "tts_pronunciation_lexicon", None)
    lexicon_path = lexicon_path.expanduser().resolve() if lexicon_path else None
    normalized_items, normalization_report = normalize_tts_script(
        beats,
        mode=text_normalization,
        pronunciation_lexicon_path=lexicon_path,
    )
    normalized_by_beat = {item.beat_id: item.tts_text for item in normalized_items}
    normalized_script_output = getattr(args, "tts_normalized_script_output", None)
    if normalized_script_output is None:
        normalized_script_output = output_timing.with_name("tts_script.json")
    else:
        normalized_script_output = normalized_script_output.expanduser().resolve()
    normalization_report_output = getattr(args, "tts_normalization_report", None)
    if normalization_report_output is None:
        normalization_report_output = output_timing.with_name("tts_normalization_report.json")
    else:
        normalization_report_output = normalization_report_output.expanduser().resolve()
    write_json(normalized_script_output, [item.to_json() for item in normalized_items])
    write_json(normalization_report_output, normalization_report.to_json())
    pronunciation_qa_output = getattr(args, "pronunciation_qa_output", None)
    if pronunciation_qa_output is None:
        pronunciation_qa_output = output_timing.with_name("tts_pronunciation_qa.json")
    else:
        pronunciation_qa_output = pronunciation_qa_output.expanduser().resolve()
    qa_report = analyze_pronunciation_risks(
        normalized_items,
        enabled=getattr(args, "pronunciation_qa", True),
        suggest_backend=getattr(args, "pronunciation_suggest_backend", "off"),
    )
    write_json(pronunciation_qa_output, qa_report.to_json())
    lexicon_candidates_output = getattr(args, "lexicon_candidates_output", None)
    if lexicon_candidates_output is not None:
        write_json(lexicon_candidates_output.expanduser().resolve(), qa_report.lexicon_candidates)

    cache_salt = stable_hash(json.dumps({
        "text_normalization": text_normalization,
        "pronunciation_lexicon_path": str(lexicon_path) if lexicon_path else None,
        "pronunciation_lexicon_hash": stable_hash(lexicon_path.read_text(encoding="utf-8")) if lexicon_path else None,
    }, ensure_ascii=False, sort_keys=True))

    cache = TtsCache(work_dir, force=args.force)
    cache.prepare()
    manifest = cache.load_manifest()
    semaphore = asyncio.Semaphore(args.concurrency)
    normalize = not args.no_normalize

    logger.info("[1/4] Synthesizing %d beats", len(beats))
    tasks = [
        synthesize_one(
            beat=beat,
            text=normalized_by_beat[beat.beat_id],
            cache_salt=cache_salt,
            cache=cache,
            manifest=manifest,
            provider_client=provider_client,
            voice_id=args.voice_id,
            provider_mode=args.provider_mode,
            genmax_voice_id=args.genmax_voice_id,
            model=args.model,
            openai_model=openai_model,
            openai_voice=openai_voice,
            provider_config=provider_config,
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
    warnings.extend(qa_report.warnings)
    entries = [entry for _path, entry in results]
    provider_counts = dict(sorted(Counter(entry.provider for entry in entries).items()))
    fallback_count = sum(count for provider, count in provider_counts.items() if provider != provider_order[0])
    if fallback_count:
        warnings.append(
            f"TTS provider fallback used for {fallback_count}/{len(entries)} beat(s); primary={provider_order[0]}"
        )
    primary_provider = provider_order[0]
    if primary_provider == "openai":
        primary_voice_id = openai_voice
        primary_model = openai_model
    elif primary_provider == "genmax":
        primary_voice_id = args.genmax_voice_id or args.voice_id
        primary_model = args.model
    else:
        primary_voice_id = args.voice_id
        primary_model = args.model

    logger.info("[4/4] Writing timing and meta")
    write_json(output_timing, timings)
    chars = sum(len(item.tts_text) for item in normalized_items)
    meta = TtsMeta(
        voice_id=primary_voice_id,
        provider_mode=args.provider_mode,
        model=primary_model,
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
        text_normalization=text_normalization,
        pronunciation_lexicon_path=str(lexicon_path) if lexicon_path else None,
        n_text_normalized=normalization_report.n_changed,
        normalization_warnings=normalization_report.warnings,
        pronunciation_qa_enabled=qa_report.enabled,
        pronunciation_risk_count=qa_report.n_risks,
        pronunciation_suggest_backend=qa_report.suggest_backend,
        pronunciation_warnings=qa_report.warnings,
        providers_used=[provider for provider in provider_order if provider in provider_counts],
        provider_counts=provider_counts,
        fallback_count=fallback_count,
        openai_model=openai_model,
        openai_voice=openai_voice,
        review_script_hash=review_script_hash,
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

