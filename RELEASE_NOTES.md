# Recap v1.0.3

Release date: 2026-11-03

`v1.0.3` collects the work since `v1.0.2`: anime season recaps, the local control-room UI, the 9router text backend, and footage matched sentence by sentence so the narration plays over the moment it describes.

## Features

- Anime support: Japanese ingest, anime single/season presets, manual OP/ED/preview ranges and context files, and `python -m series_recap` for chaptered season recaps (`series_composer -> tts -> series_match -> render`).
- Opt-in enhanced season post-production (`config.anime.series.vieneu.enhanced.yaml`): dynamic clip lengths, reframing, grading, transition ramps, guarded freezes, and local music/SFX mixed under the voiceover with no source audio.
- Local web UI (`python -m recap_ui`, loopback only) that supervises the public CLIs with a FIFO queue, guided new-run flow, run discovery and delivery QA.
- 9router gateway (`common/llm_gateway.py`) as the default text backend for translation, vision, review and the season composer, with adaptive concurrency, an in-gateway fallback model and a readiness probe before heavy stages. ChatGPT Playwright stays selectable; there is no automatic fallback between the two.
- VieNeu TTS on SDK 3.8 with a CUDA backend, reference-clip voice cloning (production preset: Ngọc Huyền clone) and batched `infer_batch` synthesis. CapCut is available as an explicit provider.
- Sentence-anchored footage (production preset): GĐ2 maps every narration sentence to source segments, GĐ3 times sentences against real pauses in the voiceover, and GĐ5 fills each sentence window from source not yet shown.
- Opt-in experiments kept off by default because they did not beat judge noise: per-sentence SigLIP ranking, the VLM `shot_pick` stage, and sentence anchors for season recaps (no season was available to run).
- `scripts/av_sync_audit.py` scores narration against the frame on screen with a multimodal judge.

## Fixes

- GĐ2 QA converges: rewrites see the source segments of their beat, later QA rounds re-check only rewritten beats, and issues of one beat are merged into one rewrite.
- GĐ4 uses YuNet face detection and an overlay guard that drops shots with burned-in ad text; hard-subtitle sources raise a warning.
- GĐ6 renders with NVENC when available, at the source frame rate, and removes unused temp clips after a successful mux.
- Runtime is isolated in a uv `.venv`; runtime preflight reports packages that drift from the declared specifiers, and GĐ1 frees GPU memory after ASR.
- The release gate works from a venv and covers the new packages.

## Compatibility

- Required stage JSON contracts are unchanged. Sentence sidecars (`review_script.sentences.json`, `sentence_timing.json`, `sentence_shot_scores.json`) are optional; without them GĐ5 matches per beat as before.
- Cache identities changed for GĐ2 (`review-prompts-v5`), GĐ5 (`MATCH_ALGORITHM_VERSION=7`) and season match (`series-match-v5-sentence-units`), so existing runs rebuild those stages once.
- `NINEROUTER_API_KEY` is read from the environment only. Existing `v1.0.0`, `v1.0.1` and `v1.0.2` tags remain immutable.

## Validation

- Benchmark film (`Quý Ông Số Đỏ`, 7,406 s): full run from scratch 75 -> 48.8 minutes; recap length 49% -> 96-106% of target; GĐ2 QA passes; zero ad-overlay placements.
- Narration/picture agreement measured by the audit judge: 0.527 per beat -> 0.638 per sentence.
- Uncached VieNeu TTS for 62 beats: 192 s -> 117 s.
- `python -m pytest -q` -> 832 passed; the local release gate with media smoke passed all 19 steps.

---

# Recap v1.0.2

Release date: 2026-07-13

`v1.0.2` is a patch release that locks review work to the Playwright-first policy and hardens long-running ChatGPT and TTS production sessions.

## Fixes

- GĐ2 now accepts only `chatgpt_playwright` as its primary text backend and enforces the canonical persistent profile at `D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1`; legacy direct `openai_api` and `off` configurations fail clearly.
- Playwright failures are classified before retry or fallback. The default two-attempt policy recovers the same submitted response for up to 60 seconds without sending a duplicate prompt.
- Resumed ChatGPT conversations wait for existing history to stabilize before counting assistant messages, preventing a stale response from being mistaken for the new answer.
- OpenAI review fallback remains opt-in and lazy. It runs only after an eligible Playwright failure exhausts recovery, the budget guard allows it, and `OPENAI_API_KEY` is available; usage reports record configured, allowed, blocked, and triggered states.
- Automatic paid ASR fallback now requires a severe classified alignment/timecode failure instead of approximate metadata alone.
- AI33/Genmax polling continues through exhausted transient `429`, `5xx`, and network request retries until the provider task deadline, allowing an already submitted TTS task to finish instead of failing the beat early.

## Compatibility

