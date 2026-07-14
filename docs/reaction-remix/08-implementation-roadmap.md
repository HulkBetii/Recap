# 08 - Implementation Roadmap

> Status: **MVP AND PRESERVATION-FIRST POC PASSED / FULL-VIDEO ACCEPTANCE PENDING**. The existing recap pipeline remains unchanged.

## 1. Delivery Strategy

Reaction-remix is implemented beside the current recap workflow with a separate entrypoint:

```text
python run_reaction.py --input source.mp4 --run-dir runs/<name> --config config.reaction-remix.yaml
```

The command, file and config above are implemented.

This separation protects the released recap contracts, keeps regression risk contained and allows reaction-remix to adopt an audio-aware timeline. A unified `run.py --pipeline ...` interface may be considered only after the new workflow passes full-media acceptance tests.

## 2. Package Layout

```text
reaction_remix/
  probe/
  analyze/
  segment/
  plan/
  write/
  tts/
  compose/
  render/
  qa/
  orchestrator/
run_reaction.py
config.reaction-remix.yaml
```

This layout is implemented. Cross-stage JSON Pydantic models live in `common/schema.py`.

## 3. Stage Graph

```text
R0 Probe
  +--> R1 Analyze --+
  +--> Shots -------+--> R2 Segment --> R3 Plan --> R4 Write --> R5 TTS --+
  +--> Stems --------------------------------------------------------------+--> R6 Compose --> R7 Render --> R8 QA
```

All stage names in this graph are implemented.

- R0 `reaction_remix.probe`: record immutable source identity and media streams.
- R1 `reaction_remix.analyze`: produce mixed-language transcript and audio observations.
- R2 `reaction_remix.segment`: classify complete blocks and safe edit boundaries, using existing shots as hints.
- R3 `reaction_remix.plan`: reorder complete reactions and allocate commentary slots.
- R4 `reaction_remix.write`: write and evidence-check Japanese commentary.
- R5 `reaction_remix.tts`: synthesize commentary with the locked AI33 voice.
- R6 `reaction_remix.compose`: resolve real TTS durations, source audio modes and Demucs audio assets into `remix_edl.json`, with per-slot TTS-only fallback on leakage.
- R7 `reaction_remix.render`: render video and interleaved audio without subtitle processing.
- R8 `reaction_remix.qa`: run structural, media, audio and editorial gates.

## 4. Reuse Matrix

| Existing area | Reuse level | Boundary |
|---|---|---|
| `common/media.py` | Direct | Reuse ffmpeg/ffprobe helpers; add only generally useful helpers. |
| `common/integrity.py` | Direct | Reuse stable hashes, media identity and atomic JSON writes. |
| `shots/` | Direct/configured | Reuse detection/features; do not treat channel branding as disposable intro by default. |
| `review/playwright_chat.py` | Adapter | Reuse browser transport/recovery, not Vietnamese recap prompts. |
| `review/session.py` | Adapter | Reuse one-session-per-run behavior. |
| `tts/providers.py` | Direct/adapter | Reuse AI33 runtime and retry behavior. |
| `tts/cache.py` | Adapter | Reuse cache pattern with commentary item identity. |
| `render/cache.py` | Adapter | Reuse media-aware temp cache. |
| `render/quantize.py` | Concept/refactor | Reuse frame-lock principles with a new timeline model. |
| Recap `review_script.json`, `beats_timing.json`, `edl.json` | No | Audio and editorial semantics do not match dedicated `commentary_script.json`, `commentary_audio.json` and `remix_edl.json`. |
| Recap match/storymap prompts | No | They summarize films instead of preserving and reordering reactions. |
| Continuous voiceover mux | No | Reaction-remix interleaves source audio and TTS. |

Refactoring shared code is allowed only when the existing recap tests remain unchanged and green.

## 5. Incremental Phases

### Phase 0 - Documentation and Decisions

Deliverables:

