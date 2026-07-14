from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from common.integrity import atomic_write_json, file_hash, media_identity_hash, stable_hash
from common.media import require_ffmpeg
from common.schema import (
    CommentaryAudio,
    CommentaryScript,
    ReactionBlocks,
    ReactionSource,
    ReactionStageMeta,
    ReactionTranscript,
    RemixCommandManifest,
    RemixEdl,
    RemixPlan,
    RemixQa,
    RemixRenderMeta,
    RemixRenderTimeline,
    RemixRepairRequests,
    validate_commentary_audio,
    validate_commentary_script,
    validate_reaction_blocks,
    validate_reaction_transcript,
    validate_remix_command_manifest,
    validate_remix_edl,
    validate_remix_plan,
    validate_remix_qa,
    validate_remix_render_timeline,
)

from reaction_remix.qa.checks import (
    RemixQaError,
    boundary_audio_defects,
    commentary_leakage_placement_ids,
    commentary_peak_dbfs,
    commentary_provenance,
    declared_reaction_mismatch_placement_ids,
    declared_reaction_mismatches,
    decoded_media_counts,
    decoded_video_frame_count,
    full_decode_ok,
    narrator_phrase_leakage_placement_ids,
    probe_output,
    program_peak_dbfs,
    measure_reaction_preservation,
    visual_operation_counts,
    write_boundary_frames,
)
from reaction_remix.qa.report_html import write_review_html


