from __future__ import annotations

import argparse
import logging
from pathlib import Path

from common.integrity import media_identity_hash, stable_hash
from common.media import MediaError, require_ffmpeg
from common.schema import ReactionSource
from reaction_remix._artifacts import load_current_artifact, write_artifact
from reaction_remix.probe.media_probe import ReactionProbeError, probe_reaction_source

PROBE_ALGORITHM_VERSION = "reaction-probe-v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction-remix R0: probe immutable source media")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-dir", default=Path("work/reaction-remix/probe"), type=Path)
    parser.add_argument("--has-burned-in-subtitles", action="store_true", default=True)
    parser.add_argument("--no-burned-in-subtitles", dest="has_burned_in_subtitles", action="store_false")
    parser.add_argument("--soft-subtitle-policy", default="fail", choices=["fail"])
    parser.add_argument("--burned-subtitle-policy", default="preserve", choices=["preserve"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def run_probe(args: argparse.Namespace) -> int:
    require_ffmpeg()
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    meta_path = output_path.with_name("reaction_source.meta.json")
    if not input_path.is_file():
        raise ReactionProbeError(f"input video does not exist: {input_path}")
    input_hashes = {"source_identity": media_identity_hash(input_path)}
    config_hash = stable_hash(
        {
            "soft_subtitle_policy": args.soft_subtitle_policy,
            "burned_subtitle_policy": args.burned_subtitle_policy,
            "has_burned_in_subtitles": args.has_burned_in_subtitles,
        }
    )
    if not args.force:
        cached = load_current_artifact(
            output_path,
            meta_path,
            ReactionSource,
            stage="probe",
            algorithm_version=PROBE_ALGORITHM_VERSION,
            input_hashes=input_hashes,
            config_hash=config_hash,
        )
        if cached is not None:
            logging.getLogger("reaction_remix.probe").info("Probe output is current; skipping")
            return 0
    source = probe_reaction_source(
        input_path,
        has_burned_in_subtitles=args.has_burned_in_subtitles,
    )
    if source.config_hash != config_hash:
        source = source.model_copy(update={"config_hash": config_hash})
    write_artifact(
        output_path,
        meta_path,
        source,
        stage="probe",
        algorithm_version=PROBE_ALGORITHM_VERSION,
        input_hashes=input_hashes,
        config_hash=config_hash,
        warnings=source.warnings,
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_probe(args)
    except (ReactionProbeError, MediaError, OSError, ValueError) as exc:
        parser.exit(2, f"reaction-remix probe: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
