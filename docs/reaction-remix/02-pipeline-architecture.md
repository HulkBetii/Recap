# Reaction Remix Pipeline Architecture

Status: MVP runtime implemented. The preservation-first POC passed; full
authorized-video acceptance remains a release gate.

## 1. Purpose

Reaction Remix is a new pipeline beside the existing Recap pipeline. It takes
one already-edited reaction video and produces a new edit that:

- may reorder complete reaction blocks to create a new narrative;
- keeps the picture, source audio, playback speed, and burned-in subtitles of
  every retained reaction block;
- removes or replaces only source blocks classified as Japanese editorial
  `commentary`;
- preserves `mixed` and `unknown` blocks, including narrator/reaction overlap,
  as untouched source video/audio;
- writes new Japanese commentary in the configured channel style;
- synthesizes that commentary with AI33 voice
  elevenlabs_QPtBgsg1dxKTQHNpHrHt;
- does not mask old subtitles and does not add new subtitles, captions, text,
  blur, delogo, or overlays;
- prefers an output between 85 and 90 percent of the source duration and never
  goes below 80 percent unless a future explicit override changes the policy.

The current Recap pipeline remains the production path for Korean-drama recap
videos. Reaction Remix must not change its behavior or file contracts.

## 2. Why This Is A Separate Pipeline

The existing Recap contracts encode assumptions that are incompatible with a
reaction edit:

- review_script.json describes narration over source footage, not a sequence of
  preserved reaction and replacement-commentary blocks;
- beats_timing.json describes one continuous voiceover timeline;
- edl.json assumes short video-only placements and does not describe source
  audio preservation or per-placement audio modes;
- match deliberately cuts footage into small clips and may reuse or speed-fit
  shots;
- render removes source audio and muxes one global voiceover.

Changing those contracts would risk regressions across G2 through G6. Reaction
Remix therefore introduces new dedicated contracts and a separate renderer.
Existing film_map.json, review_script.json, beats_timing.json, shots.json, and
edl.json stay unchanged.

## 3. Runtime DAG

~~~mermaid
flowchart LR
    P["R0 Probe"] --> A["R1 Analyze"]
    P --> S["Shots (cut hints only)"]
    P --> M["Stems (Demucs no_vocals)"]
    A --> G["R2 Segment"]
    S --> G
    G --> L["R3 Plan"]
    L --> W["R4 Write"]
    W --> T["R5 TTS"]
    G --> C["R6 Compose"]
    L --> C
    T --> C
    M --> C
    C --> R["R7 Render"]
    R --> Q["R8 QA"]
~~~

Analyze, shots, and stems start in parallel after probe. Shots provide scene
boundaries and thumbnails; they do not authorize arbitrary 3-5 second matching.
The production preset requires Demucs and creates a full-source `no_vocals`
bed. A leaking bed falls back to TTS-only for the affected commentary slot and
never replaces or processes the original audio of reaction blocks.

## 4. Stage Responsibilities

| Stage | Command | Required inputs | Primary outputs | Responsibility |
| --- | --- | --- | --- | --- |
| R0 Probe | python -m reaction_remix.probe | source video | reaction_source.json | Record immutable source identity and media streams. |
| R1 Analyze | python -m reaction_remix.analyze | source video, reaction_source.json | reaction_transcript.json, analysis.meta.json | Local multilingual ASR, language verification, and speaker clustering. |
| R2 Segment | python -m reaction_remix.segment | transcript, shot hints | reaction_blocks.json | Build atomic reaction/commentary/transition blocks and safe cut points. |
| R3 Plan | python -m reaction_remix.plan | reaction_blocks.json | remix_plan.json, plan.qa.json | Reorder blocks, allocate commentary slots, and enforce duration/retention. |
| R4 Write | python -m reaction_remix.write | plan, blocks, transcript | commentary_script.json, script.qa.json | Write only Japanese editorial commentary and verify evidence. |
| R5 TTS | python -m reaction_remix.tts | commentary_script.json | commentary_audio.json, audio files | Synthesize and cache one AI33 file per commentary slot. |
| R6 Compose | python -m reaction_remix.compose | blocks, plan, commentary audio, audio assets | remix_edl.json | Resolve the mixed source/TTS timeline without rendering media. |
| R7 Render | python -m reaction_remix.render | source video, remix_edl.json | reaction_remix.mp4, render.timeline.json, render.command-manifest.json, render.meta.json | Render source A/V and commentary placements with no visual edits. |
| R8 QA | python -m reaction_remix.qa | source, EDL, rendered video | remix_qa.json, optional QA HTML | Run deterministic contract, media, sync, audio, and preservation gates. |

