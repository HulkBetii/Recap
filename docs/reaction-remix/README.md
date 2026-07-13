# Reaction Remix — Design Index

> Status: **Proposed / not implemented**.
>
> These documents describe a planned pipeline that will live alongside the
> existing Recap pipeline. They are planning artifacts, not a statement that
> the CLI, schemas, configuration, or runtime packages already exist.

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
- Support a full-video remix, not only fixed commentary replacement at the
  source timecodes.
- Prefer an output duration 10–20% shorter than the source. The hard floor is
  80% of source duration unless the user explicitly overrides it.
- Keep the established Japanese internet-commentary tone, including expressions
  such as `お前ら`, `ネキ/ニキ`, `ワイ`, `～だぜ`, humor, and irony where suitable.

## Design Documents

- [Product scope](01-product-scope.md) — goals, boundaries, preservation rules,
  and duration policy.
- [Pipeline architecture](02-pipeline-architecture.md) — proposed R0-R8 DAG,
  package boundaries, reuse decisions, and cache invalidation.
- [Data contracts](03-data-contracts.md) — proposed JSON file interfaces,
  examples, validators, and cross-stage invariants.
- [Content analysis](04-content-analysis.md) — proposed multilingual analysis,
  segment classification, reaction integrity, and analysis QA.
- [Editorial planning](05-editorial-planning.md) — reaction ordering, duration
  budgeting, evidence rules, and Japanese commentary planning.
- [Audio and TTS](06-audio-and-tts.md) — source-audio preservation, AI33 policy,
  commentary fitting, stems, and mix constraints.
- [Render and QA](07-render-and-qa.md) — audio-aware timeline assembly, visual
  preservation, measurable gates, and QA artifacts.
- [Implementation roadmap](08-implementation-roadmap.md) — phased delivery,
  reuse boundaries, proposed tests, and exit criteria.
- [POC 09:30–12:30](poc-0930-1230.md) — evidence and lessons from the three-minute
  proof of concept.
- [Proposed config example](examples/config.proposed.yaml) and
  [contract examples](examples/contracts/) — documentation fixtures only; the
  current config loader and runtime do not consume them.

## Decision Priority

When later documents or implementation details conflict, use this order:

1. Explicit user requirements recorded above.
2. The reaction-remix product scope and approved data contracts.
3. Existing Recap project conventions that do not conflict with the new mode.

The current Recap assumptions that source audio is muted and only a voiceover is
muxed do not apply to reaction-remix. Reaction-remix requires an audio-aware
timeline because original reaction audio must be retained.
