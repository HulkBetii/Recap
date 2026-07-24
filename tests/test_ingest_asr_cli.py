from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from common.schema import IntroDetection, NonStoryRange, TranslatedSegment, TranscriptSegment, VideoProfile, VisionSegment
from ingest.__main__ import (
    IngestError,
    build_parser,
    build_vision_client,
    load_transcript,
    load_translations,
    load_vision,
    run_ingest,
    validate_openai_api_key,
)
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

def test_load_translations_passes_japanese_source_language_to_client(tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    segment = TranslatedSegment(id=0, tc_start=0, tc_end=2, ko="こんにちは", en="placeholder")
    seen = {}

    class Client:
        def translate_segments(self, transcript, logger=None, source_language="ko"):  # type: ignore[no-untyped-def]
            seen["source_language"] = source_language
            return [
                TranslatedSegment(id=item.id, tc_start=item.tc_start, tc_end=item.tc_end, ko=item.ko, en=f"translated-{item.id}")
                for item in transcript
            ], 0

    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass

    translated, warnings = load_translations(cache, [segment.model_copy(update={"en": "こんにちは"})], Client(), Logger(), translate_mode="ja-en", source_language="ja")
    assert seen["source_language"] == "ja"
    assert warnings == 0
    assert translated[0].en == "translated-0"

def test_validate_openai_api_key_rejects_bad_clipboard_values() -> None:
    with pytest.raises(IngestError, match="must start with sk-"):
        validate_openai_api_key("\x16")
    with pytest.raises(IngestError, match="hidden/control"):
        validate_openai_api_key("sk-test\x01")

def test_required_translation_fails_on_placeholder_ratio(tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    segments = [
        TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="こんにちは"),
        TranscriptSegment(id=1, tc_start=1, tc_end=2, ko="行こう"),
    ]

    class Client:
        def translate_segments(self, transcript, logger=None, source_language="ko"):  # type: ignore[no-untyped-def]
            return [
                TranslatedSegment(id=item.id, tc_start=item.tc_start, tc_end=item.tc_end, ko=item.ko, en="[translation unavailable]")
                for item in transcript
            ], len(transcript)

    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass

    with pytest.raises(IngestError, match="translation success ratio"):
        load_translations(
            cache,
            segments,
            Client(),
            Logger(),
            translate_mode="ja-en",
            source_language="ja",
            translation_required=True,
            translation_min_success_ratio=0.95,
        )
    assert not cache.path("translated.json").exists()

def test_build_vision_client_uses_mocked_local_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = object()
    seen = {}

    def fake_qwen(model_name, *, device, resize_long_edge, batch_size):  # type: ignore[no-untyped-def]
        seen.update(
            {
                "model_name": model_name,
                "device": device,
                "resize_long_edge": resize_long_edge,
                "batch_size": batch_size,
            }
        )
        return fake_client

    monkeypatch.setattr("ingest.__main__.LocalQwenVisionClient", fake_qwen)
    args = argparse.Namespace(
        max_vision_frames=30,
        vision_provider="local_qwen2_5_vl",
        vision_model="Qwen/Qwen2.5-VL-7B-Instruct",
        device="cuda",
        vision_resize_long_edge=768,
        vision_batch_size=1,
    )

    class Logger:
        def warning(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass

    client = build_vision_client(args=args, openai_client=None, logger=Logger())

    assert client is fake_client
    assert seen == {
        "model_name": "Qwen/Qwen2.5-VL-7B-Instruct",
        "device": "cuda",
        "resize_long_edge": 768,
        "batch_size": 1,
    }

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

def test_load_vision_excludes_manual_non_story_gaps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    translated = [TranslatedSegment(id=0, tc_start=10, tc_end=20, ko="こんにちは", en="Hello")]
    profile = VideoProfile(
        input_path="anime.mp4",
        duration_s=70,
        intro=IntroDetection(detected=False, confidence=0, reasons=[]),
        non_story_ranges=[
            NonStoryRange(start_s=0, end_s=10, label="opening_theme", confidence=1.0),
            NonStoryRange(start_s=20, end_s=40, label="next_episode_preview", confidence=1.0),
        ],
        classifier="heuristic",
        created_at="2026-07-20T00:00:00Z",
    )
    seen_gaps = []

    def fake_describe_gaps(**kwargs):  # type: ignore[no-untyped-def]
        seen_gaps.extend(kwargs["gaps"])
        return [
            VisionSegment(gap_id=gap.id, tc_start=gap.tc_start, tc_end=gap.tc_end, scene_desc="Story visual.")
            for gap in kwargs["gaps"]
        ], 0

    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr("ingest.__main__.describe_gaps", fake_describe_gaps)
    vision, warnings = load_vision(
        cache=cache,
        input_path=tmp_path / "anime.mp4",
        translated=translated,
        duration=70,
        gap_threshold=4,
        max_vision_frames=10,
        max_visual_gap_s=20,
        client=object(),
        logger=Logger(),
        video_profile=profile,
    )

    assert warnings == 0
    assert [gap.tc_start for gap in seen_gaps] == [40, 60]
    assert [item.tc_start for item in vision] == [40, 60]

def test_load_vision_accepts_local_provider_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    translated = [TranslatedSegment(id=0, tc_start=10, tc_end=20, ko="こんにちは", en="Hello")]

    def fake_describe_gaps(**kwargs):  # type: ignore[no-untyped-def]
        return [
            VisionSegment(gap_id=gap.id, tc_start=gap.tc_start, tc_end=gap.tc_end, scene_desc="A hunter enters a dungeon.")
            for gap in kwargs["gaps"]
        ], 0

    class Logger:
        def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass

    monkeypatch.setattr("ingest.__main__.describe_gaps", fake_describe_gaps)
    vision, warnings = load_vision(
        cache=cache,
        input_path=tmp_path / "anime.mp4",
        translated=translated,
        duration=35,
        gap_threshold=4,
        max_vision_frames=1,
        max_visual_gap_s=20,
        client=object(),
        logger=Logger(),
        vision_provider="local_qwen2_5_vl",
    )

    assert warnings == 0
    assert vision[0].scene_desc == "A hunter enters a dungeon."

def test_orchestrator_passes_vietnamese_source_options_to_ingest(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"ingest": {"source_language": "vi", "translate_mode": "none"}}), encoding="utf-8")
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "film.mp4", config, force=False, python_exe="python")
    assert command[command.index("--source-language") + 1] == "vi"
    assert command[command.index("--translate-mode") + 1] == "none"