The independent `run_reaction.py` orchestrator calls these stages through
subprocesses. The existing `run.py` and recap orchestrator graph remain unchanged.

## 5. Package Boundaries

~~~
reaction_remix/
  __init__.py
  _artifacts.py
  probe/
    __main__.py
    media_probe.py
  analyze/
    __main__.py
    asr.py
    core.py
    language.py
    regions.py
    speakers.py
  segment/
    __main__.py
    cut_points.py
    classify.py
    blocks.py
    review_html.py
  plan/
    __main__.py
    core.py
    models.py
    prompts.py
    session.py
  write/
    __main__.py
    core.py
    japanese.py
    models.py
    prompts.py
  tts/
    __main__.py
    asr.py
    audio.py
    cache.py
    core.py
    japanese.py
  stems/
    __main__.py
  compose/
    __main__.py
    cache.py
    composer.py
  render/
    __main__.py
    cache.py
    commands.py
    engine.py
    quantize.py
  qa/
    __main__.py
    checks.py
    report_html.py
  orchestrator/
    commands.py
    config.py
    graph.py
    paths.py
    runner.py
    runtime.py
    summary.py
    validation.py
run_reaction.py
config.reaction-remix.yaml
~~~

All file-interface Pydantic models live in `common/schema.py` to follow the
repository source-of-truth rule. Stage internals may use private dataclasses,
but packages communicate only through validated files.

Package dependency rules:

1. A stage may import common utilities and provider adapters.
2. A stage must not import another stage's orchestration or mutate its outputs.
3. The orchestrator passes paths and invokes stage CLIs; it does not pass
   in-memory objects.
4. Planning and writing must not call media render code.
5. Rendering must not select, reorder, rewrite, or reclassify content.
6. QA may inspect all artifacts but must write only QA and repair-request
   artifacts.

## 6. Reuse From Recap

### Reuse directly

- common/media.py for ffmpeg, ffprobe, duration probing, and command handling.
- common/integrity.py patterns for file identity, SHA-256, and config hashing.
- Review Playwright session, retry, recovery, parsing, and budget policy
  patterns.
- tts/providers.py for AI33 submission, polling, download, retry, and headers.
- TTS per-item cache and manifest-save-after-success pattern.
- shots output as optional scene-cut hints and QA thumbnails.
- render frame quantization and temp-cache-key patterns where their assumptions
  remain valid.
- Orchestrator patterns for dry run, stage ranges, force-stage, downstream
  invalidation, logs, and summary artifacts.

### Reuse only after extracting a language-neutral interface

- Ingest chunking and transcript QC. Current SourceLanguage and FilmMapSegment
  are Korean/Vietnamese recap contracts and must not be widened to carry
  reaction-specific semantics.
- Playwright adapter code may later move to a common provider module with
  compatibility imports so the existing review package keeps working.

### Do not reuse

- ReviewBeat and review_script.json.
- BeatTiming and the concatenated voiceover assumption.
- match candidate scoring, widening, shot reuse, and 3-5 second fill.
- EdlPlacement and edl.json.
- Current render video-only cuts and global voiceover mux.

## 7. Segmentation And Safe Cuts

R2 treats a reaction block as an atomic editorial unit. It may include multiple
source shots when they belong to one uninterrupted speaker reaction. The
planner may reorder independent blocks, but it must preserve internal block
order and declared sequence dependencies.

Safe cut points are generated by code from:

- ASR word or turn boundaries;
- a local silence of the configured minimum duration;
- a nearby scene cut;
- a small speech-handle padding to avoid clipped consonants;
- optional channel transition boundaries.

The configured v1 policy is `strict_or_word_edge`:

