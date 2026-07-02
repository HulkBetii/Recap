from __future__ import annotations

from common.schema import TranscriptSegment
from ingest.llm import OpenAIIngestClient


class FakeClient(OpenAIIngestClient):
    def __init__(self) -> None:
        pass

    def _translate_batch(self, batch):  # type: ignore[no-untyped-def]
        return {str(item.id): f"en-{item.id}" for item in batch}


def test_translate_segments_preserves_ids_and_timecodes() -> None:
    client = FakeClient()
    segments = [
        TranscriptSegment(id=0, tc_start=1.0, tc_end=2.0, ko="하나"),
        TranscriptSegment(id=1, tc_start=3.0, tc_end=4.0, ko="둘"),
    ]

    translated, warnings = client.translate_segments(segments, batch_size=1)

    assert warnings == 0
    assert [item.id for item in translated] == [0, 1]
    assert [item.en for item in translated] == ["en-0", "en-1"]
    assert [(item.tc_start, item.tc_end) for item in translated] == [(1.0, 2.0), (3.0, 4.0)]