def test_ingest_parser_accepts_japanese_translation_mode(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--input",
            str(tmp_path / "anime.mp4"),
            "--output",
            str(tmp_path / "film_map.json"),
            "--source-language",
            "ja",
            "--translate-mode",
            "ja-en",
        ]
    )

    assert args.source_language == "ja"
    assert args.translate_mode == "ja-en"

def test_run_ingest_rejects_invalid_japanese_translation_pair(tmp_path: Path) -> None:
    film = tmp_path / "anime.mp4"
    film.write_bytes(b"film")
    args = build_parser().parse_args(
        [
            "--input",
            str(film),
            "--output",
            str(tmp_path / "film_map.json"),
            "--source-language",
            "ja",
            "--translate-mode",
            "ko-en",
        ]
    )

    with pytest.raises(IngestError, match="translate_mode=ko-en is not valid for source_language=ja"):
        run_ingest(args)

def test_orchestrator_passes_japanese_source_options_to_ingest(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "ingest": {
                    "source_language": "ja",
                    "translate_mode": "ja-en",
                    "translation_required": True,
                    "translation_min_success_ratio": 0.95,
                    "vision_provider": "off",
                    "vision_resize_long_edge": 768,
                    "vision_batch_size": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    command = build_command("ingest", build_paths(tmp_path / "run"), tmp_path / "anime.mp4", config, force=False, python_exe="python")

    assert command[command.index("--source-language") + 1] == "ja"
    assert command[command.index("--translate-mode") + 1] == "ja-en"
    assert "--translation-required" in command
    assert command[command.index("--translation-min-success-ratio") + 1] == "0.95"
    assert command[command.index("--vision-provider") + 1] == "off"
    assert command[command.index("--vision-resize-long-edge") + 1] == "768"
    assert command[command.index("--vision-batch-size") + 1] == "1"

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


def test_orchestrator_passes_video_profile_to_ingest(tmp_path: Path) -> None:
    paths = build_paths(tmp_path / "run")
    paths.run_dir.mkdir(parents=True)
    paths.video_profile.write_text("{}", encoding="utf-8")
    config = load_config(None)

    command = build_command("ingest", paths, tmp_path / "film.mp4", config, force=False, python_exe="python")

    assert command[command.index("--video-profile") + 1] == str(paths.video_profile)
