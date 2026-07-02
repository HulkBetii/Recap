from __future__ import annotations

import argparse
import json
from pathlib import Path

from ingest.__main__ import load_transcript
from ingest.cache import StageCache
from orchestrator.config import load_config
from orchestrator.graph import build_paths
from orchestrator.runner import build_command


def test_load_transcript_manual_writes_new_cache_artifacts(tmp_path: Path) -> None:
    transcript = tmp_path / "sample.md"
    transcript.write_text("- [00:00] 첫 문장입니다. 두 번째 문장입니다.\n", encoding="utf-8")
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    args = argparse.Namespace(
        asr_provider="manual",
        transcript_input=transcript,
        aligner="none",
        timecode_quality="approximate",
        max_segment_s=30.0,
        whisper_model="large-v3",
        device="cpu",
        vad_filter=True,
    )
    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass
    segments, quality = load_transcript(cache, tmp_path / "audio.wav", 10.0, args, Logger())
    assert segments[0].tc_end == 10.0
    assert quality.approximate_timecodes is True
    assert cache.has("transcript_text.json")
    assert cache.has("transcript_aligned.json")
    assert cache.has("transcript_quality.json")


def test_orchestrator_passes_asr_options_to_ingest(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"asr_provider": "manual", "transcript_input": "sample.md", "timecode_quality": "approximate", "vad_filter": False}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert "--asr-provider" in command
    assert command[command.index("--asr-provider") + 1] == "manual"
    assert "--transcript-input" in command
    assert "--timecode-quality" in command
    assert "--no-vad-filter" in command


def test_orchestrator_passes_hybrid_alignment_options(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"asr_provider": "openai-gpt4o-hybrid", "aligner": "whisperx", "openai_chunk_s": 15, "alignment_device": "cuda"}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--asr-provider") + 1] == "openai-gpt4o-hybrid"
    assert command[command.index("--aligner") + 1] == "whisperx"
    assert command[command.index("--openai-chunk-s") + 1] == "15"
    assert command[command.index("--alignment-device") + 1] == "cuda"

def test_orchestrator_passes_transcript_correction_options(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"transcript_correction": "glossary", "glossary": "glossary.yaml", "correction_model": "gpt-4.1-mini"}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--transcript-correction") + 1] == "glossary"
    assert command[command.index("--glossary") + 1] == "glossary.yaml"
    assert command[command.index("--correction-model") + 1] == "gpt-4.1-mini"

def test_orchestrator_passes_intro_language_filter_option(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"drop_non_korean_intro_s": 45}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--drop-non-korean-intro-s") + 1] == "45"


def test_orchestrator_passes_max_visual_gap_option(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"max_visual_gap_s": 12}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--max-visual-gap-s") + 1] == "12"
