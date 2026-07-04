from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from common.schema import ReviewBeat, ReviewMeta, validate_review_script, write_json
from review.budget import allocate_char_targets, compute_budget, estimate_total_chars
from review.cache import ReviewCache
from review.coverage import coverage_ratio
from review.consistency import apply_narration_consistency
from review.inputs import ReviewInputError, load_duration, load_film_map
from review.llm_flow import regenerate_beat, request_narration, request_outline, request_qa
from review.models import NarrationBeat, OutlineResult, QaResult
from review.playwright_chat import PlaywrightChatClient, PlaywrightChatError
from review.session import build_chat_session_meta, resolve_initial_chat_url, save_chat_session
from review.style import (
    DEFAULT_MAX_SENTENCE_CHARS,
    DEFAULT_STYLE_PRESET,
    DEFAULT_STYLE_SAMPLE,
    DEFAULT_STYLE_STRENGTH,
    DEFAULT_TARGET_SENTENCE_CHARS,
    StyleConfig,
    build_style_guide,
    check_readability,
    issue_to_prompt,
    read_clean_style_sample,
    style_config_key,
)
from review.timecode import derive_review_beats
from review.view import build_film_map_view

DEFAULT_TARGET_RATIO = 0.33
DEFAULT_TTS_CPS = 15.0
DEFAULT_MIN_COVERAGE = 0.85
DEFAULT_MAX_QA_ITERATIONS = 3
DEFAULT_PROFILE_DIR = Path("data/chrome_user_data/PROFILE_GPT_1")


