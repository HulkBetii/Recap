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
