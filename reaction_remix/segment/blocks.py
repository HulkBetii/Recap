from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from common.integrity import stable_hash
from common.schema import (
    ReactionBlock,
    ReactionBlockKind,
    ReactionBlocks,
    ReactionCutPoint,
    ReactionPreservation,
    ReactionSource,
    ReactionTranscript,
    ReactionWord,
    validate_reaction_blocks,
)
from reaction_remix.segment.classify import classify_turn
from reaction_remix.segment.cut_points import (
    BoundaryPolicy,
    INSUFFICIENT_SPEECH_PADDING_CONFIDENCE,
    SelectedCut,
    select_cut,
    source_boundary_cut,
)


@dataclass(frozen=True)
class SegmentSettings:
    min_silence_s: float = 0.25
    speech_padding_s: float = 0.12
    scene_window_s: float = 0.5
    min_cut_spacing_s: float = 0.08
    commentary_min_confidence: float = 0.90
    narrator_min_regions: int = 3
    narrator_min_japanese_ratio: float = 0.90
    broll_gap_s: float = 1.5
    boundary_policy: BoundaryPolicy = "strict-or-word-edge"

    def validate(self) -> None:
        if self.min_silence_s < 0:
            raise ValueError("min_silence_s must be non-negative")
        if self.speech_padding_s < 0:
            raise ValueError("speech_padding_s must be non-negative")
        if self.scene_window_s < 0:
            raise ValueError("scene_window_s must be non-negative")
        if self.broll_gap_s <= 0:
            raise ValueError("broll_gap_s must be positive")
        if self.min_cut_spacing_s <= 0:
            raise ValueError("min_cut_spacing_s must be positive")
        if not 0.90 <= self.commentary_min_confidence <= 1:
            raise ValueError("commentary_min_confidence must be between 0.90 and 1.0")
        if self.narrator_min_regions < 3:
            raise ValueError("narrator_min_regions must be at least three")
        if not 0.90 <= self.narrator_min_japanese_ratio <= 1:
            raise ValueError("narrator_min_japanese_ratio must be between 0.90 and 1.0")
        if self.boundary_policy not in {"strict", "strict-or-word-edge"}:
            raise ValueError("boundary_policy must be strict or strict-or-word-edge")


@dataclass
class _Event:
    tc_start: float
    tc_end: float
    kind: ReactionBlockKind
    turn_ids: list[int] = field(default_factory=list)
    language_codes: list[str] = field(default_factory=list)
    speaker_ids: list[str] = field(default_factory=list)
    language_confidence: float = 1.0
    speaker_confidence: float = 1.0
    asr_confidence: float = 1.0
    has_content_overlap: bool = False


def _combined_kind(left: ReactionBlockKind, right: ReactionBlockKind) -> ReactionBlockKind:
    if left == right:
        return left
    if "unknown" in {left, right}:
        return "unknown"
    return "mixed"


