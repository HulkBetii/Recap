from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any


class QuantizeError(ValueError):
    pass


@dataclass(frozen=True)
class QuantizedPlacement:
    placement: Any
    index: int
    frame_start: int
    frame_end: int
    sample_start: int
    sample_end: int
    source_frame_start: int
    source_frame_end: int
    source_sample_start: int
    source_sample_end: int

    @property
    def frame_count(self) -> int:
        return self.frame_end - self.frame_start

    @property
    def sample_count(self) -> int:
        return self.sample_end - self.sample_start


def quantize_remix_placements(
    placements: list[Any],
    *,
    fps_num: int,
    fps_den: int,
    sample_rate: int,
) -> list[QuantizedPlacement]:
    if fps_num <= 0 or fps_den <= 0:
        raise QuantizeError("FPS numerator and denominator must be positive")
    if sample_rate <= 0:
        raise QuantizeError("audio sample rate must be positive")

    fps = Fraction(fps_num, fps_den)
    ordered = sorted(placements, key=lambda item: (item.tl_start, item.tl_end, item.placement_id))
    output: list[QuantizedPlacement] = []
    previous_frame_end = 0
    previous_sample_end = 0
    for index, placement in enumerate(ordered):
        requested_frame_start = round(Fraction(str(placement.tl_start)) * fps)
        frame_end = round(Fraction(str(placement.tl_end)) * fps)
        requested_sample_start = round(Fraction(str(placement.tl_start)) * sample_rate)
        sample_end = round(Fraction(str(placement.tl_end)) * sample_rate)
        if index == 0 and (requested_frame_start != 0 or requested_sample_start != 0):
            raise QuantizeError("remix timeline must start at zero")
        if index > 0:
            if abs(requested_frame_start - previous_frame_end) > 1:
                raise QuantizeError(f"frame gap or overlap before placement #{index}")
            if abs(requested_sample_start - previous_sample_end) > 1:
                raise QuantizeError(f"sample gap or overlap before placement #{index}")
        frame_start = previous_frame_end
        sample_start = previous_sample_end
        if frame_end <= frame_start:
            raise QuantizeError(f"placement #{index} has zero frames after quantization")
        if sample_end <= sample_start:
            raise QuantizeError(f"placement #{index} has zero samples after quantization")
        source_frame_start = round(Fraction(str(placement.video.src_in)) * fps)
        if placement.audio.mode == "source":
            source_audio_start_s = placement.audio.source_in
        elif placement.audio.mode == "tts_bed":
            source_audio_start_s = placement.audio.bed_in
        else:
            source_audio_start_s = 0.0
        if source_audio_start_s is None:
            raise QuantizeError(f"placement #{index} is missing its source audio start")
        source_sample_start = round(Fraction(str(source_audio_start_s)) * sample_rate)
        output.append(
            QuantizedPlacement(
                placement=placement,
                index=index,
                frame_start=frame_start,
                frame_end=frame_end,
                sample_start=sample_start,
                sample_end=sample_end,
                source_frame_start=source_frame_start,
                source_frame_end=source_frame_start + frame_end - frame_start,
                source_sample_start=source_sample_start,
                source_sample_end=source_sample_start + sample_end - sample_start,
            )
        )
        previous_frame_end = frame_end
        previous_sample_end = sample_end
    return output
