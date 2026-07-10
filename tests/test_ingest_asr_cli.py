from __future__ import annotations

import argparse
import json
from pathlib import Path

from ingest.__main__ import load_transcript, load_translations, load_vision
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

def test_load_translations_can_keep_vietnamese_source_text(tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    segment = __import__("common.schema", fromlist=["TranscriptSegment"]).TranscriptSegment(id=0, tc_start=0, tc_end=2, ko="Xin chào mọi người")
    class Client:
        def translate_segments(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("translation should be skipped")
    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass
    translated, warnings = load_translations(cache, [segment], Client(), Logger(), translate_mode="none")
    assert warnings == 0
    assert translated[0].ko == "Xin chào mọi người"
    assert translated[0].en == "Xin chào mọi người"

def test_load_translations_none_does_not_need_openai_client(tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    segment = __import__("common.schema", fromlist=["TranscriptSegment"]).TranscriptSegment(id=0, tc_start=0, tc_end=2, ko="Xin chào mọi người")
    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass
    translated, warnings = load_translations(cache, [segment], None, Logger(), translate_mode="none")
    assert warnings == 0
    assert translated[0].en == "Xin chào mọi người"

def test_load_vision_no_selected_gaps_does_not_need_openai_client(tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    translated_segment = __import__("common.schema", fromlist=["TranslatedSegment"]).TranslatedSegment(
        id=0,
        tc_start=0,
        tc_end=10,
        ko="Xin chào mọi người",
        en="Xin chào mọi người",
    )
    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass
    vision, warnings = load_vision(
        cache=cache,
        input_path=tmp_path / "film.mp4",
        translated=[translated_segment],
        duration=10,
        gap_threshold=4,
        max_vision_frames=0,
        max_visual_gap_s=20,
        client=None,
        logger=Logger(),
    )
    assert vision == []
    assert warnings == 0
    assert cache.has("vision.json")

def test_orchestrator_passes_vietnamese_source_options_to_ingest(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"source_language": "vi", "translate_mode": "none"}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--source-language") + 1] == "vi"
    assert command[command.index("--translate-mode") + 1] == "none"



def test_vietnamese_stable_config_uses_whisperx_alignment() -> None:
    config = load_config(Path("config.vi.stable.yaml"))
    command = build_command("ingest", build_paths(Path("runs/test")), Path("film.mp4"), config, force=False, python_exe="python")
    assert command[command.index("--source-language") + 1] == "vi"
    assert command[command.index("--translate-mode") + 1] == "none"
    assert command[command.index("--aligner") + 1] == "whisperx"
    assert command[command.index("--alignment-device") + 1] == "cuda"

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

def test_orchestrator_passes_review_chat_session_options(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"review": {"chat_session_policy": "new", "chat_session_meta": "session.json", "chat_title": "ep01"}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("review", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--chat-session-policy") + 1] == "new"
    assert command[command.index("--chat-session-meta") + 1] == "session.json"
    assert command[command.index("--chat-title") + 1] == "ep01"

def test_orchestrator_passes_drop_visual_before_option(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"drop_visual_before_s": 120}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--drop-visual-before-s") + 1] == "120"
