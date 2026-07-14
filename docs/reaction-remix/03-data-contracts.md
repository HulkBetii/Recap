# Reaction Remix Data Contracts

Status: Implemented as `reaction-remix.v1` in `common/schema.py`.

`common/schema.py` is authoritative. Every stage reads and writes validated
files; stage-owned Python objects are not passed across process boundaries.
The executable examples under `examples/contracts/` are validated by
`tests/test_reaction_schema.py`.

## 1. Contract Rules

- `schema_version` is exactly `reaction-remix.v1`.
- Time values are float seconds. Source fields use `tc_*` or `src_*`; output
  fields use `tl_*`.
- Cross-stage hashes are lowercase SHA-256 digests with exactly 64 hexadecimal
  characters and no `sha256:` prefix.
- Paths stored in JSON use forward slashes.
- Pydantic models reject unknown fields.
- Blocks and placements are ordered and tile their respective timelines without
  gaps or overlaps.
- LLM output contains IDs, semantic labels, roles, reasons, and Japanese text;
  code owns source timecodes and safe cut points.

## 2. Artifact Map

| Stage | Primary contract | Purpose |
| --- | --- | --- |
| R0 Probe | `reaction_source.json` | Immutable source identity and stream profile. |
| R1 Analyze | `reaction_transcript.json` | Regions, multilingual turns, words, language confidence, and speaker clusters. |
| R2 Segment | `reaction_blocks.json` | Full source coverage, stable block IDs, safe cuts, and preservation policy. |
| R3 Plan | `remix_plan.json` | Editorial order, exclusions, semantic annotations, commentary slots, and retention. |
| R4 Write | `commentary_script.json` | Evidence-bound Japanese commentary text. |
| R5 TTS | `commentary_audio.json` | Measured AI33 audio per commentary slot. |
| R5 Fit | `commentary_fit_requests.json` | Selective shorten/lengthen/clarify requests for duration or Japanese ASR failures. |
| Stems | `audio_assets.json` | Full-source `no_vocals` stem identity and leakage status. |
| R6 Compose | `remix_edl.json` | Audio-aware source/TTS placement timeline. |
| R7 Render | `render.timeline.json` | Frame/sample-quantized render timeline. |
| R7 Render | `render.command-manifest.json` | Auditable FFmpeg commands and forbidden-filter denylist. |
| R7 Render | `render.meta.json` | Output codec/profile, hashes, duration, and cache hits. |
| R8 QA | `remix_qa.json` | Deterministic measurement report and repair history. |

Each stage also writes a namespaced meta file with its algorithm version,
input/output hashes, config hash, cache hits, warnings, and creation time.

## 3. `reaction_source.json`

R0 records one primary video stream and one primary audio stream. The video
profile preserves rational FPS as `fps_num`/`fps_den` and declares
`frame_rate_mode` as `cfr` or `vfr`. V1 fails when either primary stream is
missing or when `subtitle_streams` is non-empty. Burned-in subtitles are a
declaration and must remain untouched.

Important invariants:

- `input_hash` identifies the current source media;
- `config_hash` identifies probe policy;
- `subtitle_policy` is `burned_in_preserve`;
- production rendering accepts CFR only and preserves source resolution, FPS,
  sample rate, and channel count.

See `examples/contracts/reaction_source.json`.

## 4. `reaction_transcript.json`

R1 stores retryable analysis regions, primary non-overlapping turns, optional
word timestamps, multilingual language evidence, and speaker clusters.
An exhausted region becomes `analysis_gap`; it remains represented rather than
crashing the full run.

Important invariants:

- region and turn IDs are unique;
- turns reference declared regions and stay inside source duration;
- word timestamps stay inside their owning turns;
- `narrator_speaker_id`, when present, references a declared speaker cluster;
- ASR metadata records Faster Whisper model, device, chunk length, overlap, and
  word-timestamp policy.

## 5. `reaction_blocks.json`

R2 tiles the complete source with stable IDs `block-0001`, `block-0002`, and so
on. The ID never contains the classification, so reclassification does not
change references.

Every block contains two spans:

