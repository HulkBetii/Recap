# 07 - Render and QA

> Status: **PROPOSED / NOT IMPLEMENTED**. R6 `reaction_remix.compose`, R7 `reaction_remix.render`, R8 `reaction_remix.qa` and the contracts described here are not available in the current application.

## 1. Render Scope

The reaction-remix renderer assembles reordered source clips and their selected audio modes into one output video. It does not redesign the source video.

Visual policy is strict:

- Do not blur or cover burned-in subtitles.
- Do not add new subtitles, captions or translations.
- Do not draw text, masks, shapes or replacement backgrounds.
- Do not remove or redraw mascot, logo or branding.
- Do not apply decorative zooms, speed ramps or generated transitions.
- Preserve source frames for each selected unit, apart from codec re-encoding and necessary resolution/fps normalization.

Existing burned-in subtitles may appear in a different overall order because their complete source reaction units were reordered. That is expected and must not trigger subtitle cleanup.

## 2. Proposed Timeline Contract

`remix_edl.json` is a **proposed, not implemented** contract. It is deliberately separate from the recap `edl.json`, whose audio assumptions are incompatible with reaction-remix.

Illustrative shape only; the canonical proposed shape is defined in [03-data-contracts.md](03-data-contracts.md):

```json
{
  "schema_version": "reaction-remix.proposed.v1",
  "placements": [
    {
      "placement_id": "placement-0000",
      "kind": "reaction",
      "tl_start": 0.0,
      "tl_end": 12.4,
      "video": {"src_in": 412.1, "src_out": 424.5, "speed": 1.0, "filters": []},
      "audio": {"mode": "source", "source_in": 412.1, "source_out": 424.5, "source_gain_db": 0.0, "filters": []}
    },
    {
      "placement_id": "placement-0001",
      "kind": "commentary",
      "tl_start": 12.4,
      "tl_end": 18.82,
      "video": {"src_in": 579.5, "src_out": 585.92, "speed": 1.0, "filters": []},
      "audio": {"mode": "tts_bed", "tts_audio_path": "audio/commentary-slot-0001.mp3"}
    }
  ]
}
```

Required validator behavior:

- Tile the output timeline without gaps or overlaps.
- Keep source and timeline duration equal when `speed=1.0`.
- Require `audio.mode=source` for reaction placements.
- Allow TTS only for commentary placements.
- Validate every path, source span and item reference.
- Reject output duration below `80%` or above `100%` of source duration.

## 3. Reuse Boundary

The proposed renderer may reuse:

- `common/media.py` for ffmpeg/ffprobe checks and probes.
- `render/cache.py` for media-aware clip cache patterns.
- `render/quantize.py` concepts for frame-locked placement.
- Video normalization logic from `render/cut.py` where it does not alter content.

It must not reuse unchanged:

- `render.compose.mux_voiceover`, because reaction-remix has interleaved source audio and TTS.
- The recap `EdlPlacement` contract.
- GĐ5 footage matching or continuous voiceover timing.
- Any earlier POC subtitle blur or ASS subtitle filter.

Default output should preserve the source's `1920x1080`, `30000/1001 fps` profile when possible. Configuration may permit normalization for other inputs, but no visual filter may be introduced to manipulate subtitles.

## 4. Render Strategy

Proposed approach:

1. Quantize timeline boundaries globally to output frames.
2. Cut each selected source span with video and its requested audio mode.
3. For reaction clips, preserve the complete original audio track at `1.0x`.
4. For commentary clips, mix TTS with an approved bed or muted source.
5. Encode clips to compatible H.264/AAC parameters.
6. Concatenate clips in planned order.
7. Apply limiter/ducking only inside commentary mixes; never process reaction audio globally.
8. Probe and validate the complete output before delivery.

Stream copy may be used only when frame-accurate cuts and compatible timestamps can be guaranteed. Correct boundaries and A/V sync are more important than avoiding re-encoding.

## 5. Proposed QA Artifacts

The following artifacts are **proposed, not implemented**:

- `remix_qa.json`: machine-readable structural, duration, audio and media checks.
- `remix.review.html`: optional local review page with planned order, source spans and warnings.
- `render.meta.json`: codec, duration, resolution, fps, cache hits and algorithm version.
- `qa/frames/`: representative frames immediately before, inside and after edit boundaries.
- `qa/audio/`: optional short diagnostic excerpts for reaction correlation and narrator-leakage review.

QA artifacts belong under the run directory and must not be committed with copyrighted source media.

## 6. QA Layers

### Structural QA

- Validate Pydantic contracts and referential integrity.
- Detect timeline gaps, overlaps, duplicate reaction use and invalid source ranges.
- Report every reaction exclusion and intentional reuse.
- Confirm duration target and hard floor.

### Visual QA

- Decode the entire output without ffmpeg errors.
- Detect black frames or frozen frames introduced at edit boundaries.
- Compare representative rendered frames with their source frames.
- Confirm no blur, subtitle, drawtext, mask or overlay filters were requested.

### Audio QA

- Verify reaction A/V sync and waveform correlation.
- Measure commentary loudness, full-output loudness and true peak.
- Detect clipping, clicks, gaps and old narrator leakage.
- Transcribe representative reaction and commentary spans to confirm content survived assembly.

### Editorial QA

- Check the new order remains understandable.
- Confirm each Japanese commentary statement is supported by adjacent reactions.
- Confirm no reaction sentence or subtitle exchange is cut in half.
- Confirm channel branding remains present according to the plan.

## 7. Acceptance Gates

Render is deliverable only when:

- Full-file ffmpeg decode returns exit code `0`.
- Output is H.264/AAC with an audio stream and the configured source-compatible resolution/fps.
- Timeline gaps and overlaps are both `0`.
- Black intervals introduced by editing are `0`.
- Reaction placements at speeds other than `1.0` are `0`.
- Reaction audio lag is at most one output frame, with `20 ms` preferred, and sampled correlation is at least `0.98`.
- Commentary source-vocal leakage produces no intelligible old narrator in spot checks.
- Commentary mixes peak at or below `-1.5 dBTP` after encode tolerance. Full-program true peak does not exceed the source program by more than `0.3 dB`, and no new clipping event is introduced.
- Output duration is `80-100%` of source duration, with the normal target at `85-90%`.
- No visual subtitle-processing filter is present in the render command or manifest.
- Representative frames from every edit boundary are source-consistent and show no flash frame.
- `remix_qa.json` contains no severity `error` finding.

For the current POC source window `09:30-12:30`, the output must remain between `144` and `180` seconds. For the full `18:49.302` source, it must not be shorter than approximately `15:03.4`.
