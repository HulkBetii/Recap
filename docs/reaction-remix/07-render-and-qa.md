# 07 - Render and QA

> Status: **R6-R8 preservation-first POC and full authorized-video hard QA accepted; preferred duration is audit-only**.

## 1. Render Scope

The reaction-remix renderer assembles reordered source clips and their selected audio modes into one output video. It does not redesign the source video.

Visual policy is strict:

- Do not blur or cover burned-in subtitles.
- Do not add new subtitles, captions or translations.
- Do not draw text, masks, shapes or replacement backgrounds.
- Do not remove or redraw mascot, logo or branding.
- Do not apply decorative zooms, speed ramps or generated transitions.
- Preserve source frames for each selected unit apart from codec re-encoding; v1 rejects inputs that require resolution/FPS conversion.

Existing burned-in subtitles may appear in a different overall order because their complete source reaction units were reordered. That is expected and must not trigger subtitle cleanup.

## 2. Timeline Contract

`remix_edl.json` is implemented and deliberately separate from recap `edl.json`, whose audio assumptions are incompatible with reaction-remix.

The canonical shape is defined in [03-data-contracts.md](03-data-contracts.md):

```json
{
  "schema_version": "reaction-remix.v1",
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

The renderer reuses:

- `common/media.py` for ffmpeg/ffprobe checks and probes.
- `render/cache.py` for media-aware clip cache patterns.
- `render/quantize.py` concepts for frame-locked placement.
- Video normalization logic from `render/cut.py` where it does not alter content.

It must not reuse unchanged:

- `render.compose.mux_voiceover`, because reaction-remix has interleaved source audio and TTS.
- The recap `EdlPlacement` contract.
- GĐ5 footage matching or continuous voiceover timing.
- Any earlier POC subtitle blur or ASS subtitle filter.

V1 output preserves the source resolution, rational FPS, aspect ratio, sample rate, and channel count. It does not normalize those media properties or introduce any visual filter to manipulate subtitles.

## 4. Render Strategy

Implemented approach:

1. Quantize timeline boundaries globally to output frames.
2. Render source-compatible video-only clips and lossless PCM placement audio.
3. For reaction clips, preserve the complete original audio track at `1.0x`.
4. For commentary clips, use TTS-only or mix TTS with an approved `no_vocals` bed.
5. Encode video clips to H.264 CRF18 and concatenate them with the demuxer.
6. Concatenate PCM audio losslessly, then encode AAC exactly once during final mux.
7. Apply fixed gains, fades, and a limiter only inside commentary mixes; never process reaction audio globally or dynamically duck reaction audio.
8. Probe and validate the complete output before delivery.

The accepted implementation identities are `reaction-render-v4` and
`reaction-qa-v7`. Source clips use the quantized frame/sample indexes written
to `render.timeline.json`; TTS is resampled before sample-accurate trim, and the
PCM placement timeline is the authority for the one-time AAC encode.

Stream copy may be used only when frame-accurate cuts and compatible timestamps can be guaranteed. Correct boundaries and A/V sync are more important than avoiding re-encoding.

## 5. QA Artifacts

The runtime writes the following artifacts:

- `remix_qa.json`: machine-readable structural, duration, audio and media checks.
- `remix.review.html`: optional local review page with planned order, source spans and warnings.
- `render.meta.json`: codec, duration, source-compatible media profile, provenance hashes, and cache hits.
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

- Decode each protected placement once, then verify A/V sync, gain, waveform
  correlation, and frame similarity at its head, middle, and tail.
- Measure commentary loudness, full-output loudness and true peak.
- Detect clipping, clicks, gaps and old narrator leakage.
- Transcribe representative reaction and commentary spans to confirm content survived assembly.

### Editorial QA

- Check the new order remains understandable.
- Confirm each Japanese commentary statement is supported by adjacent reactions.
- Confirm no reaction sentence or subtitle exchange is cut in half.
- Confirm channel branding remains present according to the plan.

Old-narrator leakage is a hard gate only for replaced commentary placements.
Narrator overlap intentionally retained in protected `mixed`/`unknown` is
listed in `protected_narrator_overlap_block_ids` and produces a warning, not a
failure.

## 7. Acceptance Gates

Render is deliverable only when:

- Full-file ffmpeg decode returns exit code `0`.
- Output is H.264/AAC with an audio stream and the configured source-compatible resolution/fps.
- Timeline gaps and overlaps are both `0`.
- Decoded video-frame and audio-sample counts match `render.timeline.json`;
  tolerance is one video frame and one output-frame worth of AAC samples.
- Black intervals introduced by editing are `0`.
- Reaction placements at speeds other than `1.0` are `0`.
- Reaction audio lag is at most one output frame, with `20 ms` preferred, and sampled correlation is at least `0.98`.
- Each protected head/tail probe uses a window of at most `0.5s`; head, middle,
  and tail all require gain delta `<=0.3 dB` and frame similarity `>=0.995` in
  addition to the correlation/lag gates.
- Commentary source-vocal leakage produces no intelligible old narrator in spot checks.
- Any edit-boundary click or unexpected silence is a hard failure.
- Commentary mixes peak at or below `-1.5 dBTP` after encode tolerance. Full-program true peak does not exceed the source program by more than `0.3 dB`, and no new clipping event is introduced.
- Output duration is `80-100%` of source duration, with the normal target at `85-90%`.
- No visual subtitle-processing filter is present in the render command or manifest.
- Representative frames from every edit boundary are source-consistent and show no flash frame.
- `remix_qa.json` contains no severity `error` finding.

For the current POC source window `09:30-12:30`, the output must remain between `144` and `180` seconds. For the full `18:49.302` source, it must not be shorter than approximately `15:03.4`.

## 8. Preservation-First POC Evidence

The accepted POC output is `176.609767s` from `184.201633s` source
(`0.958785`). It passes with:

- `35` protected placements checked and no failed placement IDs;
- minimum audio correlation `0.9986287014`, maximum A/V lag `0 ms`, maximum
  gain delta `0.0691078578 dB`, and minimum frame similarity `0.9950234368`;
- zero forbidden visual operations, clicks, unexpected silence, provider/voice
  mismatches, or old-narrator leakage in the two replaced commentary slots;
- protected narrator overlap warnings for `block-0038` and `block-0039`;
- full-output peak increase `0.1 dB`;
- decoded count deltas of `-1` frame and `-1318` samples, inside the one-frame
  tolerances (`1471` samples at this media profile).

The measured output is slightly longer than the EDL float duration because the
renderer is frame/sample quantized. The decoded media counts, not unquantized
float addition, are used for the final acceptance comparison.

## 9. Full Authorized-Video QA Evidence

The full authorized source run in `work/reaction-remix-full-r02/` passed R0-R8
hard QA on 2026-07-14. The source duration is `1129.302494s`; the rendered
output is `1010.379002s` (`0.894693x`). The run is shorter than the source and
inside the hard `0.80-1.00` duration range. It is inside the preferred `0.85-0.90` ratio after explicitly dropping
`block-0045`, a user-reported hard segment with audible old commentary. Exact
target length remains audit-only as long as the output is shorter than the
original and hard QA passes.

Measured full-run gates:

- `160` protected reaction/mixed/unknown placements checked; failed placement
  IDs `[]`.
- Minimum reaction audio correlation `0.9971666`, maximum A/V lag `0ms`,
  maximum gain delta `0.0931131dB`, and minimum frame similarity `0.9954148`.
- `12` AI33 commentary slots checked; provider and voice mismatches `0`;
  minimum commentary ASR match `0.9`; replaced-commentary old narrator leakage
  `0`.
- Protected narrator overlap is reported as warning-only audit evidence for
  `block-0093`, `block-0127`, and `block-0186`.
- Visual policy passed with `0` subtitle, ASS, drawtext, overlay, blur, mask, or
  delogo operations.
- Audio QA passed with `0` click/silence defects, commentary true peak `-1.4
  dBFS`, full-output true peak `-1.4 dBFS`, source true peak `-1.3 dBFS`, and no
  program peak increase.
- Timeline QA passed with no gaps/overlaps, full decode OK, `-1` decoded video
  frame delta and `627` decoded audio sample delta, both inside one-frame
  tolerance.

QA frame extraction now prefers exact CFR frame-index reads through OpenCV.
This avoids false failures when timestamp seeking near H.264/concat keyframes
lands on an adjacent clip even though the quantized output frame is preserved.
