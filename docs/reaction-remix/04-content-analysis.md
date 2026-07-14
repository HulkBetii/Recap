# Reaction Remix — Content Analysis

> Status: **R0-R2 preservation-first POC and full-video audit passed**.

## 1. Purpose

The analysis stage converts one mixed reaction video into a reliable map of
editorial commentary, participant reactions, transitions, branding, and
ambiguous material. This map is the evidence used by later editorial planning;
it must describe the source without deciding the final order itself.

The primary risk is not imperfect translation. The primary risk is classifying
participant speech as replaceable commentary or cutting a reaction in a way
that changes its meaning.

## 2. Inputs

- Source video path and probed media metadata.
- Timecoded multilingual transcript with speaker/language hints where available.
- Audio activity, pause, music, and speaker-change observations.
- Shot boundaries and representative frames.
- Optional manually supplied corrections for names, languages, or segment type.

ASR and translation are analysis aids. Original audio remains the authoritative
source for boundaries and retained reaction content.

## 3. Segment Taxonomy

Every source interval should receive one primary type:

### `reaction`

A participant's original spoken or nonverbal response. This includes speech,
laughter, sighs, surprise, and meaningful pauses belonging to the reaction.
Reaction intervals are protected media and retain original audio.

### `commentary`

Japanese narration written or spoken by the channel to introduce, explain,
connect, or joke about reactions. These intervals may be removed and replaced
by newly written Japanese commentary.

### `transition`

A bumper, interstitial, title card, musical bridge, or visual/audio transition
between content blocks. Preservation-first v1 may reorder it but retains it as
a source placement.

### `branding`

Channel logo, mascot, recurring layout, subscription message, or other channel
identity element. Preservation-first v1 retains branding as a source placement.

### `broll`

Source visual material without intelligible participant speech.
Preservation-first v1 retains it as its own source placement rather than
borrowing it for replacement commentary.

### `mixed`

Editorial narration and reaction audio overlap, or the source cannot be safely
split without damaging the reaction. A mixed interval is protected by default
and must not be automatically treated as replaceable commentary.

### `unknown`

Evidence is insufficient or contradictory. Unknown intervals are kept by
default and surfaced in QA rather than guessed away.

## 4. Language Handling

- Detect language per utterance, not once for the entire file.
- Primary ASR regions are capped at 30 seconds and prefer silence midpoints; a
  full-timeline 6-second/2-second-overlap refinement pass is authoritative for
  language switches, while the primary transcript fills refinement gaps.
- Japanese language alone does not imply editorial commentary; a participant may
  speak Japanese.
- English, German, Spanish, and other languages may all be valid reactions.
- Preserve the original transcript and store any working translation separately.
- Translation should retain uncertainty around names, slang, jokes, and cultural
  references instead of converting a guess into a fact.
- The editorial planner may use a normalized meaning summary, but safe clip
  boundaries must continue to reference original timecodes and audio.

## 5. Speaker and Role Classification

Classification should combine several signals:

- Speaker continuity and voice characteristics.
- Spoken language and recurring narrator phrases.
- Presence of a participant on screen.
- Existing subtitle layout and visual template.
- Music/bed behavior around the interval.
- Pause and transition patterns before and after the interval.
- Semantic role: setup, participant answer, editorial joke, or bridge.

No single signal is sufficient. In particular, voice activity detection cannot
distinguish narration from reaction, and OCR cannot be used as permission to
remove burned-in text.

Each segment should expose a confidence score and evidence summary. Low
confidence between `reaction` and `commentary` must resolve to
`mixed` or `unknown`, never automatically to replaceable commentary.

## 6. Reaction Boundary Rules

For a candidate reaction clip, analysis should identify:

- Safe start before the first meaningful word or nonverbal response.
- Safe end after the final word, laughter, or reaction tail.
- Topic/setup needed to understand the response.
- Whether the participant refers to an earlier prompt or visual.
- Whether a subtitle line begins before or ends after the selected trim.
- Whether music or ambience is continuous across the boundary.

