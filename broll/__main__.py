from __future__ import annotations

import argparse
import logging
from pathlib import Path

from common.media import require_ffmpeg
from broll.apply import apply_broll_plan
from broll.planner import build_broll_plan


class BrollError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optional broll stage: plan/apply AI image Ken Burns replacements.")
    parser.add_argument("--mode", choices=["plan", "apply"], required=True)
    parser.add_argument("--edl", type=Path, required=True)
    parser.add_argument("--edl-qa", type=Path, default=None)
    parser.add_argument("--edl-sync-qa", type=Path, default=None)
    parser.add_argument("--review-script", type=Path, default=None)
    parser.add_argument("--review-intent", type=Path, default=None)
    parser.add_argument("--output-plan", type=Path, default=None)
    parser.add_argument("--output-prompts", type=Path, default=None)
    parser.add_argument("--asset-dir", type=Path, default=None)
    parser.add_argument("--clip-dir", type=Path, default=None)
    parser.add_argument("--output-edl", type=Path, default=None)
    parser.add_argument("--output-manifest", type=Path, default=None)
    parser.add_argument("--output-qa", type=Path, default=None)
    parser.add_argument("--max-replacement-ratio", type=float, default=0.30)
    parser.add_argument("--max-broll-per-parent-beat", type=int, default=1)
    parser.add_argument("--exclude-opening-s", type=float, default=5.5)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--work-dir", type=Path, default=None, help="Accepted for orchestrator consistency; broll writes explicit artifact paths.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        edl_path = args.edl.expanduser().resolve()
        if not edl_path.is_file():
            raise BrollError(f"edl file does not exist: {edl_path}")
        plan_path = (args.output_plan or edl_path.with_name("broll_plan.json")).expanduser().resolve()
        prompts_path = (args.output_prompts or edl_path.with_name("broll_prompts.jsonl")).expanduser().resolve()
        if args.mode == "plan":
            build_broll_plan(
                edl_path=edl_path,
                qa_path=args.edl_qa.expanduser().resolve() if args.edl_qa else None,
                sync_qa_path=args.edl_sync_qa.expanduser().resolve() if args.edl_sync_qa else None,
                review_script_path=args.review_script.expanduser().resolve() if args.review_script else None,
                review_intent_path=args.review_intent.expanduser().resolve() if args.review_intent else None,
                output_plan_path=plan_path,
                output_prompts_path=prompts_path,
                max_replacement_ratio=args.max_replacement_ratio,
                max_broll_per_parent_beat=args.max_broll_per_parent_beat,
                exclude_opening_s=args.exclude_opening_s,
            )
            return 0
        require_ffmpeg()
        if not plan_path.is_file():
            raise BrollError(f"broll plan does not exist: {plan_path}")
        apply_broll_plan(
            edl_path=edl_path,
            plan_path=plan_path,
            asset_dir=(args.asset_dir or edl_path.with_name("broll_assets")).expanduser().resolve(),
            clip_dir=(args.clip_dir or edl_path.with_name("broll_clips")).expanduser().resolve(),
            output_edl_path=(args.output_edl or edl_path.with_name("edl.broll.json")).expanduser().resolve(),
            output_manifest_path=(args.output_manifest or edl_path.with_name("broll_manifest.json")).expanduser().resolve(),
            output_qa_path=(args.output_qa or edl_path.with_name("broll.qa.json")).expanduser().resolve(),
            width=args.width,
            height=args.height,
            fps=args.fps,
            crf=args.crf,
            encoder_preset=args.preset,
            force=args.force,
        )
        return 0
    except Exception as exc:
        logging.error("broll failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