- `tc_start`/`tc_end`: safe media span selected from declared cut points;
- `content_tc_start`/`content_tc_end`: speech/content span inside the safe media
  span.

Every block also records language, speaker, and boundary confidence. A block is
`commentary` only when all three values are at least `0.90`. Analysis gaps are
`unknown`; `mixed` and `unknown` are preserve-only.

Each `ReactionCutPoint` has additive boundary-audit fields:

- `safety_mode`: `source_boundary`, `full_handle`, `word_edge`, `overlap`, or
  `null` for a protected insufficient-handle edge that is not commentary-safe;
- `left_handle_s` and `right_handle_s`: measured content-free handles. A
  negative value means content overlaps the cut.

Under `commentary_boundary_policy: strict_or_word_edge`, `full_handle` requires
the configured `120 ms` on both sides. `word_edge` permits shorter
non-negative handles only when no word is cut, adjacent content spans do not
overlap, one side is a single-speaker Japanese narrator event, and speaker and
language confidence are at least `0.90`; this eligible form has boundary
confidence exactly `0.90`. A non-overlapping cut that does not cross a word but
lacks both the handle and narrator evidence is recorded with `safety_mode=null`
and confidence at or below `0.89`; it remains available as a protected reaction
edge but cannot promote commentary. `overlap` is capped at `0.89` and cannot
bound a replaceable commentary block. The implemented segment cache identity is
`reaction-segment-v7`.

R2 may leave `semantic` null. R3 owns the final `semantic_annotations` used for
editorial planning. Cut points are ordered by silence midpoint, scene boundary
within tolerance, then turn boundary with speech padding, and no cut point may
fall inside a word timestamp.

See `examples/contracts/reaction_blocks.json`.

## 6. `remix_plan.json`

R3 stores a contiguous ordered list of source blocks and commentary slots.
Source items reference the block's declared start/end cut points; commentary
slots reference evidence and preferred visual block IDs but never source
timecodes.

Required planning records:

- `excluded_blocks`: each replaced commentary block plus category, reason,
  duration, and zero unique reaction speech removed;
- `semantic_annotations`: R3 summaries, country/topic, sentiment, intensity,
  and novelty keyed by existing block ID;
- `duration_policy`: hard `0.80-1.00`, preferred `0.85-0.90`, and configured
  target duration;
- `retention`: unique reaction speech ratio is at least `0.90`;
- `char_budget`: exactly `round(target_duration_s * 6.5)` for each commentary
  slot.

V1 forbids block reuse and retains every non-commentary block as a source
placement. Only a block whose actual `kind` is `commentary` may be excluded for
narrator replacement or used as a commentary visual. Every source block is
either selected once or listed once in `excluded_blocks`.

See `examples/contracts/remix_plan.json`.

## 7. `commentary_script.json`

R4 returns exactly one Japanese script item for each plan commentary slot. Each
item repeats the slot duration/character budget, evidence IDs, adjacency, tone
tags, and deterministic QA flags. All QA flags must pass before TTS.

The Playwright LLM returns only `{slot_id, text_ja}`. Code attaches evidence,
budgets, adjacency, and hashes. The script never contains rewritten participant
reaction speech.

See `examples/contracts/commentary_script.json`.

## 8. TTS and Audio Contracts

### `commentary_audio.json`

The voice policy is locked to AI33, voice
`elevenlabs_QPtBgsg1dxKTQHNpHrHt`, model label
`eleven_multilingual_v2`, speed `1.0`, no fallback, and normalization
`ja_basic`. Each item records its real file hash, cache key, measured duration,
LUFS, true peak, requested/actual model, and Japanese ASR similarity.

See `examples/contracts/commentary_audio.json`.

### `commentary_fit_requests.json`

When measured TTS exceeds the permitted tolerance, is materially short, or
scores below the Japanese ASR threshold, R5 writes only the affected slot IDs
with actual/target/max durations, tolerance, direction, attempt `1..2`, and
reason. `direction` is `shorten`, `lengthen`, or `clarify`; `clarify` replaces
hard-to-pronounce wording while preserving evidence and duration budget. The
orchestrator sends those slots back through R4 and R5; speech is never
time-stretched.