The content range and the safe media handles should be distinct. Later editing
may cut within the handles but must include the complete content range.

The implemented boundary policy is `strict_or_word_edge`. A cut point records
its measured left/right handles and one safety mode:

- `full_handle`: at least `120 ms` clear on both sides;
- `word_edge`: a shorter clear edge outside every word timestamp, with no
  adjacent content overlap and a single high-confidence Japanese narrator on
  one side;
- `overlap`: word or speaker content intersects the boundary, so confidence is
  capped at `0.89` and the span is protected as `mixed`/`unknown`;
- `source_boundary`: immutable source start/end.

`word_edge` does not weaken reaction protection: it can promote only an
isolated narrator core, and only when both block boundaries are either
`full_handle` or `word_edge`. It never promotes an already `mixed` or `unknown`
block.

Splitting a participant sentence, accelerating it, muting it, or replacing it
with TTS is forbidden.

## 7. Reaction Unit Metadata

The analysis artifact should give later stages enough information to
reorder responsibly. A reaction unit should conceptually include:

- Stable unit and source segment IDs.
- Source start/end and safe-handle timecodes in seconds.
- Original language and optional working translation.
- Speaker label when known.
- Topic and factual claims.
- Emotional role and intensity.
- Setup dependencies.
- Whether it can stand alone or needs an editorial bridge.
- Duplicate/near-duplicate group.
- Protected audio and speed policy.
- Classification confidence and warnings.

Exact JSON field names and Pydantic schemas are locked by
`reaction-remix.v1` in `common/schema.py`.

## 8. Full-Video Coverage

Analysis must cover the complete source timeline, including intro, transitions,
outro, and commentary intervals later eligible for replacement. Source intervals should be
non-overlapping at the primary classification layer and should not leave silent
unexplained gaps.

Coverage QA should report:

- Percentage of source duration classified.
- Duration by primary segment type.
- Count and duration of `mixed` and `unknown` intervals.
- Suspected narrator/reaction overlaps.
- Reaction units with incomplete transcript or unsafe boundaries.
- Duplicate reaction groups and low-value repeated setup.

`reaction_blocks.review.html` exposes each cut's safety mode, measured left and
right handles, confidence, and downgrade reason beside transcript, speaker,
language, and thumbnails. This is the required local audit surface for deciding
whether a narrator core is replaceable or protected.

## 9. Planning Handoff

The analysis stage provides evidence; the editorial planning stage decides:

- How to order the complete retained reaction and non-commentary units safely.
- What Japanese bridge or commentary is required.
- Whether shorter replacement commentary can approach the preferred duration
  without removing protected/non-commentary material.

Analysis must not pre-trim reactions merely to hit the duration target. Duration
optimization belongs to planning, where value and dependencies can be evaluated
across the full video.

## 10. Analysis Acceptance Criteria

- Every source interval is classified or explicitly marked unknown.
- Reaction and mixed intervals are protected by default.
- No decision relies only on detected language, OCR, or transcript text.
- Reaction boundaries do not cut speech or meaningful nonverbal responses.
- Original source timecodes remain in seconds and within media duration.
- Multilingual meaning summaries do not overwrite original transcript text.
- Ambiguity and overlaps are visible in machine-readable QA.
- The analysis artifact is deterministic for unchanged source/config inputs and
  supports cache/resume under the project's existing conventions.
- The POC must classify the two isolated narrator cores as replaceable
  `commentary` while preserving the narrator/reaction tail as audited
  `mixed`/`unknown`.
- The accepted `reaction-segment-v7` POC covers all `184.201633s` in `39`
  blocks with no gap, overlap, or sub-`80 ms` block. `block-0003` and
  `block-0026` are replaceable commentary. `block-0038` and `block-0039` are
  protected narrator overlap and remain source media.
- The earlier full-source result of 12 commentary segments predates the strict
  boundary and partial-refinement fixes. Its artifacts are stale and cannot be
  used as acceptance evidence until the full R0-R2 audit is rerun.
