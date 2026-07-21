from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from common.runtime import CHATGPT_PLAYWRIGHT_PROFILE_DIR
from common.schema import (
    SeasonTargetPlan,
    SeriesChapter,
    SeriesComposerQa,
    SeriesEventBank,
    SeriesReviewBeat,
    SeriesReviewMeta,
    validate_series_review_script,
    write_json,
)
from review.playwright_chat import PlaywrightChatClient, PlaywrightChatError
from series_composer.builder import (
    build_event_bank,
    build_series_arc_plan,
    build_series_chapters,
    build_series_composer_qa,
    compose_with_client,
    to_tts_review_script,
)


class SeriesComposerError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compose one recap script from multiple episode artifacts.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--episode-run-dir", action="append", default=[], help="Episode artifact dir as episode_key=path")
    parser.add_argument("--output-event-bank", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="series_review_script.json")
    parser.add_argument("--output-tts-script", required=True, type=Path)
    parser.add_argument("--output-chapters", default=None, type=Path)
    parser.add_argument("--output-arc-plan", default=None, type=Path)
    parser.add_argument("--output-qa", default=None, type=Path)
    parser.add_argument("--output-meta", default=None, type=Path)
    parser.add_argument("--format", choices=["compact", "episode_chaptered", "episode_arc_chaptered"], default="compact")
    parser.add_argument("--detail-level", choices=["standard", "detailed"], default="standard")
    parser.add_argument("--tts-cps", default=15.0, type=float)
    parser.add_argument("--target-total-min-s", default=2100.0, type=float)
    parser.add_argument("--target-total-max-s", default=2700.0, type=float)
    parser.add_argument("--target-total-hard-cap-s", default=3000.0, type=float)
    parser.add_argument("--episode-min-s", default=90.0, type=float)
    parser.add_argument("--episode-normal-s", default=180.0, type=float)
    parser.add_argument("--episode-high-s", default=300.0, type=float)
    parser.add_argument("--arc-size", default=3, type=int)
    parser.add_argument(
        "--mode-target-ratio",
        action="append",
        default=[],
        help="Override target contribution ratio as recap_mode=float, e.g. full=0.12",
    )
    parser.add_argument("--llm-backend", choices=["chatgpt_playwright"], default="chatgpt_playwright")
    parser.add_argument("--chatgpt-profile-dir", type=Path, default=CHATGPT_PLAYWRIGHT_PROFILE_DIR)
    parser.add_argument("--reply-timeout-s", default=600, type=int)
    parser.add_argument("--playwright-max-attempts", default=2, type=int)
    parser.add_argument("--playwright-recovery-timeout-s", default=60, type=int)
    parser.add_argument("--qa-max-revisions", default=1, type=int)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--work-dir", default=Path("work") / "series_composer", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def parse_episode_run_dirs(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise SeriesComposerError("--episode-run-dir must use episode_key=path")
        key, raw_path = value.split("=", 1)
        key = key.strip()
        if not key:
            raise SeriesComposerError("--episode-run-dir episode_key cannot be empty")
        if key in result:
            raise SeriesComposerError(f"duplicate episode run dir for {key}")
        result[key] = Path(raw_path).expanduser().resolve()
    if not result:
        raise SeriesComposerError("at least one --episode-run-dir is required")
    return result

def parse_mode_target_ratios(values: list[str]) -> dict[str, float]:
    allowed = {"full", "quick", "merge", "skip"}
    result: dict[str, float] = {}
    for value in values:
        if "=" not in value:
            raise SeriesComposerError("--mode-target-ratio must use recap_mode=float")
        mode, raw_ratio = value.split("=", 1)
        mode = mode.strip()
        if mode not in allowed:
            raise SeriesComposerError(f"unknown recap mode for --mode-target-ratio: {mode}")
        try:
            ratio = float(raw_ratio)
        except ValueError as exc:
            raise SeriesComposerError(f"invalid target ratio for {mode}: {raw_ratio}") from exc
        if ratio < 0:
            raise SeriesComposerError("--mode-target-ratio values must be >= 0")
        result[mode] = ratio
    return result