def _timeline_events(
    transcript: ReactionTranscript,
    settings: SegmentSettings,
    narrator_speaker_id: str | None,
) -> list[_Event]:
    candidates: list[_Event] = [
        _Event(
            tc_start=turn.tc_start,
            tc_end=turn.tc_end,
            kind=classify_turn(
                turn,
                narrator_speaker_id,
                commentary_min_confidence=settings.commentary_min_confidence,
            ),
            turn_ids=[turn.turn_id],
            language_codes=[turn.language],
            speaker_ids=[turn.speaker_id],
            language_confidence=turn.language_confidence,
            speaker_confidence=turn.speaker_confidence,
            asr_confidence=turn.asr_confidence,
        )
        for turn in transcript.turns
    ]
    candidates.extend(
        _Event(tc_start=region.tc_start, tc_end=region.tc_end, kind="unknown")
        for region in transcript.regions
        if region.status == "analysis_gap"
    )
    if not candidates:
        return [_Event(0.0, transcript.source_duration_s, "broll")]

    boundaries = sorted(
        {
            0.0,
            transcript.source_duration_s,
            *(event.tc_start for event in candidates),
            *(event.tc_end for event in candidates),
        }
    )
    cells: list[_Event] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        active = [event for event in candidates if event.tc_start < end - 1e-6 and event.tc_end > start + 1e-6]
        if not active:
            continue
        if any(event.kind == "unknown" and not event.turn_ids for event in active):
            kind: ReactionBlockKind = "unknown"
        else:
            kinds = {event.kind for event in active}
            kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
        turn_ids = sorted({turn_id for event in active for turn_id in event.turn_ids})
        languages = sorted({value for event in active for value in event.language_codes})
        speakers = sorted({value for event in active for value in event.speaker_ids})
        cells.append(
            _Event(
                tc_start=start,
                tc_end=end,
                kind=kind,
                turn_ids=turn_ids,
                language_codes=languages,
                speaker_ids=speakers,
                language_confidence=min((event.language_confidence for event in active), default=1.0),
                speaker_confidence=min((event.speaker_confidence for event in active), default=1.0),
                asr_confidence=min((event.asr_confidence for event in active), default=1.0),
                has_content_overlap=sum(bool(event.turn_ids) for event in active) > 1,
            )
        )

    merged: list[_Event] = []
    for cell in cells:
        if (
            merged
            and abs(merged[-1].tc_end - cell.tc_start) <= 1e-6
            and merged[-1].speaker_ids == cell.speaker_ids
            and merged[-1].language_codes == cell.language_codes
        ):
            previous = merged[-1]
            previous.kind = _combined_kind(previous.kind, cell.kind)
            previous.tc_end = cell.tc_end
            previous.turn_ids = sorted(set(previous.turn_ids + cell.turn_ids))
            previous.language_confidence = min(previous.language_confidence, cell.language_confidence)
            previous.speaker_confidence = min(previous.speaker_confidence, cell.speaker_confidence)
            previous.asr_confidence = min(previous.asr_confidence, cell.asr_confidence)
            previous.has_content_overlap = previous.has_content_overlap or cell.has_content_overlap
        else:
            merged.append(cell)

    coalesced: list[_Event] = []
    for event in merged:
        if (
            coalesced
            and event.tc_start - coalesced[-1].tc_end < settings.broll_gap_s
            and coalesced[-1].speaker_ids == event.speaker_ids
            and coalesced[-1].language_codes == event.language_codes
        ):
            previous = coalesced[-1]
            previous.kind = _combined_kind(previous.kind, event.kind)
            previous.tc_end = event.tc_end
            previous.turn_ids = sorted(set(previous.turn_ids + event.turn_ids))
            previous.language_confidence = min(previous.language_confidence, event.language_confidence)
            previous.speaker_confidence = min(previous.speaker_confidence, event.speaker_confidence)
            previous.asr_confidence = min(previous.asr_confidence, event.asr_confidence)
            previous.has_content_overlap = previous.has_content_overlap or event.has_content_overlap
        else:
            coalesced.append(event)

    with_broll: list[_Event] = []
    cursor = 0.0
    for event in coalesced:
        if event.tc_start - cursor >= settings.broll_gap_s:
            with_broll.append(_Event(cursor, event.tc_start, "broll"))
        with_broll.append(event)
        cursor = event.tc_end
    if transcript.source_duration_s - cursor >= settings.broll_gap_s:
        with_broll.append(_Event(cursor, transcript.source_duration_s, "broll"))
    return with_broll or [_Event(0.0, transcript.source_duration_s, "broll")]


def _merge_short_events(events: list[_Event], min_duration_s: float) -> list[_Event]:
    output = list(events)
    while len(output) > 1:
        short_index = next(
            (index for index, event in enumerate(output) if event.tc_end - event.tc_start < min_duration_s),
            None,
        )
        if short_index is None:
            break
        output = _merge_event_at_index(output, short_index)
    return output


def _merge_event_at_index(events: list[_Event], index: int) -> list[_Event]:
    if len(events) <= 1:
        return list(events)
    output = list(events)
    left_index = index - 1 if index > 0 else index
    right_index = index if index > 0 else index + 1
    left = output[left_index]
    right = output[right_index]
    merged = _Event(
        tc_start=min(left.tc_start, right.tc_start),
        tc_end=max(left.tc_end, right.tc_end),
        kind=_combined_kind(left.kind, right.kind),
        turn_ids=sorted(set(left.turn_ids + right.turn_ids)),
        language_codes=sorted(set(left.language_codes + right.language_codes)),
        speaker_ids=sorted(set(left.speaker_ids + right.speaker_ids)),
        language_confidence=min(left.language_confidence, right.language_confidence),
        speaker_confidence=min(left.speaker_confidence, right.speaker_confidence),
        asr_confidence=min(left.asr_confidence, right.asr_confidence),
        has_content_overlap=left.has_content_overlap or right.has_content_overlap,
    )
    output[left_index : right_index + 1] = [merged]
    return output


