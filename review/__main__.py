from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from common.schema import ReviewBeat, ReviewMeta, StorySection, VideoProfile, validate_review_intents, validate_review_script, validate_story_map, write_json
from review.budget import allocate_char_targets, compute_budget, estimate_total_chars
from review.cache import ReviewCache
from review.coverage import coverage_ratio
from review.consistency import apply_narration_consistency
from review.inputs import ReviewInputError, load_duration, load_film_map
from review.intent import build_review_intents, story_map_prompt_context
from review.integrity import REVIEW_CACHE_VERSION, ReviewIdentity, build_review_identity
from review.llm_flow import regenerate_beat, request_narration, request_outline, request_qa
from review.micro_beats import split_long_beats
from review.movie_mode import (
    apply_hook_mode,
    check_opening_coherence,
    opening_rewrite_issue,
    replace_narration,
    resolve_content_defaults,
    resolve_target_ratio,
    story_start_from_profile,
)
from review.models import NarrationBeat, OutlineResult, QaResult
from review.openai_chat import FallbackChatClient, OpenAIChatClient, OpenAIChatError
from review.playwright_chat import PlaywrightChatClient, PlaywrightChatError
from review.non_story import drop_non_story_beats
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
    parser.add_argument("--target-ratio", default=str(DEFAULT_TARGET_RATIO), help="Float ratio or auto")
    parser.add_argument("--tts-cps", default=DEFAULT_TTS_CPS, type=float)
    parser.add_argument("--min-coverage", default=DEFAULT_MIN_COVERAGE, type=float)
    parser.add_argument("--max-qa-iterations", default=DEFAULT_MAX_QA_ITERATIONS, type=int)
    parser.add_argument("--max-qa-rewrites-per-iteration", default=6, type=int)
    parser.add_argument("--content-type", default="episode", choices=["episode", "movie"])
    parser.add_argument("--hook-mode", default=None, choices=["cold_open", "setup", "off"])
    parser.add_argument("--opening-coherence-qa", dest="opening_coherence_qa", action="store_true", default=None)
    parser.add_argument("--no-opening-coherence-qa", dest="opening_coherence_qa", action="store_false")
    parser.add_argument("--micro-beats", dest="micro_beats", action="store_true", default=None)
    parser.add_argument("--no-micro-beats", dest="micro_beats", action="store_false")
    parser.add_argument("--target-beat-audio-s", default=12.0, type=float)
    parser.add_argument("--max-beat-audio-s", default=18.0, type=float)
    parser.add_argument("--style-sample", default=None)
    parser.add_argument("--style-preset", default=DEFAULT_STYLE_PRESET)
    parser.add_argument("--style-strength", default=DEFAULT_STYLE_STRENGTH, choices=["medium", "strong"])
    parser.add_argument("--style-qa", action="store_true", default=True)
    parser.add_argument("--no-style-qa", dest="style_qa", action="store_false")
    parser.add_argument("--target-sentence-chars", default=DEFAULT_TARGET_SENTENCE_CHARS, type=int)
    parser.add_argument("--max-sentence-chars", default=DEFAULT_MAX_SENTENCE_CHARS, type=int)
    parser.add_argument("--drop-non-story-beats", action="store_true", default=True)
    parser.add_argument("--no-drop-non-story-beats", dest="drop_non_story_beats", action="store_false")
    parser.add_argument("--non-story-tail-s", default=300.0, type=float)
    parser.add_argument("--video-profile", default=None, type=Path)
    parser.add_argument("--story-map", default=None, type=Path)
    parser.add_argument("--review-intent-output", default=None, type=Path)
    parser.add_argument("--work-dir", default=Path("work/review"), type=Path)
    parser.add_argument("--chatgpt-profile-dir", default=DEFAULT_PROFILE_DIR, type=Path)
    parser.add_argument("--chat-session-policy", default="auto", choices=["auto", "new", "resume"], help="ChatGPT conversation policy for this video/run")
    parser.add_argument("--chat-session-meta", default=None, type=Path, help="Path to chat_session_meta.json; defaults to work-dir/chat_session_meta.json")
    parser.add_argument("--chatgpt-session-file", default=None, type=Path, help="Optional saved ChatGPT cookies/session JSON to restore before opening ChatGPT")
    parser.add_argument("--chat-title", default=None, help="Optional human title saved in chat_session_meta.json")
    parser.add_argument("--reply-timeout-s", default=None, type=int, help="Max seconds to wait for one ChatGPT response")
    parser.add_argument("--llm-backend", default="chatgpt_playwright", choices=["chatgpt_playwright", "openai_api", "off"])
    parser.add_argument("--openai-fallback-model", default=None, help="Optional OpenAI model used only after ChatGPT Playwright fails")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


