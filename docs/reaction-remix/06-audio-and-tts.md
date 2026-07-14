# 06 - Audio and TTS

> Status: **R5 and local stem preparation implemented; POC and full-media hard QA passed**.

## 1. Audio Policy

Reaction-remix uses two fundamentally different audio paths:

- Reaction units keep their original source audio, including speech, laughter, room sound and music.
- Rewritten `commentary` units replace the old Japanese narrator with new
  Japanese AI33 speech. The old narrator must not remain intelligible inside a
  replaced commentary placement.
- Narrator speech inside protected `mixed`/`unknown` remains part of the source
  reaction mix and is reported for audit rather than removed.

The pipeline does not treat the output as one continuous voiceover. Original reaction audio and commentary TTS are interleaved by `remix_edl.json`.

## 2. Japanese TTS Configuration

The locked primary provider is AI33 with:

```text
provider_mode: ai33
voice_id: elevenlabs_QPtBgsg1dxKTQHNpHrHt
language: Japanese
speed: 1.0
```

The reaction-remix preset uses strict AI33 mode and fails clearly when AI33 is unavailable. Any future fallback requires an explicit product decision.

Existing reusable implementation:

- `tts/providers.py`: AI33 submission, polling, retry and download behavior.
- `tts/cache.py`: provider/text/voice-aware cache pattern.
- `common/media.py`: probing and audio normalization helpers.

Existing behavior that must not be reused unchanged:

- Vietnamese acronym and unit normalization.
- Continuous `voiceover.mp3` concatenation as the final program audio.
- Recap-specific `ReviewBeat` semantics.

Japanese text uses `ja_basic`: Unicode NFC, removal of control/zero-width characters, and collapsed whitespace. It never passes through the Vietnamese normalization path.

## 3. Script-to-Duration Fit

The commentary script should be revised to fit available visual time rather than time-stretching synthesized speech.

Implemented fitting loop:

1. Synthesize the Japanese commentary at `1.0x`.
2. Measure actual duration with `ffprobe`.
3. Compare with the planned commentary visual capacity.
4. If too long, ask the planner to shorten the same idea and synthesize again.
5. If materially too short, expand the commentary or select more appropriate source visuals.
6. If Japanese ASR similarity is below `0.90`, request a `clarify` rewrite with
   simpler TTS-friendly wording and the same evidence/budget.
7. Stop after a configured maximum number of revisions and fail with a clear QA reason if no valid fit is found.

Silence-safe trim or pad of a few frames is permitted. Speech time-stretch, pitch shifting and reaction-speed changes are not permitted.

## 4. Source Audio Modes

The timeline supports these audio modes:

- `source`: use full source audio at `1.0x`; mandatory for reaction/mixed/unknown units.
- `tts`: use AI33 TTS only.
- `tts_bed`: mix AI33 TTS with the approved Demucs `no_vocals` stem.
- `silence`: allowed only for a validated short transition.

For commentary visuals, the local-stems helper creates a non-vocal bed with Demucs. If leakage is detected, R6 Compose switches that slot to `tts`, never the original mixed audio.

The production preset requires Demucs. A missing dependency or failed
full-source separation is a hard stage error; TTS-only fallback is reserved for
individual slots with measured narrator leakage, not for global separation
failure.

Reaction units must not pass through Demucs, denoising, voice removal or loudness rewriting as a default operation.

## 5. Mix Policy

Initial measurable targets:

- Commentary TTS integrated loudness: approximately `-16` to `-12 LUFS` per block.
- Per-slot normalization reserves `0.3 dB` codec headroom while validation
  remains locked to no higher than `-2 dBTP`.
- Final output true peak: no higher than `-1.0 dBTP`.
- No sample clipping after mix or AAC encode.
- Source reaction gain remains `0 dB` and reaction placements receive no limiter, normalization, denoising, or ducking. Any clipping already present in the source is reported as inherited rather than silently altering the reaction.
- Commentary bed remains at the configured fixed gain below TTS; the current renderer applies 180 ms bed fades and does not use dynamic ducking.
- Short boundary fades should normally remain within `30-100 ms` and must not truncate speech.
- The current `50 ms` TTS boundary fade stays entirely inside commentary
  placements. Protected placements receive no fade or crossfade at either
  edge.

These are QA bounds, not a requirement to make every reaction equally loud. Natural loudness differences that belong to the original reaction should remain.

## 6. Audio Contracts

`commentary_audio.json` is implemented. Its voice policy includes the locked
provider, voice, model, speed, no-fallback declaration, and `ja_basic`
normalization. Each item records the audio path/hash, text/cache hashes,
requested and actual model, measured duration/LUFS/true peak, and Japanese ASR
similarity. The schema-valid example is
[`examples/contracts/commentary_audio.json`](examples/contracts/commentary_audio.json).

`audio_assets.json` records separated bed paths, separation identity, file hashes and leakage warnings.

## 7. Cache and Failure Behavior

- Cache each commentary item by narration, provider, voice ID, speed and normalization mode.
- Save successful manifest entries immediately so a later failure can resume.
- Retry transient AI33 network and polling errors using the existing provider policy.
- Never fall back from AI33 solely because a script does not fit; revise the script instead.
- Treat invalid Japanese text, empty audio, corrupt media and narrator leakage as validation failures.
- A failed commentary item must not force re-synthesis of already valid items.

## 8. Acceptance Gates

Audio/TTS passes only when all of the following are true:

- `100%` of commentary items use voice ID `elevenlabs_QPtBgsg1dxKTQHNpHrHt` unless an explicit future fallback policy is enabled.
- `100%` of reaction items use source audio mode `source` and playback speed `1.0`.
- Reaction audio alignment differs from its selected source by no more than one output video frame; `20 ms` is the preferred target.
- Reaction waveform correlation after render is at least `0.98` on representative unchanged spans.
- No intelligible old Japanese narrator is present in replaced commentary
  spans during ASR/manual spot checks. Protected narrator overlap is allowed
  only in reported `mixed`/`unknown` block IDs.
- TTS is not time-stretched and reports speed `1.0`.
- Commentary duration fits its allocated timeline window within `100 ms` or one output frame, whichever is larger.
- Commentary loudness lies within the documented range and each commentary mix peaks at or below `-1.5 dBTP` after encode tolerance. The full program must introduce no new clipping and must not exceed the source program true peak by more than `0.3 dB`.
- No boundary click, unintended silence longer than `250 ms`, or truncated utterance is detected.
- Cache resume produces byte-identical commentary assets for unchanged inputs.

The accepted POC synthesized two AI33 slots with the locked voice/model at
`1.0x`. Both Japanese ASR similarities are `1.0`, no fit request remains, and
replacement-slot narrator leakage is `0`. Protected narrator overlap remains
in `block-0038` and `block-0039` by design.
