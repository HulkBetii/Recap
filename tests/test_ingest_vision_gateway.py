from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pytest

from common.llm_gateway import GatewayChatError
from common.schema import SilentGap, TranslatedSegment
from ingest.__main__ import build_vision_client, load_vision
from ingest.cache import StageCache
from ingest.gateway_vision import GatewayVisionClient
from ingest.llm import VISION_UNAVAILABLE, OpenAIIngestClient, VisionUnavailableError
from ingest.vision import VisionCircuitOpen, describe_gaps

LOGGER = logging.getLogger("test")


def gaps_with_frames(tmp_path: Path, count: int) -> tuple[list[SilentGap], Path]:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    gaps = [SilentGap(id=index, tc_start=index * 10.0, tc_end=index * 10.0 + 8.0) for index in range(count)]
    for gap in gaps:
        (frames_dir / f"gap-{gap.id:04d}.jpg").write_bytes(b"jpeg")
    return gaps, frames_dir


class QuotaClient:
    def __init__(self) -> None:
        self.calls = 0

    def describe_frame(self, _frame_path: Path) -> str:
        self.calls += 1
        raise VisionUnavailableError("no credits remaining")


def test_permanent_error_stops_after_first_frame(tmp_path: Path) -> None:
    gaps, frames_dir = gaps_with_frames(tmp_path, 5)
    client = QuotaClient()

    with pytest.raises(VisionCircuitOpen) as excinfo:
        describe_gaps(input_path=tmp_path / "film.mp4", gaps=gaps, frames_dir=frames_dir, client=client, logger=LOGGER)

    assert client.calls == 1
    assert [segment.scene_desc for segment in excinfo.value.segments] == [VISION_UNAVAILABLE] * 5
    assert excinfo.value.warnings_count == 5


class FlakyClient:
    def describe_frame(self, frame_path: Path) -> str:
        if frame_path.name.endswith("0001.jpg"):
            raise RuntimeError("transient")
        return f"desc {frame_path.stem}"


def test_transient_frame_error_keeps_other_frames(tmp_path: Path) -> None:
    gaps, frames_dir = gaps_with_frames(tmp_path, 3)
    segments, warnings = describe_gaps(input_path=tmp_path / "film.mp4", gaps=gaps, frames_dir=frames_dir, client=FlakyClient(), logger=LOGGER)
    assert [segment.scene_desc for segment in segments] == ["desc gap-0000", VISION_UNAVAILABLE, "desc gap-0002"]
    assert warnings == 1


class QuotaError(Exception):
    status_code = 429
    code = "credit_balance_exhausted"
    type = "insufficient_quota"


def test_openai_quota_error_is_not_retried(tmp_path: Path) -> None:
    client = object.__new__(OpenAIIngestClient)
    calls = 0

    def describe(_path: Path) -> str:
        nonlocal calls
        calls += 1
        raise QuotaError("You have no credits remaining")

    client._describe_frame = describe  # type: ignore[method-assign]
    with pytest.raises(VisionUnavailableError):
        client.describe_frame(tmp_path / "frame.jpg")
    assert calls == 1


class FakeVisionChat:
    def __init__(self, behaviour: dict[int, Exception]) -> None:
        self.behaviour = behaviour
        self.asked = 0

    async def __aenter__(self) -> "FakeVisionChat":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    async def ask(self, prompt: str, *, image_data_urls: list[str] | None = None) -> str:
        index = self.asked
        self.asked += 1
        assert image_data_urls and image_data_urls[0].startswith("data:image/jpeg;base64,")
        if index in self.behaviour:
            raise self.behaviour[index]
        return f" frame {index} "


def test_gateway_vision_describes_frames_concurrently(tmp_path: Path) -> None:
    gaps, frames_dir = gaps_with_frames(tmp_path, 4)
    chat = FakeVisionChat({})
    client = GatewayVisionClient(model="ag/x", concurrency=2, chat_client_factory=lambda: chat)

    segments, warnings = describe_gaps(input_path=tmp_path / "film.mp4", gaps=gaps, frames_dir=frames_dir, client=client, logger=LOGGER)

    assert warnings == 0
    assert chat.asked == 4
    assert all(segment.scene_desc.startswith("frame ") for segment in segments)


def test_gateway_vision_stops_on_permanent_gateway_error(tmp_path: Path) -> None:
    gaps, frames_dir = gaps_with_frames(tmp_path, 6)
    chat = FakeVisionChat({0: GatewayChatError("bad key", code="unauthorized", retryable=False)})
    client = GatewayVisionClient(model="ag/x", concurrency=1, chat_client_factory=lambda: chat)

    with pytest.raises(VisionCircuitOpen):
        describe_gaps(input_path=tmp_path / "film.mp4", gaps=gaps, frames_dir=frames_dir, client=client, logger=LOGGER)
    assert chat.asked == 1


def test_gateway_vision_stops_after_consecutive_transient_failures(tmp_path: Path) -> None:
    gaps, frames_dir = gaps_with_frames(tmp_path, 8)
    busy = GatewayChatError("budget exhausted", code="quarantined", retryable=True)
    chat = FakeVisionChat({0: busy, 1: busy, 2: busy})
    client = GatewayVisionClient(model="ag/x", concurrency=1, chat_client_factory=lambda: chat)

    with pytest.raises(VisionCircuitOpen):
        describe_gaps(input_path=tmp_path / "film.mp4", gaps=gaps, frames_dir=frames_dir, client=client, logger=LOGGER)
    assert chat.asked == 3


def test_circuit_open_vision_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = StageCache(tmp_path / "work", force=False)
    cache.prepare()
    translated = [TranslatedSegment(id=0, tc_start=10, tc_end=20, ko="안녕", en="Hello")]

    def tripped(**kwargs):  # type: ignore[no-untyped-def]
        raise VisionCircuitOpen("no credits", segments=[], warnings_count=1)

    monkeypatch.setattr("ingest.__main__.describe_gaps", tripped)
    segments, warnings = load_vision(
        cache=cache,
        input_path=tmp_path / "film.mp4",
        translated=translated,
        duration=35,
        gap_threshold=4,
        max_vision_frames=1,
        max_visual_gap_s=20,
        client=QuotaClient(),
        logger=LOGGER,
        vision_provider="openai",
    )
    assert (segments, warnings) == ([], 1)
    assert not cache.path("vision.json").exists()


def test_build_vision_client_returns_gateway_client() -> None:
    args = argparse.Namespace(max_vision_frames=10, vision_provider="ninerouter", vision_model="ag/x", vision_base_url=None, vision_concurrency=4)
    client = build_vision_client(args=args, openai_client=None, logger=LOGGER)
    assert isinstance(client, GatewayVisionClient)
    assert client.concurrency == 4