async def build_review_with_client(args: argparse.Namespace, client) -> tuple[list[ReviewBeat], ReviewMeta]:  # type: ignore[no-untyped-def]
    logger = logging.getLogger("review")
    if not hasattr(args, "drop_non_story_beats"):
        args.drop_non_story_beats = True
    if not hasattr(args, "non_story_tail_s"):
        args.non_story_tail_s = 300.0
    if not hasattr(args, "content_type"):
        args.content_type = "episode"
    if not hasattr(args, "hook_mode"):
        args.hook_mode = None
    if not hasattr(args, "opening_coherence_qa"):
        args.opening_coherence_qa = None
    if not hasattr(args, "max_qa_rewrites_per_iteration"):
        args.max_qa_rewrites_per_iteration = 6
    if not hasattr(args, "video_profile"):
        args.video_profile = None
    if not hasattr(args, "story_map"):
        args.story_map = None
    if not hasattr(args, "review_intent_output"):
        args.review_intent_output = None
    if not hasattr(args, "llm_backend"):
        args.llm_backend = "chatgpt_playwright"
    if not hasattr(args, "micro_beats") or args.micro_beats is None:
        args.micro_beats = False
    if not hasattr(args, "target_beat_audio_s"):
        args.target_beat_audio_s = 12.0
    if not hasattr(args, "max_beat_audio_s"):
        args.max_beat_audio_s = 18.0
    args.hook_mode, args.opening_coherence_qa = resolve_content_defaults(args.content_type, args.hook_mode, args.opening_coherence_qa)
    film_map_path = args.film_map.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()

    try:
        target_ratio_probe = None if str(args.target_ratio).lower() == "auto" else float(args.target_ratio)
    except ValueError as exc:
        raise ReviewError("--target-ratio must be a positive float or auto") from exc
    if target_ratio_probe is not None and target_ratio_probe <= 0:
        raise ReviewError("--target-ratio must be > 0")
    if args.tts_cps <= 0:
        raise ReviewError("--tts-cps must be > 0")
    if not 0 <= args.min_coverage <= 1:
        raise ReviewError("--min-coverage must be between 0 and 1")
    if args.max_qa_iterations < 0:
        raise ReviewError("--max-qa-iterations must be >= 0")
    if args.max_qa_rewrites_per_iteration < 0:
        raise ReviewError("--max-qa-rewrites-per-iteration must be >= 0")
    if args.target_sentence_chars <= 0 or args.max_sentence_chars <= 0:
        raise ReviewError("sentence char limits must be > 0")
    if args.max_sentence_chars < args.target_sentence_chars:
        raise ReviewError("--max-sentence-chars must be >= --target-sentence-chars")
    if args.non_story_tail_s < 0:
        raise ReviewError("--non-story-tail-s must be >= 0")
    if args.target_beat_audio_s <= 0 or args.max_beat_audio_s <= 0:
        raise ReviewError("beat audio limits must be > 0")
    if args.max_beat_audio_s < args.target_beat_audio_s:
        raise ReviewError("--max-beat-audio-s must be >= --target-beat-audio-s")

    film_map = load_film_map(film_map_path)
    duration_s, warnings = load_duration(film_map_path, film_map)
    video_profile = load_video_profile(args.video_profile)
    story_sections = load_story_map(args.story_map, duration_s=duration_s)
    story_start_s = story_start_from_profile(video_profile)
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
    if story_sections and args.content_type == "movie":
        style_guide = style_guide + "\n" + story_map_prompt_context(story_sections)
    auto_duration = resolve_target_ratio(args.target_ratio, content_type=args.content_type, film_map=film_map, duration_s=duration_s)
    target_video_s, char_budget = compute_budget(duration_s, auto_duration.target_ratio, args.tts_cps)

    cache = ReviewCache(work_dir, force=args.force)
    cache.prepare()
    identity: ReviewIdentity = getattr(args, "_review_identity", None) or build_review_identity(
        film_map_path=film_map_path,
        settings=args,
        style_sample_path=style_sample_path,
        story_map_path=args.story_map,
        video_profile_path=args.video_profile,
    )
    cache.reconcile(identity.cache_key)

    if cache.has("outline.json"):
        logger.info("[1/4] Using cached outline.json")
        outline_result = OutlineResult.model_validate(cache.read_json("outline.json"))
        outline_result = apply_hook_mode(outline_result, content_type=args.content_type, hook_mode=args.hook_mode, film_map=film_map, story_start_s=story_start_s)
    else:
        logger.info("[1/4] Requesting outline + glossary")
        outline_result = await request_outline(
            client,
            film_map_view=film_map_view,
            target_video_s=target_video_s,
            char_budget=char_budget,
            min_coverage=args.min_coverage,
            style_sample=style_guide,
            content_type=args.content_type,
            hook_mode=args.hook_mode,
            story_start_s=story_start_s,
        )
        outline_result = apply_hook_mode(outline_result, content_type=args.content_type, hook_mode=args.hook_mode, film_map=film_map, story_start_s=story_start_s)
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
            content_type=args.content_type,
            hook_mode=args.hook_mode,
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
                content_type=args.content_type,
                hook_mode=args.hook_mode,
                story_start_s=story_start_s,
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
        limited_issues = qa.issues[:args.max_qa_rewrites_per_iteration] if args.max_qa_rewrites_per_iteration else []
        if len(qa.issues) > len(limited_issues):
            warnings.append(f"qa rewrite limited to {len(limited_issues)} of {len(qa.issues)} issue(s) in iteration {iteration}")
        for issue in limited_issues:
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

    opening_coherence_report = {"passed": True, "issues": [], "warnings": []}
    n_opening_rewrites = 0
    opening_warnings: list[str] = []
    if args.opening_coherence_qa:
        opening_check = check_opening_coherence(beats, content_type=args.content_type, hook_mode=args.hook_mode, story_start_s=story_start_s)
        opening_coherence_report = {"passed": opening_check.passed, "issues": opening_check.issues, "warnings": opening_check.warnings}
        if not opening_check.passed and beats and args.max_qa_iterations > 0:
            revised = await regenerate_beat(
                client,
                beat=beats[0],
                issue=opening_rewrite_issue(opening_check),
                glossary=outline_result.glossary,
                char_target=char_targets[0] if char_targets else 400,
                style_sample=style_guide,
            )
            narration = replace_narration(narration, revised)
            cache.write_json("opening_coherence_revision.json", revised)
            beats = derive_review_beats(outline=outline_result.outline, narration=narration, film_map=film_map)
            n_opening_rewrites = 1
            opening_check = check_opening_coherence(beats, content_type=args.content_type, hook_mode=args.hook_mode, story_start_s=story_start_s)
            opening_coherence_report = {"passed": opening_check.passed, "issues": opening_check.issues, "warnings": opening_check.warnings}
        opening_warnings = opening_coherence_report["warnings"]
        warnings.extend(opening_warnings)
        cache.write_json("opening_coherence.json", opening_coherence_report)

    pre_story_dropped: list[int] = []
    if args.content_type == "movie" and story_start_s > 0:
        kept_beats = []
        for beat in beats:
            if not beat.is_hook and beat.src_tc_end <= story_start_s + 1e-6:
                pre_story_dropped.append(beat.beat_id)
            else:
                kept_beats.append(beat)
        if pre_story_dropped:
            beats = [beat.model_copy(update={"beat_id": index}) for index, beat in enumerate(kept_beats)]
            warnings.append(f"dropped {len(pre_story_dropped)} pre-story beat(s) before story_start_s={story_start_s:.1f}: {pre_story_dropped}")
            cache.write_json("pre_story_beats.json", {"dropped_beat_ids": pre_story_dropped, "story_start_s": story_start_s})

    micro_report = None
    if args.micro_beats:
        beats, micro_report = split_long_beats(
            beats,
            film_map,
            max_audio_s=args.max_beat_audio_s,
            target_audio_s=args.target_beat_audio_s,
            tts_cps=args.tts_cps,
            enabled=True,
        )
        if micro_report.warnings:
            warnings.extend(micro_report.warnings)
            cache.write_json("micro_beats.json", {
                "n_split_beats": micro_report.n_split_beats,
                "split_beat_ids": micro_report.split_beat_ids,
                "warnings": micro_report.warnings,
                "target_beat_audio_s": args.target_beat_audio_s,
                "max_beat_audio_s": args.max_beat_audio_s,
            })

    non_story_report = None
    if args.drop_non_story_beats:
        beats, non_story_report = drop_non_story_beats(beats, film_map, duration_s=duration_s, tail_s=args.non_story_tail_s)
        cache.write_json("non_story_beats.json", {
            "dropped_beat_ids": non_story_report.dropped_beat_ids,
            "warnings": non_story_report.warnings,
            "decisions": [decision.__dict__ for decision in non_story_report.decisions],
            "tail_s": args.non_story_tail_s,
        })
        warnings.extend(non_story_report.warnings)

    coverage_pct = coverage_ratio(beats, len(film_map))
    if coverage_pct < args.min_coverage:
        warnings.append(f"Coverage {coverage_pct:.3f} is below min_coverage {args.min_coverage:.3f}")
    validate_review_script(beats, film_map)
    if story_sections:
        review_intents = build_review_intents(beats, story_sections)
    else:
        review_intents = build_review_intents(beats, [])
    validate_review_intents(review_intents, beats)
    intent_output = args.review_intent_output.expanduser().resolve() if args.review_intent_output else output_path.with_name("review_script.intent.json")

    meta = ReviewMeta(
        glossary=outline_result.glossary,
        target_video_s=target_video_s,
        char_budget=char_budget,
        est_total_chars=estimate_total_chars(beats),
        coverage_pct=coverage_pct,
        qa_report=qa_report,
        n_qa_iterations=n_qa_iterations,
        model_versions={"llm": args.llm_backend},
        llm_backend=args.llm_backend,
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
        n_non_story_beats_dropped=len(non_story_report.dropped_beat_ids) if non_story_report else 0,
        dropped_beat_ids=non_story_report.dropped_beat_ids if non_story_report else [],
        non_story_filter_warnings=non_story_report.warnings if non_story_report else [],
        content_type=args.content_type,
        hook_mode=args.hook_mode,
        target_ratio_mode=auto_duration.target_ratio_mode,
        auto_target_ratio=auto_duration.target_ratio if auto_duration.target_ratio_mode == "auto" else None,
        complexity_score=auto_duration.complexity_score,
        opening_coherence_report=opening_coherence_report,
        n_opening_rewrites=n_opening_rewrites,
        opening_warnings=opening_warnings,
        qa_rewrite_limited=any("qa rewrite limited" in warning for warning in warnings),
        video_profile_path=str(args.video_profile) if args.video_profile else None,
        story_start_s=story_start_s,
        pre_story_dropped_beat_ids=pre_story_dropped,
        micro_beats_enabled=args.micro_beats,
        target_beat_audio_s=args.target_beat_audio_s,
        max_beat_audio_s=args.max_beat_audio_s,
        n_micro_beats_split=micro_report.n_split_beats if micro_report else 0,
        micro_beat_split_ids=micro_report.split_beat_ids if micro_report else [],
        micro_beat_warnings=micro_report.warnings if micro_report else [],
        film_map_hash=identity.film_map_hash,
        film_map_meta_hash=identity.film_map_meta_hash,
        story_map_hash=identity.story_map_hash,
        video_profile_hash=identity.video_profile_hash,
        config_hash=identity.config_hash,
        cache_version=REVIEW_CACHE_VERSION,
    )
    write_json(output_path, beats)
    write_json(intent_output, review_intents)
    write_json(output_path.with_name(f"{output_path.stem}.meta.json"), meta)
    return beats, meta