def _event_speech_span(event: _Event, turn_by_id: dict[int, object]) -> tuple[float, float]:
    words = [
        word
        for turn_id in event.turn_ids
        for word in getattr(turn_by_id.get(turn_id), "words", [])
        if word.tc_start < event.tc_end - 1e-6 and word.tc_end > event.tc_start + 1e-6
    ]
    if not words:
        return event.tc_start, event.tc_end
    return (
        max(event.tc_start, min(word.tc_start for word in words)),
        min(event.tc_end, max(word.tc_end for word in words)),
    )


def _is_high_confidence_japanese_narrator(event: _Event) -> bool:
    return (
        event.kind == "commentary"
        and len(event.speaker_ids) == 1
        and event.language_codes == ["ja"]
        and event.language_confidence >= 0.90
        and event.speaker_confidence >= 0.90
        and not event.has_content_overlap
    )


def _derive_cuts(
    events: list[_Event],
    *,
    source_duration_s: float,
    turn_by_id: dict[int, object],
    words: list[ReactionWord],
    scene_boundaries: list[float],
    settings: SegmentSettings,
) -> tuple[list[SelectedCut], int | None]:
    cuts: list[SelectedCut] = [source_boundary_cut(0.0)]
    for boundary_index, (previous, current) in enumerate(zip(events, events[1:])):
        _previous_speech_start, previous_content_end = _event_speech_span(previous, turn_by_id)
        next_content_start, _next_speech_end = _event_speech_span(current, turn_by_id)
        if previous.kind in {"broll", "transition"} and current.kind not in {"broll", "transition"}:
            previous_content_end = max(
                previous.tc_start,
                previous.tc_end - settings.speech_padding_s * 2,
            )
        elif previous.kind not in {"broll", "transition"} and current.kind in {"broll", "transition"}:
            next_content_start = min(
                current.tc_end,
                current.tc_start + settings.speech_padding_s * 2,
            )
        try:
            cut = select_cut(
                previous_content_end,
                next_content_start,
                scene_boundaries=scene_boundaries,
                words=words,
                scene_window_s=settings.scene_window_s,
                min_silence_s=settings.min_silence_s,
                speech_padding_s=settings.speech_padding_s,
                boundary_confidence=0.92,
                boundary_policy=settings.boundary_policy,
                word_edge_eligible=(
                    _is_high_confidence_japanese_narrator(previous)
                    or _is_high_confidence_japanese_narrator(current)
                ),
            )
        except ValueError:
            return cuts, boundary_index
        cuts.append(cut)
    cuts.append(source_boundary_cut(source_duration_s))
    return cuts, None