- `source_boundary` marks the start or end of the source;
- `full_handle` has at least `120 ms` of content-free handle on both sides;
- `word_edge` may have shorter non-negative handles, cuts through no word,
  separates non-overlapping content, and is capped at confidence `0.90`;
- `overlap` has intersecting word/speaker content, is capped at `0.89`, and
  forces the adjacent narrator material to remain `mixed`/`unknown`.

A commentary block is replaceable only when both boundaries are
`full_handle` or `word_edge` and language, speaker, and boundary confidence are
all at least `0.90`. Segment algorithm `reaction-segment-v7` invalidates older
block and downstream plan caches.

The LLM never writes source timecodes. It returns block IDs, turn IDs, and safe
cut-point IDs. Code derives and validates source spans.

Low-confidence behavior is conservative:

- unknown speech is preserved rather than rewritten;
- only high-confidence commentary audio is eligible for replacement;
- a low-confidence classification is recorded in QA;
- a future explicit override file may correct classification without rerunning
  ASR.

## 8. Planning And Duration Policy

Planning is a text task and must use ChatGPT through Playwright first. It uses a
dedicated conversation for each source identity. A submitted prompt is never
submitted twice during response recovery.

The accepted POC uses planner prompt `reaction-plan-v8` and writer prompt
`reaction-write-v2`. An input-hash change opens a new conversation under
session policy `auto`; fit prompts from a stale POC are not reused.

The narrative sequence is:

1. strong source reaction as hook;
2. short Japanese setup;
3. reaction blocks grouped by topic or escalating intensity;
4. short commentary bridges where context is required;
5. strongest contrast or punchline;
6. concise close.

Duration policy:

- hard minimum output ratio: 0.80;
- preferred minimum output ratio: 0.85;
- preferred maximum output ratio: 0.90;
- hard maximum output ratio: 1.00.

This interprets the user rule as: shortening is allowed but must never remove
more than 20 percent, and unnecessary shortening is not mandatory. For the
current 1129.302 second source, the hard floor is 903.442 seconds, while the
policy range is 959.907 to 1016.372 seconds. The editorial default for this
specific source is narrower at approximately 16:00 to 16:30; keeping up to
16:56 remains valid when the reactions are distinct and useful.

The preservation-first v1 planner retains every non-commentary block and
excludes only original `commentary` blocks that receive one replacement slot.
Duration reduction therefore comes from shorter replacement commentary, not
from deleting reactions, transitions, branding, b-roll, mixed, or unknown
material. The validator keeps the general `>=0.90` reaction-speech floor, while
the implemented planner targets full (`1.0`) retention.
Dependent multi-part reactions remain in their original internal order.

## 9. Writing And Evidence

R4 writes only the new Japanese editorial commentary. It must not rewrite,
dub, translate, or paraphrase speech inside a retained reaction block.

Every commentary slot carries evidence_block_ids. Claims about a country,
speaker, action, or outcome must be derivable from those blocks. Text QA checks:

- Japanese output;
- requested internet-commentary tone;
- no unsupported facts;
- no duplicated explanation;
- target character and duration budgets;
- a clean lead-in and lead-out for adjacent reactions.

ChatGPT Playwright is primary for planning, writing, and text-heavy QA. Paid
OpenAI fallback is disabled. ASR, media, stems, and TTS
are not browser-suitable tasks.

## 10. TTS Policy

The production configuration locks:

- provider: ai33;
- voice ID: elevenlabs_QPtBgsg1dxKTQHNpHrHt;
- speed: 1.0;
- text normalization: basic or off, never Vietnamese normalization;
- provider fallback: none unless a later explicit product decision changes it.

Audio is synthesized per commentary slot, not concatenated into one global
voiceover. If a clip is too long, the write stage receives a repair request to
shorten the text. Reaction video or reaction audio is never time-stretched to
fit commentary.

## 11. Compose And Render Policy

R6 emits explicit video and audio policies for each placement.

For a retained reaction placement:

- video and audio use the same source span;
- speed is exactly 1.0;
- source gain is 0 dB;
- no audio filter, crossfade, ducking, or global normalization is applied;
- no visual filter or overlay is applied.

