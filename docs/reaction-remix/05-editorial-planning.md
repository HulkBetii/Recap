# 05 - Editorial Planning

> Status: **R3-R4 preservation-first POC and full-media editorial hard QA accepted; preferred duration is audit-only**.

## 1. Goal

The editorial planner rebuilds the narrative order of an existing reaction video while preserving the substance of each foreign-language reaction. It may reorder complete reaction units and rewrite Japanese editorial commentary, but it must not turn the source into a short recap or materially alter what a reactor said.

Locked editorial rules:

- Preserve the source branding, mascot, burned-in subtitles and reaction footage.
- Preserve reaction speech, playback speed and meaning.
- Rewrite only Japanese editorial commentary.
- Use the established Japanese internet-commentary voice, including terms such as `お前ら`, `ネキ/ニキ` and `ワイ`, when contextually appropriate.
- Do not add, hide, translate or restyle subtitles.
- Prefer a coherent new story arc over a one-for-one replacement of the original narrator blocks.

## 2. Edit Units

The analyzer classifies source spans into these edit units:

- `reaction`: a complete foreign-language reaction or demonstration whose original audio must be retained.
- `commentary`: Japanese channel narration that may be replaced.
- `transition`: a short bridge, bumper or visual connective segment.
- `branding`: channel intro, mascot or outro material that should normally remain.
- `broll`: source visual material without intelligible reaction speech.
- `mixed`: overlapping reaction and editorial narration; preserve by default.
- `unknown`: a span that cannot be classified safely; preserve by default.

Classification confidence must favor precision over recall. It is better to keep an old commentary fragment for manual QA than to delete part of a genuine reaction.

## 3. Narrative Shape

R3 `reaction_remix.plan` and R4 `reaction_remix.write` normally organize content as:

1. A strong but accurate reaction hook.
2. Brief Japanese context explaining the situation.
3. Reactions grouped by topic rather than strictly by source chronology.
4. Escalation from ordinary observations to the strongest contrast or surprise.
5. A punchline or ironic Japanese observation.
6. A short conclusion that does not repeat every prior point.

This is a guideline, not a requirement to force every video into the same template. The planner must preserve causal context where reordering would otherwise make a reaction confusing.

## 4. Duration Policy

Let `source_duration` be the duration of the original uploaded video and `output_duration` the final remix duration.

- Preferred target: `0.85 <= output_duration / source_duration <= 0.90`.
- Hard permitted range: `0.80 <= output_duration / source_duration <= 1.00`.
- The pipeline must fail validation rather than silently render below the `0.80` hard floor.
- The planner keeps more than 90% when preservation-first constraints leave too
  little replaceable commentary to reach the preferred range.

For the current `18:49.302` source:

- Preferred result: approximately `16:00-16:30`.
- Absolute minimum: approximately `15:03.4`.
- Maximum: the original duration unless a later product decision explicitly permits expansion.

Reduction priority:

1. Replace each isolated narrator core with a concise evidence-bound line.
2. Fit that line to the available commentary visual without time-stretching.
3. Preserve every non-commentary block even when the output remains above the
   preferred range.

## 5. Reordering Rules

- Reorder complete reaction units, not arbitrary shot fragments.
- Keep reaction playback at `1.0x`.
- Do not cut across a spoken sentence, laugh, gesture payoff or subtitle exchange.
- Keep a small configurable handle around reaction boundaries when needed for natural breathing and room tone.
- Every selected source unit appears exactly once; source-block reuse is
  forbidden in v1.
- Do not move a payoff before the context required to understand it.
- Keep every `branding` block as a source placement.
- Keep every `mixed` and `unknown` unit as a source block. V1 never promotes,
  excludes, or uses one as a commentary visual, even when narrator speech is
  audible inside it.
- Exclude or use as a replacement visual only a block whose analyzed
  `kind=commentary`.

## 6. Planner Contract

`remix_plan.json` is implemented and validated by `common/schema.py`. The canonical shape is defined in [03-data-contracts.md](03-data-contracts.md).

Illustrative shape only:

```json
{
  "schema_version": "reaction-remix.v1",
  "items": [
    {
      "item_id": "item-0000",
      "kind": "source_block",
      "block_id": "block-0001",
      "role": "hook"
    },
    {
      "item_id": "item-0001",
      "kind": "commentary_slot",
      "slot_id": "commentary-slot-0001",
      "role": "setup",
      "evidence_block_ids": ["block-0001"]
    }
  ]
}
```

The model should use a discriminated union so reaction and commentary items cannot accidentally accept each other's fields. LLM output must reference source segment IDs only; code derives timecodes from the analyzed source map.

## 7. LLM and Reuse Boundary

The R3 planner and R4 writer reuse the existing ChatGPT Playwright transport, persistent profile, session recovery and JSON extraction patterns. Existing Vietnamese recap prompts, `ReviewBeat`, story-map logic and recap QA rules must not be reused as reaction-remix semantics.

The implemented prompt identities are `reaction-plan-v8` and
`reaction-write-v2`. Session policy `auto` resumes only when the core input
hash is unchanged; changed blocks/config start a new ChatGPT conversation and
cannot reuse a fit response from an earlier POC.

For long responses, recovery searches the current conversation for the same
normalized user prompt and reuses its following assistant response. ChatGPT UI
chrome such as a trailing `Show more` label is ignored, so process restarts do
not resend prompts whose responses arrived after the original timeout.

The planner must:

- Use Playwright as the primary text path.
- Keep one conversation per source/run.
- Validate every returned segment ID locally.
- Reject invented reactions or unsupported claims.
- Regenerate only invalid commentary items when possible.
- Record why a reaction was reordered; reject any non-commentary exclusion.

## 8. Acceptance Gates

Planning passes only when all of the following are true:

- `100%` of referenced segment IDs exist in the analyzed source map.
- `0` reaction items contain a partial utterance according to source boundaries.
- `0` reaction items request a speed other than `1.0`.
- `0` unmarked duplicate reaction units exist.
- No reaction, mixed, unknown, branding, transition, or b-roll block is
  excluded.
- Japanese commentary contains no unsupported factual claim when compared with the referenced reactions.
- Estimated duration is within `80-100%` of source duration and preferably within `85-90%`.
- The plan contains no instruction to blur, mask, replace or generate subtitles.
- A deterministic validator passes without relying on LLM self-approval.

The accepted preservation-first POC plan contains two one-for-one commentary
slots for `block-0003` and `block-0026`, predicts `176.08766425s`
(`0.9559506` of source), and retains `1.0` of unique reaction speech. The ratio
is above the preferred range because the protected timeline already exceeds
the target; this is a warning, not permission to remove `mixed`/`unknown`.
