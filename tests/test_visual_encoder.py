from __future__ import annotations

from contextlib import nullcontext

from visual_index.encoder import TransformerVisualEncoder


class FakeTensor:
    def __init__(self, rows):  # type: ignore[no-untyped-def]
        self.rows = rows

    def to(self, _device):  # type: ignore[no-untyped-def]
        return self

    def detach(self):  # type: ignore[no-untyped-def]
        return self

    def cpu(self):  # type: ignore[no-untyped-def]
        return self

    def float(self):  # type: ignore[no-untyped-def]
        return self

    def tolist(self):  # type: ignore[no-untyped-def]
        return self.rows


class FakeProcessor:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return {"input_ids": FakeTensor([[1]])}


class FakeModel:
    def get_text_features(self, **_inputs):  # type: ignore[no-untyped-def]
        return FakeTensor([[1.0, 0.0]])


class FakeTorch:
    @staticmethod
    def inference_mode():  # type: ignore[no-untyped-def]
        return nullcontext()


def test_siglip_text_preprocessing_uses_fixed_64_tokens() -> None:
    encoder = object.__new__(TransformerVisualEncoder)
    encoder.device = "cpu"
    encoder.processor = FakeProcessor()
    encoder.model = FakeModel()
    encoder.torch = FakeTorch()

    assert encoder.encode_texts(["query"], batch_size=1) == [[1.0, 0.0]]
    call = encoder.processor.calls[0]
    assert call["padding"] == "max_length"
    assert call["max_length"] == 64
    assert call["truncation"] is True