These rules also apply without exception to `mixed` and `unknown` placements.
They stay on the output timeline even when a narrator is audible over the
reaction. Commentary fades remain inside commentary audio only and never touch
the protected placement on either side.

For replacement commentary:

- the video uses exactly one eligible source block whose analyzed
  `kind=commentary`;
- the original mixed commentary audio is never used;
- audio is AI33 TTS, optionally mixed with a local no-vocals bed;
- bed fades and limiting apply only to the commentary mix;
- burned-in source text may remain because masking is explicitly forbidden.

V1 does not borrow `broll`, `transition`, `mixed`, or `unknown` as a commentary
visual because those blocks are retained as source placements. The selected
commentary visual preserves its burned-in pixels.

The v1 renderer preserves source dimensions, rational frame rate, aspect ratio,
sample rate, and channel count. It does not invoke subtitles, ASS, drawtext,
overlay, delogo, blur, mask, or caption-generation filters.

## 12. Cache And Selective Invalidation

Every stage writes metadata/cache state with input hashes, config identity,
algorithm version, output hashes, and creation time. Outputs are skipped only
after schema and integrity validation.

| Change | Preserve | Invalidate |
| --- | --- | --- |
| Source identity changes | nothing | every stage |
| ASR model/chunking changes | probe, shots, stems | analyze and downstream |
| Classification override changes | probe, ASR, stems, shots | segment and downstream |
| Reaction order changes | probe, ASR, blocks, stems, unchanged TTS slots | plan, compose, render, QA |
| Commentary text changes | all upstream and unrelated TTS slots | changed TTS slots, compose, render, QA |
| AI33 voice/model/speed changes | analysis, blocks, plan, script, stems | all TTS slots and downstream |
| Bed/stem settings change | analysis, blocks, plan, script, TTS | stems, compose, render, QA |
| Render codec changes | all content artifacts | render and QA |
| QA thresholds change | every production artifact | QA only |

TTS cache identity includes text hash, provider, voice, model, speed,
normalization policy, and audio-normalization settings. The manifest is saved
after every successful slot so provider failure does not discard prior work.

Playwright operational settings such as timeout or headless mode do not change
content cache identity. Conversation auto-resume is allowed only when the core
planning or writing input hash is unchanged.

## 13. Resume And Repair

The orchestrator supports dry-run, from, to, only, force, and
force-stage behavior without changing the current Recap orchestrator.

Automatic repair order:

1. Output below the hard duration floor: restore the highest-value omitted
   reaction blocks.
2. Output above the preferred range: shorten commentary within evidence and
   fit constraints; if protected material still keeps the result above `0.90`,
   report a warning instead of deleting non-commentary blocks.
3. TTS too long: rewrite only the offending commentary slot.
4. Original narrator leakage: regenerate the local bed or remove the bed for
   that commentary slot only; protected overlap placements are not changed.
5. Reaction preservation failure: rerender only IDs reported in
   `reaction_preservation.failed_placement_ids`, using source audio mode and no
   filters.
6. Decode or timeline failure: rerender affected cached clips, then the final
   concat.

Pending repair requests are never replayed merely because a stale file remains
in the run directory. After a repaired run passes every QA hard gate, the
orchestrator stores a content-addressed immutable request under
`work/orchestrator/accepted_repairs/` and writes
`accepted_repair_ledger.json`, binding the source, request, and passing QA
hashes. Only this accepted state is replayed on resume or force; corrupt,
legacy, pending, or source-mismatched state is ignored. Reaction-media repair
IDs are remapped from `origin_block_id` after any duration/compose changes so
the renderer bypasses the current placement IDs rather than stale ones.

No automatic repair may mask subtitles, change reaction playback speed, alter
reaction gain, or substitute a different TTS provider.

## 14. Acceptance Boundary

The MVP commands and contracts are runnable. The preservation-first POC passes
R0-R8 with `176.609767s` output, `1.0` unique reaction retention, correlation
`0.9986287014`, frame similarity `0.9950234368`, zero lag, zero replacement-slot
leakage, and protected overlap reported for `block-0038`/`block-0039`.

Release readiness still requires:

- full authorized-video audit and render hard gates;
- no changes to current Recap contract semantics;
- no PR, tag, or release before acceptance passes.
