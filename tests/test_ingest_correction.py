from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.schema import TranscriptQuality, TranscriptSegment
from ingest.__main__ import correct_transcript
from ingest.cache import StageCache
from ingest.correction import apply_glossary_replacements, load_glossary


class Logger:
    def info(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        pass


def test_load_glossary_json_and_apply_replacements(tmp_path: Path) -> None:
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(json.dumps({"replacements": {"문지현": "황준현"}, "names": ["황준현"]}, ensure_ascii=False), encoding="utf-8")
    glossary = load_glossary(glossary_path)
    segments = [TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="문지현이 말했다.")]

    corrected, warnings = apply_glossary_replacements(segments, glossary)

    assert corrected[0].ko == "황준현이 말했다."
    assert any("changed 1" in warning for warning in warnings)


def test_correct_transcript_glossary_writes_cache(tmp_path: Path) -> None:
    glossary_path = tmp_path / "glossary.txt"
    glossary_path.write_text("문지현 => 황준현\n", encoding="utf-8")
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    args = argparse.Namespace(transcript_correction="glossary", glossary=glossary_path, correction_model="gpt-4.1-mini")
    quality = TranscriptQuality(asr_provider="manual", timecode_quality="approximate", approximate_timecodes=True)
    segments = [TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="문지현")]

    corrected, updated = correct_transcript(cache, segments, quality, args, Logger())

    assert corrected[0].ko == "황준현"
    assert updated.correction_mode == "glossary"
    assert updated.correction_warnings
    assert cache.has("transcript_corrected.json")
    assert cache.has("transcript_correction.meta.json")


def test_correction_does_not_overwrite_aligned_transcript(tmp_path: Path) -> None:
    glossary_path = tmp_path / "glossary.txt"
    glossary_path.write_text("문지현 => 황준현\n", encoding="utf-8")
    cache = StageCache(tmp_path / "work")
    cache.prepare()
    original = [TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="문지현")]
    cache.write_json("transcript_aligned.json", original)
    args = argparse.Namespace(transcript_correction="glossary", glossary=glossary_path, correction_model="gpt-4.1-mini")
    quality = TranscriptQuality(asr_provider="manual", timecode_quality="approximate", approximate_timecodes=True)

    corrected, _ = correct_transcript(cache, original, quality, args, Logger())

    aligned = [TranscriptSegment.model_validate(item) for item in cache.read_json("transcript_aligned.json")]
    assert aligned[0].ko == "문지현"
    assert corrected[0].ko == "황준현"


def test_correct_transcript_openai_uses_corrector(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    class FakeCorrector:
        def __init__(self, api_key: str, model: str) -> None:
            assert api_key == "test-key"
            assert model == "gpt-test"

        def correct_segments(self, segments, glossary):  # type: ignore[no-untyped-def]
            return [segments[0].model_copy(update={"ko": "황준현"})], ["mock warning"]

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("ingest.__main__.OpenAITranscriptCorrector", FakeCorrector)
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    args = argparse.Namespace(transcript_correction="openai", glossary=None, correction_model="gpt-test")
    quality = TranscriptQuality(asr_provider="manual", timecode_quality="approximate", approximate_timecodes=True)
    segments = [TranscriptSegment(id=0, tc_start=0, tc_end=1, ko="문지현")]

    corrected, updated = correct_transcript(cache, segments, quality, args, Logger())

    assert corrected[0].ko == "황준현"
    assert updated.correction_mode == "openai"
    assert updated.correction_model == "gpt-test"
    assert updated.correction_warnings == ["mock warning"]