- Product requirements and locked behavior.
- Pipeline and JSON contract examples.
- Editorial, audio, render and QA documents.
- Branch `codex/reaction-remix-plan` pushed to the GitHub remote.

Exit gate:

- Every runtime name was documented before implementation began.
- Duration hard floor, subtitle policy, AI33 voice and reaction-preservation rules are unambiguous.
- No production code is changed.

### Phase 1 - Contracts and Synthetic Fixtures

Deliverables:

- Pydantic models for source segments, remix plan, commentary audio, timeline and QA.
- Deterministic validators and JSON examples.
- Synthetic multilingual fixtures; no copyrighted source video committed.

Exit gate:

- Valid fixtures round-trip through Pydantic.
- Invalid segment references, partial durations, timeline gaps/overlaps and reaction speed changes fail tests.
- Existing recap schema tests remain green.

### Phase 2 - Probe, Analysis and Segmentation

Deliverables:

- Implemented R0-R2 CLIs, mixed-language ASR path and conservative type classifier.
- Safe reaction-unit boundary builder.
- Analyzer cache/meta and a local QA view.

Exit gate:

- The three known POC narrator anchors are accounted for within approximately
  `1 second`: isolated cores are replaceable and overlapping tail material is
  protected with a reason.
- Known reaction spans are never classified as replaceable commentary.
- Every `mixed` or `unknown` span is preserved by default.
- A full-video local audit accounts for all `11` known narrator blocks before
  full R3-R8 acceptance proceeds.

Status: passed on the preservation-first POC with segment algorithm
`reaction-segment-v7` and policy `strict_or_word_edge`. The complete
`184.201633s` timeline is covered by `39` blocks. `block-0003` and
`block-0026` are replaceable narrator cores; `block-0038` and `block-0039`
remain protected overlap. Full R0-R2 v5/v7 has now been rerun on the
`1129.302494s` source: 191 blocks cover the timeline, all 11 narrator anchors
have a replaceable core, and overlap portions at anchors 6, 8, and 11 remain
protected `unknown/mixed`. Full R3-R8 hard QA has now passed; after listening
QA, `block-0045` was manually dropped as a hard segment with audible old
commentary. The accepted output is `0.894693x` and shorter than the source.

### Phase 3 - Editorial Plan and Writing

Deliverables:

- Implemented R3-R4 Playwright-first prompts and validated JSON parsing.
- Reaction reordering, separate Japanese commentary writing and deterministic duration budgeting.
- Per-run ChatGPT session metadata and cache identity.

Exit gate:

- All referenced source IDs exist.
- No unmarked duplicates, partial utterances or reaction speed changes.
- Estimated output is `80-100%` of source and normally `85-90%`.
- No non-commentary block is excluded; every commentary exclusion maps to one
  replacement slot.

POC evidence: planner prompt `reaction-plan-v8`, two commentary slots,
predicted duration `176.08766425s`, output ratio `0.9559506`, and unique
reaction speech retention `1.0`. Writer prompt `reaction-write-v2` produced two
evidence-bound Japanese lines within deterministic budgets.

### Phase 4 - Japanese TTS and Demucs Stems

Deliverables:

- Implemented R5 AI33 commentary synthesis using `elevenlabs_QPtBgsg1dxKTQHNpHrHt`.
- Japanese-safe text handling and script auto-fit loop.
- Demucs commentary beds with per-slot TTS-only fallback.

Exit gate:

- All commentary uses the locked voice and `1.0x` speed.
- Actual audio duration fits its planned capacity within `100 ms` or one frame.
- No old narrator remains intelligible in prepared replacement-commentary
  audio; protected overlap remains outside this gate.
- Successful items resume from cache after an injected provider failure.

POC evidence: two AI33 assets use
`elevenlabs_QPtBgsg1dxKTQHNpHrHt` and `eleven_multilingual_v2`; both Japanese
ASR similarities are `1.0`, and no fit request remains.