class ReviewError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 2 review: film_map.json -> review_script.json")
    parser.add_argument("--film-map", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-ratio", default=DEFAULT_TARGET_RATIO, type=float)
    parser.add_argument("--tts-cps", default=DEFAULT_TTS_CPS, type=float)
    parser.add_argument("--min-coverage", default=DEFAULT_MIN_COVERAGE, type=float)
    parser.add_argument("--max-qa-iterations", default=DEFAULT_MAX_QA_ITERATIONS, type=int)
    parser.add_argument("--style-sample", default=None)
    parser.add_argument("--style-preset", default=DEFAULT_STYLE_PRESET)
    parser.add_argument("--style-strength", default=DEFAULT_STYLE_STRENGTH, choices=["medium", "strong"])
    parser.add_argument("--style-qa", action="store_true", default=True)
    parser.add_argument("--no-style-qa", dest="style_qa", action="store_false")
    parser.add_argument("--target-sentence-chars", default=DEFAULT_TARGET_SENTENCE_CHARS, type=int)
    parser.add_argument("--max-sentence-chars", default=DEFAULT_MAX_SENTENCE_CHARS, type=int)
    parser.add_argument("--work-dir", default=Path("work/review"), type=Path)
    parser.add_argument("--chatgpt-profile-dir", default=DEFAULT_PROFILE_DIR, type=Path)
    parser.add_argument("--chat-session-policy", default="auto", choices=["auto", "new", "resume"], help="ChatGPT conversation policy for this video/run")
    parser.add_argument("--chat-session-meta", default=None, type=Path, help="Path to chat_session_meta.json; defaults to work-dir/chat_session_meta.json")
    parser.add_argument("--chatgpt-session-file", default=None, type=Path, help="Optional saved ChatGPT cookies/session JSON to restore before opening ChatGPT")
    parser.add_argument("--chat-title", default=None, help="Optional human title saved in chat_session_meta.json")
    parser.add_argument("--reply-timeout-s", default=None, type=int, help="Max seconds to wait for one ChatGPT response")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


async def build_review_with_client(args: argparse.Namespace, client) -> tuple[list[ReviewBeat], ReviewMeta]:  # type: ignore[no-untyped-def]
    logger = logging.getLogger("review")
    film_map_path = args.film_map.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()

    if args.target_ratio <= 0:
        raise ReviewError("--target-ratio must be > 0")
    if args.tts_cps <= 0:
        raise ReviewError("--tts-cps must be > 0")
    if not 0 <= args.min_coverage <= 1:
        raise ReviewError("--min-coverage must be between 0 and 1")
    if args.max_qa_iterations < 0:
        raise ReviewError("--max-qa-iterations must be >= 0")
    if args.target_sentence_chars <= 0 or args.max_sentence_chars <= 0:
        raise ReviewError("sentence char limits must be > 0")
    if args.max_sentence_chars < args.target_sentence_chars:
        raise ReviewError("--max-sentence-chars must be >= --target-sentence-chars")

    film_map = load_film_map(film_map_path)
    duration_s, warnings = load_duration(film_map_path, film_map)
    film_map_view = build_film_map_view(film_map)
    style_sample_path = Path(args.style_sample).expanduser().resolve() if args.style_sample else DEFAULT_STYLE_SAMPLE
    cleaned_style_sample = read_clean_style_sample(style_sample_path)
    style_config = StyleConfig(
        preset=args.style_preset,
        strength=args.style_strength,
        sample_path=style_sample_path,
        style_qa=args.style_qa,
        target_sentence_chars=args.target_sentence_chars,
        max_sentence_chars=args.max_sentence_chars,
    )
    style_guide = build_style_guide(style_config, cleaned_style_sample)
    target_video_s, char_budget = compute_budget(duration_s, args.target_ratio, args.tts_cps)

    cache = ReviewCache(work_dir, force=args.force)
    cache.prepare()
    refresh_style_cache(cache, style_config_key(style_config, cleaned_style_sample))

    if cache.has("outline.json"):
        logger.info("[1/4] Using cached outline.json")
        outline_result = OutlineResult.model_validate(cache.read_json("outline.json"))
    else:
        logger.info("[1/4] Requesting outline + glossary")
        outline_result = await request_outline(
            client,
            film_map_view=film_map_view,
            target_video_s=target_video_s,
            char_budget=char_budget,
            min_coverage=args.min_coverage,
            style_sample=style_guide,
        )
        cache.write_json("outline.json", outline_result)

    char_targets = allocate_char_targets(outline_result.outline, char_budget)
    if cache.has("narration.json"):
        logger.info("[2/4] Using cached narration.json")
        narration = [NarrationBeat.model_validate(item) for item in cache.read_json("narration.json")]
    else:
        logger.info("[2/4] Requesting narration")
        narration = await request_narration(
            client,
            outline=outline_result.outline,
            glossary=outline_result.glossary,
            char_targets=char_targets,
            style_sample=style_guide,
        )
        cache.write_json("narration.json", narration)

    narration, consistency_warnings = ensure_narration_consistency(cache, narration, outline_result.glossary, logger)
    warnings.extend(consistency_warnings)
    beats = derive_review_beats(outline=outline_result.outline, narration=narration, film_map=film_map)
    style_qa_report: list[dict] = []
    n_style_rewrites = 0
    readability_warnings: list[str] = []
    if args.style_qa:
        narration, beats, style_qa_report, n_style_rewrites, readability_warnings = await ensure_style_readability(
            cache=cache,
            client=client,
            narration=narration,
            beats=beats,
            outline=outline_result.outline,
            film_map=film_map,
            glossary=outline_result.glossary,
            char_targets=char_targets,
            style_config=style_config,
            style_guide=style_guide,
            logger=logger,
            max_iterations=args.max_qa_iterations,
        )
        warnings.extend(readability_warnings)
    qa_report: list[dict] = []
    n_qa_iterations = 0

    for iteration in range(args.max_qa_iterations + 1):
        current_coverage = coverage_ratio(beats, len(film_map))
        if iteration == 0 and cache.has("qa.json"):
            logger.info("[3/4] Using cached qa.json")
            qa = QaResult.model_validate(cache.read_json("qa.json"))
        else:
            logger.info("[3/4] Requesting QA iteration %d", iteration)
            qa = await request_qa(
                client,
                film_map_view=film_map_view,
                beats=beats,
                glossary=outline_result.glossary,
                char_budget=char_budget,
                coverage_pct=current_coverage,
            )
            if iteration == 0:
                cache.write_json("qa.json", qa)
            else:
                cache.write_json(f"revisions/qa-{iteration}.json", qa)
        qa_report.append(qa.model_dump_public())
        if qa.passed or not qa.issues or iteration >= args.max_qa_iterations:
            n_qa_iterations = iteration
            break
        narration_by_id = {item.beat_id: item for item in narration}
        for issue in qa.issues:
            if issue.beat_id >= len(beats):
                continue
            revised = await regenerate_beat(
                client,
                beat=beats[issue.beat_id],
                issue=f"{issue.type}: {issue.suggestion}",
                glossary=outline_result.glossary,
                char_target=char_targets[min(issue.beat_id, len(char_targets) - 1)],
                style_sample=style_guide,
            )
            narration_by_id[revised.beat_id] = revised
        narration = [narration_by_id[index] for index in sorted(narration_by_id)]
        narration, revision_consistency_warnings = apply_narration_consistency(narration, outline_result.glossary)
        warnings.extend(revision_consistency_warnings)
        cache.write_json(f"revisions/narration-{iteration + 1}.json", narration)
        beats = derive_review_beats(outline=outline_result.outline, narration=narration, film_map=film_map)

    coverage_pct = coverage_ratio(beats, len(film_map))
    if coverage_pct < args.min_coverage:
        warnings.append(f"Coverage {coverage_pct:.3f} is below min_coverage {args.min_coverage:.3f}")
    validate_review_script(beats, film_map)

    meta = ReviewMeta(
        glossary=outline_result.glossary,
        target_video_s=target_video_s,
        char_budget=char_budget,
        est_total_chars=estimate_total_chars(beats),
        coverage_pct=coverage_pct,
        qa_report=qa_report,
        n_qa_iterations=n_qa_iterations,
        model_versions={"llm": "chatgpt-playwright"},
        created_at=datetime.now(timezone.utc),
        warnings=warnings,
        cache_hits=cache.cache_hits,
        consistency_warnings=[warning for warning in warnings if "consistency" in warning],
        style_preset=args.style_preset,
        style_strength=args.style_strength,
        style_sample_path=str(style_sample_path) if style_sample_path else None,
        style_qa_report=style_qa_report,
        n_style_rewrites=n_style_rewrites,
        readability_warnings=readability_warnings,
    )
    write_json(output_path, beats)
    write_json(output_path.with_name(f"{output_path.stem}.meta.json"), meta)
    return beats, meta


def refresh_style_cache(cache: ReviewCache, config_key: dict) -> None:
    current_path = cache.path("style_config.json")
    if current_path.exists():
        try:
            previous = cache.read_json("style_config.json")
        except Exception:  # noqa: BLE001 - bad cache should be rebuilt
            previous = None
        if previous != config_key:
            for name in ("outline.json", "narration.json", "narration_consistent.json", "narration_style_checked.json", "qa.json", "style_qa.json"):
                target = cache.path(name)
                if target.exists():
                    target.unlink()
            for dirname in ("revisions", "style_revisions"):
                target_dir = cache.path(dirname)
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
    cache.write_json("style_config.json", config_key)


async def ensure_style_readability(
    *,
    cache: ReviewCache,
    client,
    narration: list[NarrationBeat],
    beats: list[ReviewBeat],
    outline,
    film_map,
    glossary: list[dict],
    char_targets: list[int],
    style_config: StyleConfig,
    style_guide: str,
    logger: logging.Logger,
    max_iterations: int,
) -> tuple[list[NarrationBeat], list[ReviewBeat], list[dict], int, list[str]]:
    if cache.has("narration_style_checked.json") and cache.has("style_qa.json"):
        logger.info("[2/4] Using cached narration_style_checked.json")
        cached_narration = [NarrationBeat.model_validate(item) for item in cache.read_json("narration_style_checked.json")]
        cached_beats = derive_review_beats(outline=outline, narration=cached_narration, film_map=film_map)
        cached_report = cache.read_json("style_qa.json")
        warnings = cached_report.get("readability_warnings", []) if isinstance(cached_report, dict) else []
        rewrites = int(cached_report.get("n_style_rewrites", 0)) if isinstance(cached_report, dict) else 0
        reports = cached_report.get("iterations", []) if isinstance(cached_report, dict) else []
        return cached_narration, cached_beats, reports, rewrites, warnings

    reports: list[dict] = []
    rewritten: set[int] = set()
    consistency_warnings: list[str] = []
    for iteration in range(max_iterations + 1):
        qa = check_readability(beats, style_config)
        reports.append(qa.model_dump_public())
        if qa.passed:
            cache.write_json("style_qa.json", {"iterations": reports, "n_style_rewrites": len(rewritten), "readability_warnings": []})
            cache.write_json("narration_style_checked.json", narration)
            return narration, beats, reports, len(rewritten), consistency_warnings
        if iteration >= max_iterations:
            warnings = [f"style readability issue remains in beat {issue.beat_id}: {issue.type}" for issue in qa.issues]
            cache.write_json("style_qa.json", {"iterations": reports, "n_style_rewrites": len(rewritten), "readability_warnings": warnings})
            cache.write_json("narration_style_checked.json", narration)
            return narration, beats, reports, len(rewritten), warnings + consistency_warnings
        logger.info("[2/4] Style readability QA iteration %d flagged %d issue(s)", iteration, len(qa.issues))
        narration_by_id = {item.beat_id: item for item in narration}
        rewritten_this_round: set[int] = set()
        for issue in qa.issues:
            if issue.beat_id in rewritten_this_round or issue.beat_id >= len(beats):
                continue
            revised = await regenerate_beat(
                client,
                beat=beats[issue.beat_id],
                issue=issue_to_prompt(issue),
                glossary=glossary,
                char_target=char_targets[min(issue.beat_id, len(char_targets) - 1)],
                style_sample=style_guide,
            )
            narration_by_id[revised.beat_id] = revised
            rewritten.add(revised.beat_id)
            rewritten_this_round.add(revised.beat_id)
            cache.write_json(f"style_revisions/{revised.beat_id}.json", revised)
        narration = [narration_by_id[index] for index in sorted(narration_by_id)]
        narration, round_consistency_warnings = apply_narration_consistency(narration, glossary)
        consistency_warnings.extend(round_consistency_warnings)
        beats = derive_review_beats(outline=outline, narration=narration, film_map=film_map)


def ensure_narration_consistency(
    cache: ReviewCache,
    narration: list[NarrationBeat],
    glossary: list[dict],
    logger: logging.Logger,
) -> tuple[list[NarrationBeat], list[str]]:
    if cache.has("narration_consistent.json"):
        logger.info("[2/4] Using cached narration_consistent.json")
        return [NarrationBeat.model_validate(item) for item in cache.read_json("narration_consistent.json")], []
    consistent, consistency_warnings = apply_narration_consistency(narration, glossary)
    if consistency_warnings:
        logger.info("[2/4] Applied narration consistency pass")
    cache.write_json("narration_consistent.json", consistent)
    return consistent, consistency_warnings


async def run_review(args: argparse.Namespace) -> int:
    profile_dir = args.chatgpt_profile_dir.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    session_meta_path = (args.chat_session_meta or (work_dir / "chat_session_meta.json")).expanduser().resolve()
    initial_url, previous_session, session_warnings = resolve_initial_chat_url(session_meta_path, args.chat_session_policy)
    async with PlaywrightChatClient(
        profile_dir,
        headless=args.headless,
        initial_url=initial_url,
        timeout_s=args.reply_timeout_s or 600,
        session_file=args.chatgpt_session_file.expanduser().resolve() if args.chatgpt_session_file else None,
    ) as client:
        await build_review_with_client(args, client)
        session_meta = build_chat_session_meta(
            policy=args.chat_session_policy,
            chat_url=client.current_url,
            profile_dir=profile_dir,
            film_map_path=args.film_map.expanduser().resolve(),
            title=args.chat_title or args.output.stem,
            previous=previous_session,
            warnings=session_warnings,
        )
        save_chat_session(session_meta_path, session_meta)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run_review(args))
    except (ReviewError, ReviewInputError, PlaywrightChatError, ValueError) as exc:
        parser.exit(2, f"review: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