def protected_narrator_overlap_block_ids(
    transcript: ReactionTranscript,
    blocks: ReactionBlocks,
) -> list[str]:
    narrator_speaker_id = transcript.narrator_speaker_id
    if narrator_speaker_id is None:
        return []
    return sorted(
        block.block_id
        for block in blocks.blocks
        if block.kind in {"mixed", "unknown"} and narrator_speaker_id in block.speaker_ids
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reaction-remix R8: deterministic structural and media QA")
    parser.add_argument("--film", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--blocks", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--commentary-script", required=True, type=Path)
    parser.add_argument("--commentary-audio", required=True, type=Path)
    parser.add_argument("--edl", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--render-meta", required=True, type=Path)
    parser.add_argument("--render-timeline", required=True, type=Path)
    parser.add_argument("--command-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--review-html", type=Path)
    parser.add_argument("--qa-dir", type=Path)
    parser.add_argument("--repair-requests", type=Path)
    parser.add_argument("--min-output-ratio", default=0.8, type=float)
    parser.add_argument("--preferred-min-ratio", default=0.85, type=float)
    parser.add_argument("--preferred-max-ratio", default=0.9, type=float)
    parser.add_argument("--min-correlation", default=0.98, type=float)
    parser.add_argument("--min-frame-similarity", default=0.995, type=float)
    parser.add_argument("--min-tts-asr-match", default=0.9, type=float)
    parser.add_argument(
        "--max-samples",
        default=8,
        type=int,
        help="Deprecated compatibility option; the hard preservation gate always checks every protected placement",
    )
    parser.add_argument("--leakage-asr-model", default="large-v3")
    parser.add_argument("--leakage-asr-device", default="cuda")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def run_qa(args: argparse.Namespace) -> int:
    require_ffmpeg()
    film_path = args.film.expanduser().resolve()
    source_path = args.source.expanduser().resolve()
    transcript_path = args.transcript.expanduser().resolve()
    blocks_path = args.blocks.expanduser().resolve()
    plan_path = args.plan.expanduser().resolve()
    script_path = args.commentary_script.expanduser().resolve()
    commentary_audio_path = args.commentary_audio.expanduser().resolve()
    edl_path = args.edl.expanduser().resolve()
    video_path = args.video.expanduser().resolve()
    timeline_path = args.render_timeline.expanduser().resolve()
    manifest_path = args.command_manifest.expanduser().resolve()
    meta_path = args.render_meta.expanduser().resolve()
    for path in (
        film_path,
        source_path,
        transcript_path,
        blocks_path,
        plan_path,
        script_path,
        commentary_audio_path,
        edl_path,
        video_path,
        timeline_path,
        manifest_path,
        meta_path,
    ):
        if not path.is_file():
            raise RemixQaError(f"required QA input does not exist: {path}")
    source = ReactionSource.model_validate_json(source_path.read_text(encoding="utf-8"))
    transcript = ReactionTranscript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
    blocks = ReactionBlocks.model_validate_json(blocks_path.read_text(encoding="utf-8"))
    plan = RemixPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    script = CommentaryScript.model_validate_json(script_path.read_text(encoding="utf-8"))
    commentary_audio = CommentaryAudio.model_validate_json(commentary_audio_path.read_text(encoding="utf-8"))
    edl = RemixEdl.model_validate_json(edl_path.read_text(encoding="utf-8"))
    timeline = RemixRenderTimeline.model_validate_json(timeline_path.read_text(encoding="utf-8"))
    manifest = RemixCommandManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    render_meta = RemixRenderMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    if source.input_hash != media_identity_hash(film_path) or edl.source_hash != source.input_hash:
        raise RemixQaError("QA source identity mismatch")
    expected_hashes = {
        "blocks transcript": (blocks.transcript_hash, file_hash(transcript_path)),
        "plan blocks": (plan.blocks_hash, file_hash(blocks_path)),
        "script plan": (script.plan_hash, file_hash(plan_path)),
        "audio script": (commentary_audio.script_hash, file_hash(script_path)),
        "EDL plan": (edl.plan_hash, file_hash(plan_path)),
        "EDL commentary audio": (edl.commentary_audio_hash, file_hash(commentary_audio_path)),
    }
    mismatched = [name for name, (declared, actual) in expected_hashes.items() if declared != actual]
    if mismatched:
        raise RemixQaError(f"QA provenance hash mismatch: {', '.join(mismatched)}")
    if timeline.edl_hash != file_hash(edl_path) or manifest.edl_hash != file_hash(edl_path):
        raise RemixQaError("render provenance does not match remix_edl.json")
    if render_meta.timeline_hash != file_hash(timeline_path) or render_meta.command_manifest_hash != file_hash(manifest_path):
        raise RemixQaError("render sidecar integrity mismatch")
    validate_reaction_transcript(transcript)
    validate_reaction_blocks(blocks, transcript)
    validate_remix_plan(plan, blocks)
    validate_commentary_script(script, plan)
    validate_commentary_audio(commentary_audio, script)
    validate_remix_edl(edl, source=source)
    validate_remix_render_timeline(timeline, edl=edl)
    validate_remix_command_manifest(manifest)

    output_probe = probe_output(video_path)
    decode_ok = full_decode_ok(video_path)
    actual_frame_count, actual_sample_count = decoded_media_counts(
        video_path,
        audio_channels=source.audio.channels,
    )
    source_frame_count = decoded_video_frame_count(film_path)
    frame_count_delta = actual_frame_count - timeline.total_frames
    sample_count_delta = actual_sample_count - timeline.total_samples
    frame_count_ok = abs(frame_count_delta) <= 1
    sample_tolerance = max(1, round(source.audio.sample_rate * source.video.fps_den / source.video.fps_num))
    sample_count_ok = abs(sample_count_delta) <= sample_tolerance
    output_ratio = output_probe["duration_s"] / source.duration_s
    duration_status = "pass" if args.min_output_ratio <= output_ratio <= 1.0 else "fail"
    warnings: list[str] = []
    protected_overlap_block_ids = protected_narrator_overlap_block_ids(transcript, blocks)
    if protected_overlap_block_ids:
        warnings.append(
            f"{len(protected_overlap_block_ids)} protected mixed/unknown narrator overlap block(s): "
            f"{', '.join(protected_overlap_block_ids)}"
        )
    if duration_status == "pass" and not args.preferred_min_ratio <= output_ratio <= args.preferred_max_ratio:
        warnings.append(f"output ratio {output_ratio:.4f} is outside preferred range")
    if frame_count_ok and frame_count_delta:
        warnings.append(f"decoded video differs from render timeline by {frame_count_delta} frame")
    if sample_count_ok and sample_count_delta:
        warnings.append(
            f"decoded audio differs from render timeline by {sample_count_delta} samples "
            f"within one-frame tolerance ({sample_tolerance})"
        )

    checked, speed_mismatches, gain_mismatches, span_mismatches = declared_reaction_mismatches(edl)
    preservation = measure_reaction_preservation(
        film_path=film_path,
        output_path=video_path,
        edl=edl,
        max_samples=args.max_samples,
        timeline=timeline,
        source_frame_count=source_frame_count,
        output_frame_count=actual_frame_count,
    )
    min_correlation = preservation.min_audio_correlation
    max_drift_ms = preservation.max_av_drift_ms
    min_frame_similarity = preservation.min_frame_similarity
    max_gain_delta_db = preservation.max_gain_delta_db
    qa_dir = (args.qa_dir or args.output.expanduser().resolve().parent / "qa").expanduser().resolve()
    boundary_frames = write_boundary_frames(
        film_path=film_path,
        output_path=video_path,
        edl=edl,
        qa_dir=qa_dir,
        timeline=timeline,
        source_frame_count=source_frame_count,
        output_frame_count=actual_frame_count,
    )
    min_frame_similarity = min(min_frame_similarity, boundary_frames.min_frame_similarity)
    one_frame_ms = 1000.0 * source.video.fps_den / source.video.fps_num
    failed_reaction_placement_ids = sorted(
        set(declared_reaction_mismatch_placement_ids(edl))
        | set(
            boundary_frames.failed_placement_ids(
                min_frame_similarity=args.min_frame_similarity,
            )
        )
        | set(
            preservation.failed_placement_ids(
                min_correlation=args.min_correlation,
                max_lag_ms=one_frame_ms + 1e-3,
                min_frame_similarity=args.min_frame_similarity,
                max_gain_delta_db=0.3,
            )
        )
    )
    reaction_status = "pass"
    if (
        checked == 0
        or speed_mismatches
        or gain_mismatches
        or span_mismatches
        or min_correlation < args.min_correlation
        or max_drift_ms > one_frame_ms + 1e-3
        or min_frame_similarity < args.min_frame_similarity
        or max_gain_delta_db > 0.3
        or plan.retention.unique_reaction_speech_ratio < 0.90
    ):
        reaction_status = "fail"
    if 20.0 < max_drift_ms <= one_frame_ms + 1e-3:
        warnings.append(f"reaction A/V drift {max_drift_ms:.1f}ms exceeds preferred 20ms")

    slots, provider_mismatches, voice_mismatches, min_asr_match = commentary_provenance(commentary_audio)
    correlation_leakage = commentary_leakage_placement_ids(film_path, video_path, edl)
    phrase_leakage = narrator_phrase_leakage_placement_ids(
        output_path=video_path,
        edl=edl,
        transcript=transcript,
        blocks=blocks,
        plan=plan,
        script=script,
        work_dir=qa_dir / "leakage_asr",
        model_name=args.leakage_asr_model,
        device=args.leakage_asr_device,
    )
    leaking_placement_ids = set(correlation_leakage) | set(phrase_leakage)
    placement_by_id = {placement.placement_id: placement for placement in edl.placements}
    slot_by_item_id = {item.item_id: item.slot_id for item in plan.items if item.kind == "commentary_slot"}
    leakage_slot_ids: list[str] = []
    for placement_id in sorted(leaking_placement_ids):
        placement = placement_by_id.get(placement_id)
        slot_id = slot_by_item_id.get(placement.item_id) if placement is not None else None
        if slot_id is None:
            raise RemixQaError(f"could not map leaking commentary placement {placement_id} to a slot")
        leakage_slot_ids.append(slot_id)
    leakage_slot_ids = sorted(set(leakage_slot_ids))
    leakage_count = len(leakage_slot_ids)
    commentary_status = (
        "pass"
        if provider_mismatches == 0
        and voice_mismatches == 0
        and leakage_count == 0
        and min_asr_match >= args.min_tts_asr_match
        else "fail"
    )
    visual_counts = visual_operation_counts(manifest)
    visual_status = "pass" if not any(visual_counts.values()) else "fail"
    silence_count, click_count = boundary_audio_defects(video_path, edl)
    commentary_peak = commentary_peak_dbfs(video_path, edl)
    full_output_peak = program_peak_dbfs(video_path)
    source_peak = program_peak_dbfs(film_path)
    peak_increase = full_output_peak - source_peak
    audio_status = (
        "pass"
        if silence_count == 0
        and click_count == 0
        and (commentary_peak is None or commentary_peak <= -1.2)
        and peak_increase <= 0.3
        else "fail"
    )

    gap_count = 0
    overlap_count = 0
    previous_end = 0.0
    for placement in edl.placements:
        if placement.tl_start > previous_end + 1e-3:
            gap_count += 1
        if placement.tl_start < previous_end - 1e-3:
            overlap_count += 1
        previous_end = placement.tl_end
    profile_ok = (
        output_probe["video_codec"] == "h264"
        and output_probe["audio_codec"] == "aac"
        and output_probe["width"] == source.video.width
        and output_probe["height"] == source.video.height
        and abs(output_probe["fps"] - source.video.fps_num / source.video.fps_den) <= 0.01
        and output_probe["sample_rate"] == source.audio.sample_rate
        and output_probe["channels"] == source.audio.channels
    )
    timeline_status = (
        "pass"
        if decode_ok and profile_ok and frame_count_ok and sample_count_ok and gap_count == 0 and overlap_count == 0
        else "fail"
    )
    statuses = [duration_status, reaction_status, commentary_status, visual_status, audio_status, timeline_status]
    overall = "pass" if all(value == "pass" for value in statuses) else "fail"
    repair_path: Path | None = None
    repair_items: list[dict[str, object]] = []
    if args.repair_requests is not None:
        repair_path = args.repair_requests.expanduser().resolve()
        if not repair_path.is_file():
            raise RemixQaError(f"repair request does not exist: {repair_path}")
        repairs = RemixRepairRequests.model_validate_json(repair_path.read_text(encoding="utf-8"))
        if repairs.source_hash != source.input_hash:
            raise RemixQaError("repair request source hash does not match source")
        repair_items = [
            {
                "kind": item.kind,
                "affected_ids": item.affected_ids,
                "attempt": item.attempt,
                "previous_result": item.reason,
                "new_result": overall,
            }
            for item in repairs.items
        ]
    qa = RemixQa.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "source_hash": source.input_hash,
            "edl_hash": file_hash(edl_path),
            "output_path": video_path.as_posix(),
            "status": overall,
            "duration": {
                "source_s": source.duration_s,
                "output_s": output_probe["duration_s"],
                "output_ratio": output_ratio,
                "hard_min_ratio": args.min_output_ratio,
                "preferred_range": [args.preferred_min_ratio, args.preferred_max_ratio],
                "status": duration_status,
            },
            "reaction_preservation": {
                "placements_checked": checked,
                "speed_mismatches": speed_mismatches,
                "gain_mismatches": gain_mismatches,
                "span_mismatches": span_mismatches,
                "failed_placement_ids": failed_reaction_placement_ids,
                "min_audio_correlation": min_correlation,
                "max_av_drift_ms": max_drift_ms,
                "min_sample_frame_similarity": min_frame_similarity,
                "max_gain_delta_db": max_gain_delta_db,
                "status": reaction_status,
            },
            "commentary": {
                "slots_checked": slots,
                "provider_mismatches": provider_mismatches,
                "voice_mismatches": voice_mismatches,
                "old_narrator_leakage_count": leakage_count,
                "old_narrator_leakage_slot_ids": leakage_slot_ids,
                "protected_narrator_overlap_block_ids": protected_overlap_block_ids,
                "min_asr_text_match": min_asr_match,
                "status": commentary_status,
            },
            "visual_policy": {**visual_counts, "status": visual_status},
            "audio": {
                "unexpected_silence_count": silence_count,
                "boundary_click_count": click_count,
                "max_commentary_true_peak_dbfs": commentary_peak if commentary_peak is not None else -120.0,
                "full_output_true_peak_dbfs": full_output_peak,
                "source_true_peak_dbfs": source_peak,
                "peak_increase_db": peak_increase,
                "status": audio_status,
            },
            "timeline": {
                "gap_count": gap_count,
                "overlap_count": overlap_count,
                "decode_ok": decode_ok and profile_ok,
                "expected_frame_count": timeline.total_frames,
                "actual_frame_count": actual_frame_count,
                "frame_count_delta": frame_count_delta,
                "expected_sample_count": timeline.total_samples,
                "actual_sample_count": actual_sample_count,
                "sample_count_delta": sample_count_delta,
                "status": timeline_status,
            },
            "repairs": repair_items,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": warnings,
        }
    )
    validate_remix_qa(qa)
    output_path = args.output.expanduser().resolve()
    atomic_write_json(output_path, qa.model_dump(mode="json"))
    qa_meta = ReactionStageMeta.model_validate(
        {
            "schema_version": "reaction-remix.v1",
            "stage": "qa",
            "algorithm_version": "reaction-qa-v7",
            "input_hashes": {path.name: file_hash(path) for path in (
                source_path,
                transcript_path,
                blocks_path,
                plan_path,
                script_path,
                commentary_audio_path,
                edl_path,
                video_path,
                timeline_path,
                manifest_path,
                meta_path,
            )},
            "config_hash": stable_hash(
                {
                    "min_output_ratio": args.min_output_ratio,
                    "preferred_range": [args.preferred_min_ratio, args.preferred_max_ratio],
                    "min_correlation": args.min_correlation,
                    "min_frame_similarity": args.min_frame_similarity,
                    "min_tts_asr_match": args.min_tts_asr_match,
                    "leakage_asr_model": args.leakage_asr_model,
                }
            ),
            "output_hashes": {output_path.name: file_hash(output_path)},
            "cache_hits": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "warnings": warnings,
        }
    )
    if repair_path is not None:
        qa_meta = qa_meta.model_copy(
            update={"input_hashes": {**qa_meta.input_hashes, repair_path.name: file_hash(repair_path)}}
        )
    atomic_write_json(output_path.with_name("remix_qa.meta.json"), qa_meta.model_dump(mode="json"))
    if args.review_html:
        write_review_html(args.review_html.expanduser().resolve(), qa)
    return 0 if qa.status == "pass" else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    try:
        return run_qa(args)
    except (RemixQaError, ValueError, OSError) as exc:
        parser.exit(2, f"reaction-remix qa: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