### `audio_assets.json`

The stems stage records the full-source Demucs `no_vocals` asset with content
hash, source hash, duration, sample rate, channel count, source span, and
`leakage_detected`. A leaking asset is not used for that commentary slot; R6
falls back to TTS-only.

## 9. `remix_edl.json`

R6 creates one placement timeline with separate video and audio policies.

For `reaction`, `mixed`, and `unknown` placements:

- video and source audio use the identical source span;
- speed is `1.0`;
- source gain is `0 dB`;
- video and audio filters are empty.

The same source span must remain present on the output timeline. Planner and
composer code cannot remove, promote, or repurpose a protected block because it
contains suspected narrator audio.

For commentary placements, R6 uses the single eligible `commentary` visual
assigned by the plan. Audio is either `tts` or `tts_bed`. The bed uses fixed
`-14 dB` gain and 180 ms fades; TTS uses `+1 dB`, 50 ms boundary fades, and a
limiter only inside the commentary mix. There is no dynamic ducking.

The visual policy forbids masking, subtitles, text, blur, overlay, delogo, or
other pixel edits. See `examples/contracts/remix_edl.json`.

## 10. Render Contracts

`render.timeline.json` stores globally quantized source/output frame and sample
ranges for every placement. Starts are exactly the previous placement's ends;
final frame/sample ends equal the declared totals.

`render.command-manifest.json` records each command and a denylist including
subtitle, ASS, drawtext, overlay, delogo, blur, and mask filters. Validation
fails when any command contains a denylisted term.

`render.meta.json` binds source, EDL, timeline, command manifest, and output
hashes to H.264 CRF18/AAC192k source-compatible output metadata.

## 11. `remix_qa.json`

R8 measures and reports; it does not silently repair media. The report includes:

- output duration and hard/preferred ratio status;
- reaction speed/gain/span mismatches, max gain delta, correlation, A/V lag,
  and frame similarity;
- AI33 provider/voice provenance, Japanese ASR similarity, narrator leakage
  count for replaced commentary placements, and localized
  `old_narrator_leakage_slot_ids` used for per-slot repair;
- `protected_narrator_overlap_block_ids`, a warning-only audit list for
  narrator speech intentionally retained inside protected `mixed`/`unknown`;
- forbidden visual operation counts;
- unexpected silence, boundary clicks, commentary/full/source true peaks, and
  peak increase;
- decode, frame/sample tiling, gaps, overlaps, warnings, and repair history;
- expected versus actually decoded video-frame and audio-sample counts, including
  signed deltas. V1 permits at most one video frame and one output-frame worth
  of AAC sample padding.

Reaction preservation samples the head, middle, and tail of each protected
placement after decoding the placement once. Head/tail windows are at most
`0.5s`; each probe requires correlation `>=0.98`, lag no more than one frame,
gain delta `<=0.3 dB`, and frame similarity `>=0.995`. Failed placements are
localized in `failed_placement_ids`, including boundary-frame failures after
clamping to decoded source/output frame counts. Identical silent windows count
as preserved with zero gain delta; one-sided silence still fails.

Accepted repair state is an internal orchestrator artifact rather than a new
cross-stage contract. Immutable accepted requests live under
`work/orchestrator/accepted_repairs/`; `accepted_repair_ledger.json` binds the
source hash, request hash, and passing QA hash. Pending or stale
`remix_repair_requests.json` files are not replayed automatically.

Overall `status` is `fail` when any hard sub-gate fails. See
`examples/contracts/remix_qa.json`.

## 12. Example Validation

The seven committed examples use internally consistent dummy identities that
match the runtime schema. The focused regression test loads all examples and
runs cross-contract validators for source, plan, script, audio, EDL, and QA.
Live runs replace the example hashes with hashes of the actual input artifacts.

The preservation-first POC validates the additive fields in production-shaped
artifacts: `reaction-segment-v7`, `reaction-plan-v8`, `reaction-write-v2`,
`reaction-render-v4`, and `reaction-qa-v7`. Its decoded timeline delta is
`-1` frame and `-1318` samples, both inside the declared one-output-frame
tolerance.