### Phase 5 - Compose, Render and Media QA

Deliverables:

- Implemented R6 `remix_edl.json` composition.
- Implemented R7 frame-locked H.264/AAC renderer with original reaction audio.
- Implemented R8 deterministic media QA.
- No-subtitle-processing command manifest.

Exit gate:

- Timeline has zero gaps/overlaps and all reaction clips remain `1.0x`.
- No blur, drawtext, subtitle, mask or overlay filter is present.
- POC output decodes completely and remains within `144-180` seconds.
- Reaction audio lag is at most `20 ms` and sampled correlation is at least `0.98`.

Status: passed on the POC with renderer `reaction-render-v4` and QA
`reaction-qa-v7`. Output is `176.609767s` (`0.958785`), correlation
`0.9986287014`, lag `0 ms`, gain delta `0.0691078578 dB`, frame similarity
`0.9950234368`, replacement-slot leakage `0`, and peak increase `0.1 dB`.
Decoded counts differ from the render timeline by `-1` frame and `-1318`
samples, both inside the configured one-frame tolerance.

### Phase 6 - Orchestrator, Resume and QA

Deliverables:

- Implemented `run_reaction.py` CLI, dry-run, `--from`, `--to`, `--only`, `--force-stage` and downstream invalidation.
- Run summary, cache reporting and machine-readable QA artifacts.
- Unit/mock suite following current repository patterns.

Exit gate:

- Changing a planner input reruns planner and downstream only.
- Changing one narration item resynthesizes that item and downstream only.
- Corrupt artifacts are detected rather than silently reused.
- Existing recap tests and new reaction-remix tests all pass.

### Phase 7 - Full-Video Acceptance

Deliverables:

- Full render of the authorized `18:49.302` source.
- QA JSON, representative frames, audio diagnostics and local review HTML.
- Updated README, AGENTS source-of-truth and PROJECT_LOG.

Exit gate:

- Final duration is at least `15:03.4`, at most `18:49.302`, and preferably `16:00-16:30`.
- Full-file decode passes with no black edit intervals, clipping or A/V drift.
- All reaction content decisions and exclusions are auditable.
- Manual review confirms coherent order, preserved reaction meaning and no subtitle visual processing.

Current status: full authorized-video hard QA passed in
`work/reaction-remix-full-r02`. Full R0-R2 accounts for all `11` narrator
anchors, and full R3-R8 produced a decodable `1010.379002s` output with all
hard preservation/audio/visual/leakage gates passing after manual dropping
`block-0045`. Exact target length is audit-only because the output is shorter
than the original.

## 6. Test Plan

Implemented test areas:

```text
tests/test_reaction_schema.py
tests/test_reaction_probe.py
tests/test_reaction_analyze.py
tests/test_reaction_segment.py
tests/test_remix_plan.py
tests/test_remix_write.py
tests/test_remix_tts.py
tests/test_remix_stems.py
tests/test_remix_compose.py
tests/test_remix_render.py
tests/test_reaction_orchestrator.py
tests/test_remix_qa.py
```

Automated tests should mock browser, TTS, ffmpeg and heavy ML providers. Real-media tests remain explicit local smoke tests using the authorized source under ignored `work/` or `runs/` directories.

## 7. Definition of Done

Reaction-remix is production-ready only when:

- The complete graph is implemented with validated file contracts and resume behavior.
- The output duration is never silently reduced below `80%`.
- Reaction units retain original meaning, audio, `1.0x` speed and safe speech boundaries.
- Only Japanese editorial commentary is rewritten and synthesized with the locked AI33 voice.
- The renderer performs no subtitle masking, generation or restyling.
- All structural, audio, visual and editorial hard gates pass on the POC and
  full authorized video.
- POC and full authorized-video hard QA are complete; the preferred
  full-duration warning has been accepted as audit-only before PR/release.
- The existing recap pipeline remains backward-compatible and its complete test suite stays green.
