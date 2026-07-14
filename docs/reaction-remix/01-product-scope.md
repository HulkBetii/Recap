# Reaction Remix — Product Scope

> Status: **MVP implemented; preservation-first POC and full authorized-video hard QA accepted; preferred duration is audit-only**.

## 1. Product Goal

Given one existing reaction video, produce a newly edited video that tells the
same broad subject through a stronger editorial structure. The pipeline may
reorder complete source blocks and write new Japanese commentary around them.
The preservation-first MVP replaces only isolated narrator commentary; it does
not remove non-commentary blocks merely to force a duration target.

The result is a content remix rather than a 1:1 narrator replacement. It must
still preserve the authenticity of each participant's reaction and the visual
identity already burned into the source video.

## 2. Input and Output

### Input

- One source video containing reaction clips, Japanese editorial commentary,
  channel branding, and burned-in subtitles.
- The source may contain several spoken languages, including Japanese, English,
  German, Spanish, or other languages.

### Output

- One edited video using the source reaction and other non-commentary blocks.
- New Japanese editorial commentary synthesized through AI33 with voice ID
  `elevenlabs_QPtBgsg1dxKTQHNpHrHt`.
- Original reaction audio wherever a reaction clip is retained.
- Existing branding, mascot, graphics, and burned-in subtitles unchanged.

## 3. In Scope

- Analyze the full source and classify reaction, editorial commentary,
  transition, branding, and ambiguous intervals.
- Replace only narrator cores classified as `commentary`; retain every
  non-commentary block, with `reaction`, `mixed`, and `unknown` video/audio
  explicitly protected.
- Understand the meaning and emotional role of reactions across languages.
- Build a new editorial arc for the complete video.
- Reorder reaction clips when the new order is coherent and does not alter the
  meaning of an individual reaction.
- Shorten or rewrite isolated commentary when it improves pacing; report when
  preservation prevents the preferred duration reduction.
- Rewrite all Japanese editorial commentary needed by the new structure.
- Preserve useful original music or ambience when it belongs to the channel's
  presentation and does not obscure speech.
- Automatically choose commentary placement, audio transitions, gain, and
  pacing within the approved constraints.
- Generate deterministic QA artifacts that explain what was retained, removed,
  reordered, or replaced.

## 4. Out of Scope

- Rewriting, dubbing, translating, or voice-cloning reaction participants.
- Changing the playback speed of reaction clips.
- Masking, blurring, cropping away, or replacing burned-in subtitles.
- Adding new subtitles or captions.
- Generating new reaction footage or inserting unrelated stock footage.
- Changing the channel mascot, logo, branding, or visual layout.
- Publishing, thumbnail generation, SEO, or channel upload automation.
- Treating the current movie-recap EDL and render behavior as sufficient for
  reaction-remix; the current renderer deliberately discards source audio.

## 5. Preservation Contract

For every retained reaction interval:

- Video and original reaction audio remain synchronized.
- Playback speed remains `1.0`.
- No words are removed from the middle of a participant's sentence.
- No edit changes the apparent answer, target, cause, or emotional meaning of
  the reaction.
- Burned-in subtitles remain exactly as present in the source frames.
- Branding and overlays within those frames remain untouched.
- No fade, ducking, limiter, crossfade, Demucs, normalization, or other audio
  processing is applied to the protected source placement.

Trimming is allowed only at safe semantic boundaries, normally before a speaker
starts or after the speaker finishes. A short handle may be retained when it is
needed to avoid clipped breaths, consonants, laughter, or room tone.

R2 uses `commentary_boundary_policy: strict_or_word_edge`. A Japanese narrator
core may become replaceable commentary when both boundaries either have the
full configured `120 ms` handle or sit outside every word timestamp with no
content overlap. A word edge eligible to bound commentary is promoted to
confidence exactly `0.90`; a lower-confidence edge cannot make commentary
replaceable. Any word, speaker, or language overlap remains protected as
`mixed` or `unknown`, even
when that means the old narrator remains audible in the final output.

## 6. Editorial Freedom

Reaction clips may be reordered independently of the original timeline. The new
order should prioritize:

1. A clear hook using a strong but representative reaction.
2. Enough setup for the viewer to understand the topic.
3. Escalating reactions, contrast, or surprise.
4. A memorable climax or reversal.
5. A concise punchline or closing observation.

Reordering must not fabricate a dialogue between people who did not respond to
one another. Japanese commentary must clearly bridge topic, location, speaker,
or time changes when the visual order is no longer chronological.

## 7. Japanese Commentary Policy

- Commentary is newly written for the remixed order rather than mechanically
  paraphrased line by line.
- Style is informal Japanese internet commentary with humor and irony.
- Expressions such as `お前ら`, `ネキ/ニキ`, `ワイ`, and `～だぜ` are permitted
  when natural, but should not be forced into every sentence.
- Commentary must not invent facts, nationality, intent, or quotes unsupported
  by the retained reaction.
- Each line should be TTS-friendly and sized for its allocated editorial gap.
- The participants' own speech is never replaced by Japanese TTS.

## 8. Duration Policy

Let `source_duration` be the duration of the complete input video and
`output_duration` be the final rendered duration.

Preferred target:

```text
0.85 * source_duration <= output_duration <= 0.90 * source_duration
```

Hard permitted range:

```text
0.80 * source_duration <= output_duration <= 1.00 * source_duration
```

The normal target is approximately 85–90% of the source.
The editor should keep more material when cutting to the target would remove a
distinct, useful reaction.

The 80% lower bound is a hard floor. Automatic planning must fail QA rather than
render a shorter result, unless an explicit future override is supplied by the
user. Keeping more than 90% is acceptable when the source has little repetition,
but should produce a QA note explaining why the preferred reduction was not met.

For the current 18:49.302 reference video:

- 90% is approximately `16:56.4`.
- 85% is approximately `15:59.9`.
- 80% is approximately `15:03.4` and is the hard minimum.

## 9. Success Criteria

A full-video result is acceptable when:

- The output respects the duration floor and reports its reduction ratio.
- Retained reactions have intact picture, original audio, speed, subtitles, and
  semantic meaning.
- Original Japanese editorial narration is absent from replacement intervals.
- New Japanese commentary follows the remixed story and is intelligible.
- Reordered transitions are understandable without false conversational links.
- There are no clipped phrases, A/V drift, boundary clicks, accidental silence,
  decode failures, or unexplained duplicated reactions.
- No subtitle mask or new subtitle layer is present.

The authorized `09:30-12:30` POC passed the preservation-first gate with:

- source duration `184.201633s`, output duration `176.609767s`, and output
  ratio `0.958785`;
- replaceable commentary cores `block-0003` and `block-0026`;
- protected narrator overlap in `block-0038` and `block-0039`;
- unique reaction speech retention `1.0`;
- minimum reaction audio correlation `0.9986287014`, maximum lag `0 ms`,
  maximum gain delta `0.0691078578 dB`, and minimum frame similarity
  `0.9950234368`;
- old narrator leakage count `0` for replaced commentary placements and full
  output peak increase `0.1 dB`.

The POC ratio is above the preferred range because preservation takes priority
over forcing a reduction. It remains inside the hard range and is reported as
an audit warning rather than a failure.

## 10. Compatibility Boundary

Reaction-remix is implemented as the separate `reaction_remix/` package and
`run_reaction.py` entrypoint. It reuses shared media, Playwright, TTS, cache, and
validation utilities only where their behavior matches this scope. It does not
change existing recap output contracts or make the recap renderer retain source
audio.