def profile_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved != CHATGPT_PLAYWRIGHT_PROFILE_DIR.resolve():
        raise SeriesComposerError(f"ChatGPT profile is locked to {CHATGPT_PLAYWRIGHT_PROFILE_DIR}")
    return resolved


def outputs_current(args: argparse.Namespace) -> bool:
    paths = [
        args.output_event_bank,
        args.output,
        args.output_tts_script,
        args.output_chapters or args.output.with_name("series_chapters.json"),
        args.output_arc_plan or args.output.with_name("series_arc_plan.json"),
        args.output_qa or args.output.with_name("series_composer.qa.json"),
        args.output_meta or args.output.with_name("series_review_script.meta.json"),
    ]
    if not all(path.is_file() for path in paths):
        return False
    SeriesEventBank.model_validate_json(args.output_event_bank.read_text(encoding="utf-8"))
    beats = [SeriesReviewBeat.model_validate(item) for item in json.loads(args.output.read_text(encoding="utf-8"))]
    validate_series_review_script(beats)
    [SeriesChapter.model_validate(item) for item in json.loads(paths[3].read_text(encoding="utf-8"))]
    SeasonTargetPlan.model_validate_json(paths[4].read_text(encoding="utf-8"))
    SeriesComposerQa.model_validate_json(paths[5].read_text(encoding="utf-8"))
    SeriesReviewMeta.model_validate_json(paths[-1].read_text(encoding="utf-8"))
    return True


async def run_composer_async(args: argparse.Namespace) -> int:
    if args.tts_cps <= 0:
        raise SeriesComposerError("--tts-cps must be > 0")
    if args.qa_max_revisions < 0:
        raise SeriesComposerError("--qa-max-revisions must be >= 0")
    if args.arc_size <= 0:
        raise SeriesComposerError("--arc-size must be > 0")
    args.output = args.output.expanduser().resolve()
    args.output_event_bank = args.output_event_bank.expanduser().resolve()
    args.output_tts_script = args.output_tts_script.expanduser().resolve()
    args.output_chapters = (args.output_chapters or args.output.with_name("series_chapters.json")).expanduser().resolve()
    args.output_arc_plan = (args.output_arc_plan or args.output.with_name("series_arc_plan.json")).expanduser().resolve()
    args.output_qa = (args.output_qa or args.output.with_name("series_composer.qa.json")).expanduser().resolve()
    args.output_meta = (args.output_meta or args.output.with_name("series_review_script.meta.json")).expanduser().resolve()
    if not args.force and outputs_current(args):
        logging.info("Using existing series composer outputs")
        return 0

    episode_run_dirs = parse_episode_run_dirs(args.episode_run_dir)
    bank = build_event_bank(
        manifest_path=args.manifest.expanduser().resolve(),
        episode_run_dirs=episode_run_dirs,
        tts_cps=args.tts_cps,
        mode_target_ratios=parse_mode_target_ratios(args.mode_target_ratio),
        recap_format=args.format,
        detail_level=args.detail_level,
        target_total_min_s=args.target_total_min_s,
        target_total_max_s=args.target_total_max_s,
        target_total_hard_cap_s=args.target_total_hard_cap_s,
        episode_min_s=args.episode_min_s,
        episode_normal_s=args.episode_normal_s,
        episode_high_s=args.episode_high_s,
        arc_size=args.arc_size,
    )
    async with PlaywrightChatClient(
        profile_dir(args.chatgpt_profile_dir),
        headless=args.headless,
        timeout_s=args.reply_timeout_s,
        max_attempts=args.playwright_max_attempts,
        recovery_timeout_s=args.playwright_recovery_timeout_s,
    ) as client:
        beats, meta = await compose_with_client(client, bank, qa_max_revisions=args.qa_max_revisions)
    write_json(args.output_event_bank, bank)
    write_json(args.output, beats)
    write_json(args.output_tts_script, to_tts_review_script(beats))
    write_json(args.output_chapters, build_series_chapters(beats, bank))
    write_json(args.output_arc_plan, build_series_arc_plan(bank))
    write_json(args.output_qa, build_series_composer_qa(bank=bank, meta=meta, tts_cps=args.tts_cps))
    write_json(args.output_meta, meta)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run_composer_async(args))
    except (SeriesComposerError, PlaywrightChatError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"series_composer: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
