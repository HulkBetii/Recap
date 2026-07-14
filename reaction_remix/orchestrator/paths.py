from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReactionRunPaths:
    run_dir: Path
    work_dir: Path
    logs_dir: Path
    reaction_source: Path
    reaction_transcript: Path
    shots: Path
    audio_assets: Path
    reaction_blocks: Path
    blocks_review_html: Path
    remix_plan: Path
    commentary_script: Path
    commentary_audio: Path
    commentary_fit_requests: Path
    remix_edl: Path
    repair_requests: Path
    accepted_repair_dir: Path
    accepted_repair_ledger: Path
    output_video: Path
    render_timeline: Path
    render_command_manifest: Path
    render_meta: Path
    remix_qa: Path
    qa_review_html: Path
    summary: Path

    def stage_work_dir(self, stage: str) -> Path:
        return self.work_dir / stage


def build_paths(run_dir: Path) -> ReactionRunPaths:
    resolved = run_dir.expanduser().resolve()
    work = resolved / "work"
    return ReactionRunPaths(
        run_dir=resolved,
        work_dir=work,
        logs_dir=resolved / "logs",
        reaction_source=resolved / "reaction_source.json",
        reaction_transcript=resolved / "reaction_transcript.json",
        shots=resolved / "shots.json",
        audio_assets=resolved / "audio_assets.json",
        reaction_blocks=resolved / "reaction_blocks.json",
        blocks_review_html=resolved / "reaction_blocks.review.html",
        remix_plan=resolved / "remix_plan.json",
        commentary_script=resolved / "commentary_script.json",
        commentary_audio=resolved / "commentary_audio.json",
        commentary_fit_requests=resolved / "commentary_fit_requests.json",
        remix_edl=resolved / "remix_edl.json",
        repair_requests=resolved / "remix_repair_requests.json",
        accepted_repair_dir=work / "orchestrator" / "accepted_repairs",
        accepted_repair_ledger=work / "orchestrator" / "accepted_repair_ledger.json",
        output_video=resolved / "reaction_remix.mp4",
        render_timeline=resolved / "render.timeline.json",
        render_command_manifest=resolved / "render.command-manifest.json",
        render_meta=resolved / "render.meta.json",
        remix_qa=resolved / "remix_qa.json",
        qa_review_html=resolved / "remix.review.html",
        summary=resolved / "summary.json",
    )


def primary_outputs(paths: ReactionRunPaths, stage: str) -> tuple[Path, ...]:
    return {
        "probe": (paths.reaction_source,),
        "analyze": (paths.reaction_transcript,),
        "shots": (paths.shots,),
        "stems": (paths.audio_assets,),
        "segment": (paths.reaction_blocks, paths.blocks_review_html),
        "plan": (paths.remix_plan,),
        "write": (paths.commentary_script,),
        "tts": (paths.commentary_audio, paths.commentary_fit_requests),
        "compose": (paths.remix_edl,),
        "render": (
            paths.output_video,
            paths.render_timeline,
            paths.render_command_manifest,
            paths.render_meta,
        ),
        "qa": (paths.remix_qa,),
    }[stage]
