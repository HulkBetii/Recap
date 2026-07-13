# Reaction Remix Pipeline Architecture

Status: Proposed design only. None of the runtime names, commands, packages, or
contracts in this document are implemented yet.

## 1. Purpose

Reaction Remix is a new pipeline beside the existing Recap pipeline. It takes
one already-edited reaction video and produces a new edit that:

- may reorder complete reaction blocks to create a new narrative;
- keeps the picture, source audio, playback speed, and burned-in subtitles of
  every retained reaction block;
- removes or replaces only the original Japanese editorial commentary audio;
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
Remix therefore introduces new proposed contracts and a separate renderer.
Existing film_map.json, review_script.json, beats_timing.json, shots.json, and
edl.json stay unchanged.

## 3. Proposed DAG

~~~mermaid
flowchart LR
    P["R0 Probe"] --> A["R1 Analyze"]
    P --> S["Existing Shots (cut hints only)"]
    P --> M["Optional Local Stems"]
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

Shots and stems are parallel helpers. Shots provide scene boundaries and
thumbnails; they do not authorize arbitrary 3-5 second matching. Stems provide
an optional music/ambience bed for replacement commentary and never replace the
original audio of reaction blocks.

## 4. Proposed Stage Responsibilities

| Stage | Proposed command | Required inputs | Primary outputs | Responsibility |
| --- | --- | --- | --- | --- |
| R0 Probe | python -m reaction_remix.probe | source video | reaction_source.json | Record immutable source identity and media streams. |
| R1 Analyze | python -m reaction_remix.analyze | source video, reaction_source.json | reaction_transcript.json, analysis.meta.json | Local multilingual ASR, VAD, language hints, optional speaker hints. |
| R2 Segment | python -m reaction_remix.segment | transcript, shot hints | reaction_blocks.json | Build atomic reaction/commentary/transition blocks and safe cut points. |
| R3 Plan | python -m reaction_remix.plan | reaction_blocks.json | remix_plan.json, plan.qa.json | Reorder blocks, allocate commentary slots, and enforce duration/retention. |
| R4 Write | python -m reaction_remix.write | plan, blocks, transcript | commentary_script.json, script.qa.json | Write only Japanese editorial commentary and verify evidence. |
| R5 TTS | python -m reaction_remix.tts | commentary_script.json | commentary_audio.json, audio files | Synthesize and cache one AI33 file per commentary slot. |
| R6 Compose | python -m reaction_remix.compose | blocks, plan, commentary audio, optional stems | remix_edl.json | Resolve the mixed source/TTS timeline without rendering media. |
| R7 Render | python -m reaction_remix.render | source video, remix_edl.json | reaction_remix.mp4, render.meta.json | Render source A/V and commentary placements with no visual edits. |
| R8 QA | python -m reaction_remix.qa | source, EDL, rendered video | remix_qa.json, optional QA HTML | Run deterministic contract, media, sync, audio, and preservation gates. |

An independent proposed orchestrator, run_reaction.py, should call these stages
through subprocesses. The existing run.py and orchestrator graph must not be
modified in the first implementation phase.

## 5. Proposed Package Boundaries

~~~
reaction_remix/
  __init__.py
  probe/
    __main__.py
    media_probe.py
  analyze/
    __main__.py
    asr.py
    language.py
    speakers.py
  segment/
    __main__.py
    cut_points.py
    classify.py
    blocks.py
  plan/
    __main__.py
    prompts.py
    validator.py
    duration.py
  write/
    __main__.py
    prompts.py
    style.py
    evidence.py
  tts/
    __main__.py
    fit.py
  compose/
    __main__.py
    scheduler.py
    audio_policy.py
  render/
    __main__.py
    clips.py
    audio.py
    concat.py
  qa/
    __main__.py
    contracts.py
    preservation.py
    audio.py
    report.py
  orchestrator/
    config.py
    graph.py
    runner.py
run_reaction.py
config.reaction-remix.yaml
~~~

If implemented, all file-interface Pydantic models should live in
common/schema.py to follow the current repository source-of-truth rule. Stage
internals may use private dataclasses, but packages must communicate only
through validated files.

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

The proposed narrative sequence is:

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

The removal order is fixed:

1. dead air and safe handles;
2. repetitive original commentary;
3. redundant transitions;
4. duplicate reaction ideas;
5. unique reaction speech only as a last resort.

The proposed default retains at least 90 percent of unique reaction speech.
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
OpenAI fallback is proposed as disabled by default. ASR, media, stems, and TTS
are not browser-suitable tasks.

## 10. TTS Policy

The production proposal locks:

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

For replacement commentary:

- the video may use an eligible b-roll, transition, or original commentary
  visual span;
- the original mixed commentary audio is never used;
- audio is AI33 TTS, optionally mixed with a local no-vocals bed;
- bed fades and limiting apply only to the commentary mix;
- burned-in source text may remain because masking is explicitly forbidden.

Visual selection priority:

1. neutral b-roll or transition with no intelligible reaction speech;
2. commentary-support visual with low OCR text activity;
3. original commentary visual as fallback, preserving its burned-in pixels.

The renderer should preserve source dimensions, frame rate, and aspect ratio
when all placements come from one source. It must not invoke subtitles, ass,
drawtext, overlay, delogo, blur, or caption-generation filters.

## 12. Cache And Selective Invalidation

Every stage should write a cache manifest with input hashes, config hash,
algorithm version, output hashes, and creation time. Outputs are skipped only
after schema and integrity validation.

| Change | Preserve | Invalidate |
| --- | --- | --- |
| Source identity changes | nothing | every stage |
| ASR model/chunking changes | probe, optional stems | analyze and downstream |
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

The proposed orchestrator supports dry-run, from, to, only, force, and
force-stage behavior without changing the current Recap orchestrator.

Automatic repair order:

1. Output below the hard duration floor: restore the highest-value omitted
   reaction blocks.
2. Output above the preferred range: shorten commentary, remove dead air, then
   remove duplicate transitions or reaction ideas.
3. TTS too long: rewrite only the offending commentary slot.
4. Original narrator leakage: regenerate the local bed or remove the bed.
5. Reaction preservation failure: rerender only the failed placement with
   source audio mode and no filters.
6. Decode or timeline failure: rerender affected cached clips, then the final
   concat.

No automatic repair may mask subtitles, change reaction playback speed, alter
reaction gain, or substitute a different TTS provider.

## 14. Acceptance Boundary

The architecture is ready for implementation only after the proposed contracts
in 03-data-contracts.md are accepted. Until then:

- no command in this document should be considered runnable;
- no proposed config should be passed to the existing Recap config loader;
- no current Recap schema may be changed to emulate these contracts;
- documentation must continue to label Reaction Remix as proposed or planned.