def build_reaction_blocks(
    source: ReactionSource,
    transcript: ReactionTranscript,
    *,
    scene_boundaries: list[float] | None = None,
    settings: SegmentSettings | None = None,
) -> ReactionBlocks:
    settings = settings or SegmentSettings()
    settings.validate()
    if source.input_hash != transcript.source_hash:
        raise ValueError("reaction source and transcript hashes do not match")
    narrator_speaker_id = transcript.narrator_speaker_id
    narrator_cluster = next(
        (item for item in transcript.speaker_clusters if item.speaker_id == narrator_speaker_id),
        None,
    )
    if narrator_cluster is None or (
        narrator_cluster.region_count < settings.narrator_min_regions
        or narrator_cluster.language_ratios.get("ja", 0.0) < settings.narrator_min_japanese_ratio
    ):
        narrator_speaker_id = None
    events = sorted(
        _timeline_events(transcript, settings, narrator_speaker_id),
        key=lambda item: (item.tc_start, item.tc_end),
    )
    events = _merge_short_events(events, settings.min_cut_spacing_s)
    scene_boundaries = sorted(scene_boundaries or [])
    words = [word for turn in transcript.turns for word in turn.words]
    turn_by_id = {turn.turn_id: turn for turn in transcript.turns}
    while True:
        cuts, unsafe_boundary_index = _derive_cuts(
            events,
            source_duration_s=source.duration_s,
            turn_by_id=turn_by_id,
            words=words,
            scene_boundaries=scene_boundaries,
            settings=settings,
        )
        if unsafe_boundary_index is not None:
            if len(events) <= 1:
                raise ValueError("could not derive a cut outside word timestamps")
            events = _merge_event_at_index(events, unsafe_boundary_index + 1)
            continue
        short_index = next(
            (
                index
                for index, (previous, current) in enumerate(zip(cuts, cuts[1:]))
                if current.tc - previous.tc < settings.min_cut_spacing_s - 1e-6
            ),
            None,
        )
        if short_index is None:
            break
        if len(events) <= 1:
            raise ValueError("could not derive cut points with the configured minimum spacing")
        events = _merge_event_at_index(events, short_index)
    cut_models = [
        ReactionCutPoint(
            cut_point_id=f"cut-{index:04d}",
            tc=cut.tc,
            kind=cut.kind,
            confidence=cut.confidence,
            speech_padding_s=settings.speech_padding_s,
            safety_mode=cut.safety_mode,
            left_handle_s=cut.left_handle_s,
            right_handle_s=cut.right_handle_s,
        )
        for index, cut in enumerate(cuts)
    ]

    blocks: list[ReactionBlock] = []
    sequence_counts: dict[str, int] = {}
    for index, event in enumerate(events, start=1):
        safe_start = cut_models[index - 1].tc
        safe_end = cut_models[index].tc
        if safe_end <= safe_start:
            raise ValueError(f"derived non-positive safe media span for block #{index}")
        content_start = max(safe_start, event.tc_start)
        content_end = min(safe_end, event.tc_end)
        if content_end <= content_start:
            content_start, content_end = safe_start, safe_end
        boundary_confidence = min(cut_models[index - 1].confidence, cut_models[index].confidence)
        if event.has_content_overlap:
            boundary_confidence = min(
                boundary_confidence,
                INSUFFICIENT_SPEECH_PADDING_CONFIDENCE,
            )
        kind = event.kind
        warnings: list[str] = []
        if event.has_content_overlap:
            kind = "mixed"
            warnings.append("overlapping speech content preserved as mixed")
        allowed_commentary_boundary_modes = (
            {"full_handle", "word_edge"}
            if settings.boundary_policy == "strict-or-word-edge"
            else {"full_handle"}
        )
        if kind == "commentary" and (
            cut_models[index - 1].safety_mode not in allowed_commentary_boundary_modes
            or cut_models[index].safety_mode not in allowed_commentary_boundary_modes
        ):
            kind = "mixed"
            warnings.append(
                "commentary candidate downgraded because both boundaries are not eligible under the configured policy"
            )
        if kind == "commentary" and min(
            event.language_confidence,
            event.speaker_confidence,
            boundary_confidence,
        ) < settings.commentary_min_confidence:
            kind = "mixed"
            warnings.append("commentary candidate downgraded because one confidence signal is below 0.90")
        sequence_group_id: str | None = None
        sequence_index: int | None = None
        if kind == "reaction" and len(event.speaker_ids) == 1:
            sequence_group_id = event.speaker_ids[0]
            sequence_index = sequence_counts.get(sequence_group_id, 0)
            sequence_counts[sequence_group_id] = sequence_index + 1
        blocks.append(
            ReactionBlock(
                block_id=f"block-{index:04d}",
                kind=kind,
                tc_start=safe_start,
                tc_end=safe_end,
                content_tc_start=content_start,
                content_tc_end=content_end,
                start_cut_point_id=cut_models[index - 1].cut_point_id,
                end_cut_point_id=cut_models[index].cut_point_id,
                turn_ids=event.turn_ids,
                language_codes=event.language_codes,
                speaker_ids=event.speaker_ids,
                sequence_group_id=sequence_group_id,
                sequence_index=sequence_index,
                semantic=None,
                preservation=ReactionPreservation(
                    audio="replace_commentary" if kind == "commentary" else "source_mix"
                ),
                eligible_commentary_visual=kind == "commentary",
                classification_confidence=min(
                    event.language_confidence,
                    event.speaker_confidence,
                    boundary_confidence,
                ),
                language_confidence=event.language_confidence,
                speaker_confidence=event.speaker_confidence,
                boundary_confidence=boundary_confidence,
                warnings=warnings,
            )
        )
    artifact = ReactionBlocks(
        source_hash=source.input_hash,
        transcript_hash=stable_hash(transcript.model_dump(mode="json")),
        source_duration_s=source.duration_s,
        cut_points=cut_models,
        blocks=blocks,
        created_at=datetime.now(timezone.utc),
        warnings=[],
    )
    return validate_reaction_blocks(artifact, transcript)