- No required JSON contract between pipeline stages changed.
- Playwright success does not initialize or call OpenAI, and `api_budget_guard=block` still permits Playwright while blocking paid fallback in every quality mode.
- ASR, vision, TTS, and media remain local/provider-first because they require artifact contracts that browser automation cannot reliably produce.
- Existing `v1.0.0` and `v1.0.1` tags remain immutable.

## Validation

- Full production GĐ2 cleanup completed with 18 beats, zero final QA issues, and `0.8841` coverage.
- The rebuilt production EDL contains 394 placements with no gaps, overlaps, reuse, or widening; browser visual QA found no media or console errors.
- Local CUDA listening QA found no truncation or long silence in key beats, with transcript similarity from `0.9692` to `0.9885`; no OpenAI API was used for this QA.
- Final production output is H.264/AAC, 1920×1080 at 30fps, `1324.092s`, with `duration_match=true`.
- Full automated suite passed with `386` tests; compileall and `git diff --check` also passed before the release gate.

---

# Recap v1.0.1

Release date: 2026-07-13

`v1.0.1` is a patch release focused on long-running Playwright review reliability, auditable OpenAI fallback behavior, and safer GĐ5 intra-beat splicing.

## Fixes

- GĐ2 now waits for a newly created assistant response before considering ChatGPT streaming complete, preventing a previous response from being mistaken for the new answer.
- Playwright response text stabilization has a bounded deadline, so a stalled page fails clearly instead of waiting indefinitely.
- Added an opt-in `review.openai_fallback_model` circuit breaker. ChatGPT through Playwright remains primary; OpenAI activates only after a proven browser failure and handles the remaining GĐ2 requests.
- OpenAI review fallback retries transient failures, records model and token usage in `work/review/openai_usage.json`, participates in API budget blocking, and remains visible in fallback/cost reports after partial reruns.
- GĐ5 intra-beat splicing trims replacement boundaries to preserve `min_visual_clip` for both retained baseline fragments and replacement fragments, avoiding flash cuts caused by tiny leftovers.

## Compatibility

- No required JSON stage contract changed.
- OpenAI review fallback is disabled by default and requires both explicit configuration and `OPENAI_API_KEY`.
- Existing Playwright-only, TTS, matching, rendering, and cache workflows remain compatible.

## Validation

- Real Playwright-first E2E completed for `Toan-Tri-Doc-Gia.mp4`.
- Final GĐ2 QA passed with zero issues; final EDL contained 391 placements with no timeline gaps or overlaps.
- Playwright QA loaded all 138 EDL thumbnails and four representative 1920×1080 frames.
- Full automated suite passed with `350` tests before the release gate.

---

# Recap v1.0.0

Release date: 2026-07-12

`v1.0.0` is the first production-ready release of Recap: a local-first pipeline that turns one movie or episode into a Vietnamese 1080p recap video with synchronized voiceover and automatically selected footage.

## Highlights

- End-to-end orchestration from video preflight and transcript ingestion through review writing, TTS, shot analysis, footage matching, and final rendering.
- Timecode-first contracts between stages, with Pydantic validation, resumable file artifacts, selective cache invalidation, and deterministic QA outputs.
- Korean and Vietnamese source workflows, including Faster Whisper, optional WhisperX alignment, manual/OpenAI ASR modes, glossary correction, and visual-gap analysis.
- ChatGPT Playwright review workflow with per-video sessions, story mapping, cold-open hooks, Vietnamese recap style controls, consistency checks, and readability QA.
- AI33/VBee TTS with Genmax and OpenAI fallback, per-beat caching, retry/backoff, pronunciation QA, and measured audio timing.
- Offline shot detection, visual features, non-story and end-credit exclusion, optional SigLIP2 visual indexing, and BGE-M3-assisted chronological matching.
- Frame-locked 1080p H.264 rendering with muted source audio, no captions or background music, cached clip rendering, and non-black freeze-frame tail padding.
- Stable, visual, Vietnamese, low-cost, balanced, and CUDA production presets for common runtime choices.

## Quality And Release Gate

- GitHub Actions Windows/Python 3.11 release gate passed on the pre-release commit.
- Clean local gate passed with zero secret findings, `344` tests, compile checks, wheel build/install/import, CLI help, production dry-run, and real-media smoke.
- Media smoke passed all `33` assertions covering unchanged cache reuse, profile-only invalidation, glossary invalidation, and film identity rebuilds.

## Known Limitations

- The pipeline processes one input video per run; series-level memory is not included in v1.0.0.
- The production review flow requires a valid persistent ChatGPT browser session.
- GPU-heavy alignment, semantic embedding, and visual indexing remain optional and require their extra dependencies.
- TTS and selected ingest modes require provider credentials; Content ID risk is reduced by muted, fragmented footage but cannot be eliminated.
