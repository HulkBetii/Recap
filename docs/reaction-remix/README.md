# Reaction Remix — Runtime Index

> Status: **MVP implemented; preservation-first POC and full authorized-video
> hard QA accepted; preferred duration is audit-only**.
>
> Runtime code lives under `reaction_remix/` and is invoked through
> `python run_reaction.py`. The existing Recap pipeline remains separate.

## Purpose

`reaction-remix` turns an existing multilingual reaction video into a newly
edited version. It may reorder the original reaction clips and rewrite the
Japanese editorial commentary, while preserving the actual reactions and the
channel's existing visual presentation.

This is a separate operating mode from the current movie-recap pipeline. The
existing recap contracts and behavior must remain backward-compatible.

## Locked Requirements

- Use AI33 for Japanese TTS with voice ID
  `elevenlabs_QPtBgsg1dxKTQHNpHrHt`.
- Preserve each retained reaction clip's picture, original audio, playback
  speed, and burned-in subtitles.
- Do not mask existing subtitles and do not add replacement subtitles.
- Reordering reaction clips is allowed when it improves the new editorial arc.
- Rewrite and replace only Japanese editorial commentary; do not rewrite or
  synthesize the participants' reactions.
- A Japanese narrator core may use a safe `word_edge` boundary when no word is
  cut and the single-speaker/language confidence gates pass. Narrator audio that
  overlaps reaction speech remains protected as `mixed/unknown`.
- Support a full-video remix, not only fixed commentary replacement at the
  source timecodes.
- Prefer an output duration 10–20% shorter than the source. The hard floor is
  80% of source duration unless the user explicitly overrides it.
- Keep the established Japanese internet-commentary tone, including expressions
  such as `お前ら`, `ネキ/ニキ`, `ワイ`, `～だぜ`, humor, and irony where suitable.

## Current Acceptance

The source-compatible `09:30–12:30` POC passed R0-R8 under the preservation-first
policy. Its `184.201633s` source timeline produced a decodable `176.609767s`
output with unique reaction speech retention `1.0`. Across 35 protected
placements, minimum audio correlation was `0.9986287014`, maximum lag was `0ms`,
maximum gain delta was `0.0691078578dB`, and minimum frame similarity was
`0.9950234368`.

Two narrator cores were replaced through AI33 and both reached Japanese ASR
similarity `1.0`; old-narrator leakage in replaced commentary was zero. The
overlapping narrator tail remains intentionally protected in `block-0038` and
`block-0039`.

The full authorized `1129.302494s` source has also passed R0-R8 hard QA in
`work/reaction-remix-full-r02/`. Its output is `1010.379002s` (`0.894693x`) with
160 protected placements checked, no failed placement IDs, minimum audio
correlation `0.9971666`, maximum lag `0ms`, maximum gain delta `0.0931131dB`,
minimum frame similarity `0.9954148`, zero commentary leakage, zero forbidden
visual operations, and protected-overlap warnings for `block-0093`,
`block-0127`, and `block-0186`. After manual listening QA, `block-0045` was explicitly dropped because
old commentary remained audible in that hard segment. The output is shorter than
the source and inside the preferred `0.85-0.90` ratio; exact target length remains
audit-only rather than a release blocker.

## Design Documents

- [Product scope](01-product-scope.md) — goals, boundaries, preservation rules,
  and duration policy.
- [Pipeline architecture](02-pipeline-architecture.md) — implemented R0-R8 DAG,
  package boundaries, reuse decisions, and cache invalidation.
- [Data contracts](03-data-contracts.md) — `reaction-remix.v1` JSON file interfaces,
  examples, validators, and cross-stage invariants.
- [Content analysis](04-content-analysis.md) — multilingual analysis,
  segment classification, reaction integrity, and analysis QA.
- [Editorial planning](05-editorial-planning.md) — reaction ordering, duration
  budgeting, evidence rules, and Japanese commentary planning.
- [Audio and TTS](06-audio-and-tts.md) — source-audio preservation, AI33 policy,
  commentary fitting, stems, and mix constraints.
- [Render and QA](07-render-and-qa.md) — audio-aware timeline assembly, visual
  preservation, measurable gates, and QA artifacts.
- [Implementation roadmap](08-implementation-roadmap.md) — delivery status,
  reuse boundaries, automated tests, and exit criteria.
- [POC 09:30–12:30](poc-0930-1230.md) — evidence and lessons from the three-minute
  proof of concept.
- Production config: [`config.reaction-remix.yaml`](../../config.reaction-remix.yaml).
  Contract fixtures remain under [examples/contracts](examples/contracts/) for documentation and tests.

## Decision Priority

When later documents or implementation details conflict, use this order:

1. Explicit user requirements recorded above.
2. The reaction-remix product scope and approved data contracts.
3. Existing Recap project conventions that do not conflict with the new mode.

The current Recap assumptions that source audio is muted and only a voiceover is
muxed do not apply to reaction-remix. Reaction-remix requires an audio-aware
timeline because original reaction audio must be retained.
