# 06 - Audio and TTS

> Status: **PROPOSED / NOT IMPLEMENTED**. R5 `reaction_remix.tts`, optional local stem preparation, `commentary_audio.json` and all related runtime behavior below are design proposals only.

## 1. Audio Policy

Reaction-remix uses two fundamentally different audio paths:

- Reaction units keep their original source audio, including speech, laughter, room sound and music.
- Rewritten commentary units replace the old Japanese narrator with new Japanese AI33 speech. The old narrator must not remain intelligible underneath.

The pipeline must not treat the output as one continuous voiceover. Original reaction audio and commentary TTS are interleaved according to the proposed remix timeline.

## 2. Japanese TTS Configuration

The locked primary provider is AI33 with:

```text
provider_mode: ai33
voice_id: elevenlabs_QPtBgsg1dxKTQHNpHrHt
language: Japanese
speed: 1.0
```

The proposed reaction-remix preset should use strict AI33 mode by default. It should fail clearly when AI33 is unavailable instead of silently changing the channel voice. Any future fallback must be an explicit product decision and must be recorded in output metadata.

Existing reusable implementation:

- `tts/providers.py`: AI33 submission, polling, retry and download behavior.
- `tts/cache.py`: provider/text/voice-aware cache pattern.
- `common/media.py`: probing and audio normalization helpers.

Existing behavior that must not be reused unchanged:

- Vietnamese acronym and unit normalization.
- Continuous `voiceover.mp3` concatenation as the final program audio.
- Recap-specific `ReviewBeat` semantics.

Japanese text should initially use the language-neutral `basic` mode or `off`; a future explicit `ja` mode may replace it. It must never pass through the Vietnamese normalization path.

## 3. Script-to-Duration Fit

The commentary script should be revised to fit available visual time rather than time-stretching synthesized speech.

Proposed fitting loop:

1. Synthesize the Japanese commentary at `1.0x`.
2. Measure actual duration with `ffprobe`.
3. Compare with the planned commentary visual capacity.
4. If too long, ask the planner to shorten the same idea and synthesize again.
5. If materially too short, expand the commentary or select more appropriate source visuals.
6. Stop after a configured maximum number of revisions and fail with a clear QA reason if no valid fit is found.

Silence-safe trim or pad of a few frames is permitted. Speech time-stretch, pitch shifting and reaction-speed changes are not permitted.

## 4. Source Audio Modes

The proposed timeline will support these audio modes:

- `original`: use full source audio at `1.0x`; mandatory for reaction units.
- `bed`: use locally separated non-vocal music/ambience under new TTS.
- `mute`: remove source audio and use only TTS; safe fallback for commentary.
- `original_branding`: retain branding audio where no narrator replacement is needed.

For commentary visuals, the proposed optional local-stems helper should create a non-vocal bed with Demucs processing. If separation fails or leaves intelligible narrator leakage, R6 Compose must use `mute`, not the original mixed audio.

Reaction units must not pass through Demucs, denoising, voice removal or loudness rewriting as a default operation.

## 5. Mix Policy

Initial measurable targets:

- Commentary TTS integrated loudness: approximately `-16` to `-12 LUFS` per block.
- Final output true peak: no higher than `-1.0 dBTP`.
- No sample clipping after mix or AAC encode.
- Source reaction gain remains `0 dB` and reaction placements receive no limiter, normalization, denoising, or ducking. Any clipping already present in the source is reported as inherited rather than silently altering the reaction.
- Commentary bed remains clearly below TTS and is automatically ducked when speech is present.
- Short boundary fades should normally remain within `30-100 ms` and must not truncate speech.

These are QA bounds, not a requirement to make every reaction equally loud. Natural loudness differences that belong to the original reaction should remain.

## 6. Proposed Audio Contracts

`commentary_audio.json` is a **proposed, not implemented** contract. The canonical proposed shape is defined in [03-data-contracts.md](03-data-contracts.md):

```json
{
  "schema_version": "reaction-remix.proposed.v1",
  "voice_policy": {
    "provider": "ai33",
    "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
    "speed": 1.0
  },
  "items": [
    {
      "slot_id": "commentary-slot-0001",
      "audio_path": "audio/commentary-slot-0001.mp3",
      "duration_s": 6.42,
      "provider": "ai33",
      "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
      "speed": 1.0
    }
  ]
}
```

An optional `audio_assets.json` is also **proposed, not implemented** for separated bed paths, separation model identity, cache hash and leakage warnings. Final field names must be locked in `common/schema.py` before coding.

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
- `100%` of reaction items use source audio mode `original` and playback speed `1.0`.
- Reaction audio alignment differs from its selected source by no more than one output video frame; `20 ms` is the preferred target.
- Reaction waveform correlation after render is at least `0.98` on representative unchanged spans.
- No intelligible old Japanese narrator is present in commentary spans during ASR/manual spot checks.
- TTS is not time-stretched and reports speed `1.0`.
- Commentary duration fits its allocated timeline window within `100 ms` or one output frame, whichever is larger.
- Commentary loudness lies within the documented range and each commentary mix peaks at or below `-1.5 dBTP` after encode tolerance. The full program must introduce no new clipping and must not exceed the source program true peak by more than `0.3 dB`.
- No boundary click, unintended silence longer than `250 ms`, or truncated utterance is detected.
- Cache resume produces byte-identical commentary assets for unchanged inputs.
