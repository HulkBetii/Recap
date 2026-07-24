from __future__ import annotations

import argparse

import pytest

from common.schema import TranscriptSegment, TranslatedSegment, VisionSegment
from ingest.__main__ import IngestError, run_ingest


class FakeClient:
    def __init__(self, api_key: str, translate_model: str, vision_model: str) -> None:
        self.api_key = api_key
        self.translate_model = translate_model
        self.vision_model = vision_model

    def translate_segments(self, transcript, logger=None, source_language="ko"):  # type: ignore[no-untyped-def]
        return [
            TranslatedSegment(
                id=item.id,
                tc_start=item.tc_start,
                tc_end=item.tc_end,
                ko=item.ko,
                en=f"translated-{item.id}",
            )
            for item in transcript
        ], 0

    def describe_frame(self, frame_path):  # type: ignore[no-untyped-def]
        return "A character stands in a quiet room."


def make_args(tmp_path, input_path):  # type: ignore[no-untyped-def]
    return argparse.Namespace(
        input=input_path,
        output=tmp_path / "out" / "film_map.json",
        whisper_model="tiny",
        gap_threshold=1.0,
        max_vision_frames=10,
        translate_model="gpt-4.1-mini",
        vision_model="gpt-4.1-mini",
        device="cpu",
        work_dir=tmp_path / "work",
        force=False,
        log_level="ERROR",
    )


def test_run_ingest_rejects_missing_input(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    with pytest.raises(IngestError, match="Input video does not exist"):
        run_ingest(make_args(tmp_path, tmp_path / "missing.mp4"))


def test_run_ingest_requires_openai_key(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "film.mp4"
    input_path.write_bytes(b"fake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(IngestError, match="OPENAI_API_KEY"):
        run_ingest(make_args(tmp_path, input_path))


def test_run_ingest_rejects_invalid_openai_key_before_client(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "film.mp4"
    input_path.write_bytes(b"fake")
    monkeypatch.setenv("OPENAI_API_KEY", "\x16")
    monkeypatch.setattr(
        "ingest.__main__.OpenAIIngestClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )

    with pytest.raises(IngestError, match="must start with sk-"):
        run_ingest(make_args(tmp_path, input_path))

def test_run_ingest_mocked_end_to_end(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "film.mp4"
    input_path.write_bytes(b"fake")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setattr("ingest.__main__.require_ffmpeg", lambda: None)
    monkeypatch.setattr("ingest.__main__.probe_duration", lambda path: 10.0)
    monkeypatch.setattr("ingest.__main__.extract_audio", lambda src, dst: dst.write_bytes(b"wav"))
    monkeypatch.setattr(
        "ingest.__main__.transcribe_korean",
        lambda audio, model, device, *args, **kwargs: [
            TranscriptSegment(id=0, tc_start=2.0, tc_end=3.0, ko="안녕"),
            TranscriptSegment(id=1, tc_start=6.0, tc_end=7.0, ko="가자"),
        ],
    )
    monkeypatch.setattr("ingest.__main__.OpenAIIngestClient", FakeClient)
    monkeypatch.setattr(
        "ingest.__main__.describe_gaps",
        lambda **kwargs: ([VisionSegment(gap_id=0, tc_start=0.0, tc_end=2.0, scene_desc="Opening shot.")], 0),
    )

    exit_code = run_ingest(make_args(tmp_path, input_path))

    assert exit_code == 0
    assert (tmp_path / "out" / "film_map.json").exists()
    assert (tmp_path / "out" / "film_map.meta.json").exists()
    output = (tmp_path / "out" / "film_map.json").read_text(encoding="utf-8")
    assert "Opening shot." in output
    assert "translated-0" in output