def load_story_map(path: Path | None, *, duration_s: float) -> list[StorySection]:
    if path is None:
        return []
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ReviewError(f"story map does not exist: {resolved}")
    sections = [StorySection.model_validate(item) for item in __import__("json").loads(resolved.read_text(encoding="utf-8"))]
    return validate_story_map(sections, duration=duration_s)

def load_video_profile(path: Path | None) -> VideoProfile | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ReviewError(f"video profile does not exist: {resolved}")
    return VideoProfile.model_validate_json(resolved.read_text(encoding="utf-8"))


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
    args.hook_mode, args.opening_coherence_qa = resolve_content_defaults(
        args.content_type,
        args.hook_mode,
        args.opening_coherence_qa,
    )
    if args.micro_beats is None:
        args.micro_beats = False
    style_sample_path = Path(args.style_sample).expanduser().resolve() if args.style_sample else DEFAULT_STYLE_SAMPLE
    identity = build_review_identity(
        film_map_path=args.film_map,
        settings=args,
        style_sample_path=style_sample_path,
        story_map_path=args.story_map,
        video_profile_path=args.video_profile,
    )
    setattr(args, "_review_identity", identity)
    session_meta_path = (args.chat_session_meta or (work_dir / "chat_session_meta.json")).expanduser().resolve()
    initial_url, previous_session, session_warnings = resolve_initial_chat_url(
        session_meta_path,
        args.chat_session_policy,
        identity.core_input_hash,
    )
    async with PlaywrightChatClient(
        profile_dir,
        headless=args.headless,
        initial_url=initial_url,
        timeout_s=args.reply_timeout_s or 600,
        session_file=args.chatgpt_session_file.expanduser().resolve() if args.chatgpt_session_file else None,
    ) as client:
        review_client = client
        fallback_client = None
        if args.openai_fallback_model:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ReviewError("OPENAI_API_KEY is required when --openai-fallback-model is configured")
            fallback_client = FallbackChatClient(
                client,
                OpenAIChatClient(
                    api_key,
                    model=args.openai_fallback_model,
                    timeout_s=args.reply_timeout_s or 300,
                ),
            )
            review_client = fallback_client
        await build_review_with_client(args, review_client)
        if fallback_client is not None:
            write_json(work_dir / "openai_usage.json", fallback_client.usage_summary())
        session_meta = build_chat_session_meta(
            policy=args.chat_session_policy,
            chat_url=client.current_url,
            profile_dir=profile_dir,
            film_map_path=args.film_map.expanduser().resolve(),
            title=args.chat_title or args.output.stem,
            previous=previous_session,
            warnings=session_warnings,
            core_input_hash=identity.core_input_hash,
        )
        save_chat_session(session_meta_path, session_meta)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run_review(args))
    except (ReviewError, ReviewInputError, PlaywrightChatError, OpenAIChatError, OSError, ValueError) as exc:
        parser.exit(2, f"review: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
