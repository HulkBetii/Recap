# Reaction Remix Proposed Data Contracts

Status: Proposed design only. These schemas, validators, filenames, and model
names are not implemented.

## 1. Contract Rules

If implemented, Reaction Remix stages communicate only through JSON files and
media artifacts. They do not pass stage-owned Python objects across package
boundaries.

All proposed file-interface models follow these rules:

- Python 3.11 or newer and Pydantic with extra fields forbidden;
- time values are float seconds from the beginning of the source or output;
- source time fields use src_ or tc_ prefixes;
- output timeline fields use tl_ prefixes;
- paths are normalized to forward slashes in JSON;
- IDs are stable inside one source identity and are never silently renumbered;
- lists with timeline meaning are sorted and validated;
- source and config hashes use SHA-256;
- an LLM may select IDs, but code derives all media timecodes;
- every top-level file has schema_version and created_at;
- every stage writes a sidecar meta or manifest containing cache identity,
  algorithm version, warnings, and output hashes.

The proposed models should be added to common/schema.py only when
implementation begins. Existing Recap models remain unchanged.

## 2. Why Existing Contracts Stay Unchanged

FilmMapSegment describes Korean/Vietnamese recap speech and visual gaps.
ReviewBeat describes new narration tied to a source story span. BeatTiming
describes a continuous voiceover. EdlPlacement describes video placements that
are later muxed with that voiceover.

Reaction Remix needs different semantics:

- multilingual source turns;
- atomic reaction blocks that retain source audio;
- block reordering and dependency constraints;
- per-placement audio modes;
- individual Japanese commentary files between source reaction clips;
- an explicit no-visual-edit policy.

Extending existing models with optional reaction fields would make validation
ambiguous and could allow the current renderer to consume an incompatible EDL.
Separate filenames make an accidental cross-pipeline handoff fail early.

## 3. reaction_source.json

Purpose: immutable media identity and source-format contract from R0.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "input_path": "C:/media/source.mp4",
  "input_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "duration_s": 1129.302,
  "video": {
    "stream_index": 0,
    "codec": "h264",
    "width": 1920,
    "height": 1080,
    "fps_num": 30000,
    "fps_den": 1001,
    "pixel_format": "yuv420p"
  },
  "audio": {
    "stream_index": 1,
    "codec": "aac",
    "sample_rate": 44100,
    "channels": 2,
    "channel_layout": "stereo"
  },
  "subtitle_streams": [],
  "has_burned_in_subtitles": true,
  "subtitle_policy": "burned_in_preserve",
  "created_at": "2026-07-13T12:00:00Z",
  "config_hash": "sha256:5b21e166af70f7b3392e11b02698a8de1d885658b0f4b832af04d90ee8c1d031",
  "warnings": []
}
~~~

Invariants:

- duration_s, dimensions, sample rate, channel count, and FPS terms are
  positive;
- fps_num divided by fps_den is the authoritative frame rate;
- input_hash identifies path, size, modification time, and preferably content;
- exactly one primary video and audio stream are selected in proposed v1;
- burned_in_preserve means pixels must pass through without subtitle masking or
  replacement;
- proposed v1 supports burned-in subtitles. Soft subtitle retiming is outside
  scope and must produce a warning or fail according to config.

## 4. reaction_transcript.json

Purpose: multilingual, timecoded source speech turns from R1. This is not a
FilmMapSegment replacement and is not accepted by the Recap review stage.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "source_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "turns": [
    {
      "turn_id": 0,
      "tc_start": 570.12,
      "tc_end": 578.96,
      "text": "I did not realize how much I would miss it.",
      "language": "en",
      "language_confidence": 0.99,
      "speaker_id": "speaker_reaction_03",
      "speaker_confidence": 0.91,
      "asr_confidence": 0.94,
      "word_timestamps_ref": "analysis/words/turn-000000.json",
      "warnings": []
    },
    {
      "turn_id": 1,
      "tc_start": 579.5,
      "tc_end": 588.94,
      "text": "日本の快適さに慣れた彼女が帰国して最初に驚いたのは、会計のたびに必要なチップでした。",
      "language": "ja",
      "language_confidence": 0.99,
      "speaker_id": "speaker_narrator",
      "speaker_confidence": 0.97,
      "asr_confidence": 0.92,
      "word_timestamps_ref": "analysis/words/turn-000001.json",
      "warnings": []
    }
  ],
  "asr": {
    "provider": "faster-whisper",
    "model": "large-v3",
    "device": "cuda",
    "chunk_s": 30.0,
    "language_mode": "auto"
  },
  "created_at": "2026-07-13T12:10:00Z",
  "warnings": []
}
~~~

Invariants:

- turn_id values are unique and stable;
- tc_end is greater than tc_start and no turn exceeds source duration;
- language is a normalized BCP-47 code or und when confidence is insufficient;
- text is the original-language transcript, not a summary or translation;
- word timestamps are optional sidecars and must stay inside the owning turn;
- overlap is permitted only when explicitly represented by an overlap_group in
  a future schema revision. Proposed v1 produces non-overlapping primary turns.

## 5. reaction_blocks.json

Purpose: atomic editorial units, safe cut points, semantic labels, and
preservation policy from R2.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "source_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "transcript_hash": "sha256:30dc132170fdf50cf07e9ab45d54cc6a233f73e2e32d301d5280fdfadacdb51b",
  "cut_points": [
    {
      "cut_point_id": "cut-0041",
      "tc": 570.0,
      "kind": "silence_scene_boundary",
      "confidence": 0.97,
      "speech_padding_s": 0.12
    },
    {
      "cut_point_id": "cut-0042",
      "tc": 578.96,
      "kind": "turn_boundary",
      "confidence": 0.95,
      "speech_padding_s": 0.12
    },
    {
      "cut_point_id": "cut-0043",
      "tc": 579.5,
      "kind": "turn_boundary",
      "confidence": 0.96,
      "speech_padding_s": 0.12
    },
    {
      "cut_point_id": "cut-0044",
      "tc": 588.94,
      "kind": "silence_scene_boundary",
      "confidence": 0.98,
      "speech_padding_s": 0.12
    }
  ],
  "blocks": [
    {
      "block_id": "reaction-0017",
      "kind": "reaction",
      "tc_start": 570.0,
      "tc_end": 578.96,
      "start_cut_point_id": "cut-0041",
      "end_cut_point_id": "cut-0042",
      "turn_ids": [0],
      "language_codes": ["en"],
      "speaker_ids": ["speaker_reaction_03"],
      "sequence_group_id": "trip-return-03",
      "sequence_index": 0,
      "semantic": {
        "summary_ja": "帰国後、日本の快適さが恋しくなったという反応",
        "country": "United States",
        "topic": "reverse culture shock",
        "sentiment": "surprised",
        "intensity": 0.73,
        "novelty": 0.82
      },
      "preservation": {
        "video": "source_frames",
        "audio": "source_mix",
        "speed": 1.0,
        "allow_trim_to_safe_cut_points": true
      },
      "eligible_commentary_visual": false,
      "classification_confidence": 0.98,
      "warnings": []
    },
    {
      "block_id": "commentary-0008",
      "kind": "commentary",
      "tc_start": 579.5,
      "tc_end": 588.94,
      "start_cut_point_id": "cut-0043",
      "end_cut_point_id": "cut-0044",
      "turn_ids": [1],
      "language_codes": ["ja"],
      "speaker_ids": ["speaker_narrator"],
      "sequence_group_id": null,
      "sequence_index": null,
      "semantic": {
        "summary_ja": "チップ文化への導入コメント",
        "country": "United States",
        "topic": "tipping",
        "sentiment": "ironic",
        "intensity": 0.48,
        "novelty": 0.41
      },
      "preservation": {
        "video": "source_frames",
        "audio": "replace_commentary",
        "speed": 1.0,
        "allow_trim_to_safe_cut_points": true
      },
      "eligible_commentary_visual": true,
      "classification_confidence": 0.99,
      "warnings": []
    }
  ],
  "created_at": "2026-07-13T12:20:00Z",
  "warnings": []
}
~~~

Proposed kind values:

- reaction: source A/V must be preserved;
- commentary: source video may be reused, source mixed audio must be replaced;
- transition: channel transition or connective material;
- branding: intro/outro/mascot/channel identity;
- broll: visual material without intelligible reaction speech;
- mixed: overlapping reaction and commentary; conservative preserve-only block;
- unknown: conservative preserve-only block.

Invariants:

- cut_point_id and block_id values are unique;
- cut points are sorted and inside source duration;
- block tc_start and tc_end are derived from referenced cut points;
- block turn_ids exist and lie inside the block span;
- sequence_index is required when sequence_group_id is set;
- reaction, mixed, and unknown blocks require audio=source_mix and speed=1.0;
- commentary blocks require audio=replace_commentary;
- a planner may select only declared cut-point IDs, never arbitrary timecodes;
- blocks may touch but may not overlap unless a later explicit overlap model is
  introduced.

## 6. remix_plan.json

Purpose: editorial order, safe trims, commentary slots, duration budget, and
retention metrics from R3.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "source_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "blocks_hash": "sha256:6e678af0cebf4b048bc870ab48dbc5e51841df0aab1e375fa78ecc887909ef75",
  "original_duration_s": 1129.302,
  "duration_policy": {
    "hard_min_output_ratio": 0.8,
    "preferred_min_output_ratio": 0.85,
    "preferred_max_output_ratio": 0.9,
    "hard_max_output_ratio": 1.0,
    "target_duration_s": 975.0
  },
  "items": [
    {
      "item_id": "item-0000",
      "order": 0,
      "kind": "source_block",
      "role": "hook",
      "block_id": "reaction-0017",
      "slot_id": null,
      "start_cut_point_id": "cut-0041",
      "end_cut_point_id": "cut-0042",
      "evidence_block_ids": [],
      "preferred_visual_block_ids": [],
      "target_duration_s": null,
      "max_duration_s": null,
      "char_budget": null,
      "dependency_group_id": "trip-return-03",
      "reason": "Strong concise reverse-culture-shock reaction."
    },
    {
      "item_id": "item-0001",
      "order": 1,
      "kind": "commentary_slot",
      "role": "setup",
      "block_id": null,
      "slot_id": "commentary-slot-0001",
      "start_cut_point_id": null,
      "end_cut_point_id": null,
      "evidence_block_ids": ["reaction-0017"],
      "preferred_visual_block_ids": ["commentary-0008"],
      "target_duration_s": 9.44,
      "max_duration_s": 10.2,
      "char_budget": 62,
      "dependency_group_id": null,
      "reason": "Introduce the tipping theme before the next reaction."
    }
  ],
  "predicted_duration_s": 975.0,
  "predicted_output_ratio": 0.8634,
  "retention": {
    "unique_reaction_speech_ratio": 0.94,
    "reaction_block_ratio": 0.91,
    "country_coverage_ratio": 1.0,
    "topic_coverage_ratio": 0.96
  },
  "llm": {
    "backend": "chatgpt_playwright",
    "session_url": "https://chatgpt.com/c/example",
    "attempts": 1
  },
  "created_at": "2026-07-13T12:30:00Z",
  "warnings": []
}
~~~

Invariants:

- item_id and order values are unique; order is contiguous from zero;
- source_block items reference one valid block and valid safe cut points;
- commentary_slot items reference one unique slot_id and at least one evidence
  block;
- source item timecodes are derived from cut points after loading the block
  catalog;
- sequence dependencies form an acyclic graph and preserve internal order;
- predicted_duration_s equals source spans plus commentary target durations and
  configured transition padding;
- output ratio cannot be below hard_min_output_ratio or above
  hard_max_output_ratio;
- preferred-range misses are warnings, not permission to violate the hard
  floor;
- unique reaction speech retention must meet the configured minimum.

## 7. commentary_script.json

Purpose: new Japanese editorial text from R4. Reaction speech never appears as
replacement narration in this file.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "source_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "plan_hash": "sha256:5f4d96ac0799a5736fc0ba3b31eb66aaa29a49164889600ed625c93e00fc564c",
  "language": "ja",
  "style_id": "reaction-internet-ja-v1",
  "slots": [
    {
      "slot_id": "commentary-slot-0001",
      "before_item_id": "item-0000",
      "after_item_id": null,
      "role": "setup",
      "text_ja": "日本の快適さに慣れたネキが帰国して最初に食らうのは、会計のたびに襲うチップ地獄だ。まずはその悲鳴を聞いてみよう。",
      "evidence_block_ids": ["reaction-0017"],
      "target_duration_s": 9.44,
      "max_duration_s": 10.2,
      "char_budget": 62,
      "tone_tags": ["お前ら", "ネキ", "humorous", "ironic"],
      "qa": {
        "language_ok": true,
        "evidence_ok": true,
        "style_ok": true,
        "length_ok": true
      },
      "warnings": []
    }
  ],
  "llm": {
    "backend": "chatgpt_playwright",
    "session_url": "https://chatgpt.com/c/example",
    "attempts": 1
  },
  "created_at": "2026-07-13T12:40:00Z",
  "warnings": []
}
~~~

Invariants:

- language is ja in the proposed production preset;
- every plan commentary slot appears exactly once;
- non-null before_item_id and after_item_id values agree with plan order;
- evidence_block_ids are a non-empty subset of the slot evidence in the plan;
- text_ja is non-empty and passes deterministic Japanese/script-length checks;
- the script may analyze a reaction but may not replace or dub the reaction;
- an evidence or style QA failure requires slot regeneration before TTS.

## 8. commentary_audio.json

Purpose: actual per-slot TTS artifacts and measured audio properties from R5.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "source_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "script_hash": "sha256:89aeb13c0dd90ad83989e39d79502b7402e5be00039e0af95a56f1de3ec246ba",
  "voice_policy": {
    "provider": "ai33",
    "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
    "model": "eleven_multilingual_v2",
    "speed": 1.0,
    "fallback_provider": null,
    "text_normalization": "basic"
  },
  "items": [
    {
      "slot_id": "commentary-slot-0001",
      "audio_path": "audio/commentary-slot-0001.mp3",
      "duration_s": 9.44,
      "provider": "ai33",
      "voice_id": "elevenlabs_QPtBgsg1dxKTQHNpHrHt",
      "model": "eleven_multilingual_v2",
      "speed": 1.0,
      "text_hash": "sha256:3e080b6eb0482948222281173fdfac79c113e851b752bb4d3e69b6fa74c49f46",
      "cache_key": "sha256:07d4c3d28b309317223748920ea7706eaa7fda4cd1becbb772f1011578b97f2c",
      "normalized": true,
      "lufs_i": -13.5,
      "true_peak_dbfs": -1.7,
      "asr_text_match": 0.96,
      "warnings": []
    }
  ],
  "total_commentary_duration_s": 9.44,
  "created_at": "2026-07-13T12:50:00Z",
  "warnings": []
}
~~~

Invariants:

- every script slot has exactly one audio item;
- production provider, voice ID, model, and speed match voice_policy;
- fallback_provider is null unless a later explicit policy authorizes fallback;
- duration is measured from the final normalized media, not estimated from text;
- the audio file exists and its content hash is recorded in the stage manifest;
- a duration above the slot maximum returns a rewrite request instead of
  time-stretching reaction media;
- manifest updates are atomic after every completed item.

## 9. remix_edl.json

Purpose: mixed A/V placement contract from R6. This file is intentionally not
compatible with edl.json.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "source_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "plan_hash": "sha256:5f4d96ac0799a5736fc0ba3b31eb66aaa29a49164889600ed625c93e00fc564c",
  "commentary_audio_hash": "sha256:24ca4039cf57fd0398d31293222b44d45cb8ff83bac954efbcdb13eea5ed3d20",
  "output": {
    "width": 1920,
    "height": 1080,
    "fps_num": 30000,
    "fps_den": 1001,
    "audio_sample_rate": 44100,
    "audio_channels": 2
  },
  "visual_policy": {
    "mask_subtitles": false,
    "add_subtitles": false,
    "add_text": false,
    "blur": false,
    "overlay": false,
    "preserve_burned_in_pixels": true
  },
  "placements": [
    {
      "placement_id": "placement-0000",
      "item_id": "item-0000",
      "kind": "reaction",
      "origin_block_id": "reaction-0017",
      "tl_start": 0.0,
      "tl_end": 8.96,
      "video": {
        "src": "C:/media/source.mp4",
        "src_in": 570.0,
        "src_out": 578.96,
        "speed": 1.0,
        "filters": []
      },
      "audio": {
        "mode": "source",
        "source_src": "C:/media/source.mp4",
        "source_in": 570.0,
        "source_out": 578.96,
        "source_gain_db": 0.0,
        "tts_audio_path": null,
        "tts_gain_db": null,
        "bed_audio_path": null,
        "bed_in": null,
        "bed_out": null,
        "bed_gain_db": null,
        "filters": []
      },
      "warnings": []
    },
    {
      "placement_id": "placement-0001",
      "item_id": "item-0001",
      "kind": "commentary",
      "origin_block_id": "commentary-0008",
      "tl_start": 8.96,
      "tl_end": 18.4,
      "video": {
        "src": "C:/media/source.mp4",
        "src_in": 579.5,
        "src_out": 588.94,
        "speed": 1.0,
        "filters": []
      },
      "audio": {
        "mode": "tts_bed",
        "source_src": null,
        "source_in": null,
        "source_out": null,
        "source_gain_db": null,
        "tts_audio_path": "audio/commentary-slot-0001.mp3",
        "tts_gain_db": 1.0,
        "bed_audio_path": "stems/no_vocals.wav",
        "bed_in": 579.5,
        "bed_out": 588.94,
        "bed_gain_db": -14.0,
        "filters": ["bed_fade_180ms", "commentary_limiter_-1.5db"]
      },
      "warnings": []
    }
  ],
  "total_duration_s": 18.4,
  "created_at": "2026-07-13T13:00:00Z",
  "warnings": []
}
~~~

Proposed audio mode values:

- source: original source audio with the same span as video;
- tts: TTS only;
- tts_bed: TTS plus an explicitly referenced non-vocal bed;
- silence: no audio, allowed only for a validated short transition.

Invariants:

- placement_id values are unique and timeline order is exact;
- placements tile the output without gaps or overlaps beyond one frame during
  pre-quantization, then tile exactly after quantization;
- tl duration equals video source duration divided by speed;
- speed is exactly 1.0 in proposed v1;
- reaction video and source audio spans are identical, gain is 0 dB, and both
  filter lists are empty;
- commentary cannot use audio mode source;
- TTS modes reference a valid commentary_audio item;
- bed spans are source-bounded and never contain the original mixed narrator
  track;
- visual filters are empty for all proposed v1 placements;
- visual_policy values are all enforced by the renderer and QA;
- total_duration_s equals the final placement tl_end.

## 10. remix_qa.json

Purpose: deterministic acceptance report from R8.

Proposed complete example:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "source_hash": "sha256:4f3bb04d5a9edcf50dc0a735b1596f31eecab51a942f41ce1fc80c68fd002afd",
  "edl_hash": "sha256:710b054cae177bb47f40972b7cf52304a7e73ecb75c19262b1fc6b4999030e7e",
  "output_path": "out/reaction_remix.mp4",
  "status": "pass",
  "duration": {
    "source_s": 1129.302,
    "output_s": 975.0,
    "output_ratio": 0.8634,
    "hard_min_ratio": 0.8,
    "preferred_range": [0.85, 0.9],
    "status": "pass"
  },
  "reaction_preservation": {
    "placements_checked": 27,
    "speed_mismatches": 0,
    "gain_mismatches": 0,
    "span_mismatches": 0,
    "min_audio_correlation": 0.994,
    "max_av_drift_ms": 16.7,
    "min_sample_frame_similarity": 0.998,
    "status": "pass"
  },
  "commentary": {
    "slots_checked": 11,
    "provider_mismatches": 0,
    "voice_mismatches": 0,
    "old_narrator_leakage_count": 0,
    "min_asr_text_match": 0.93,
    "status": "pass"
  },
  "visual_policy": {
    "mask_operations": 0,
    "subtitle_additions": 0,
    "text_overlays": 0,
    "blur_operations": 0,
    "other_overlays": 0,
    "status": "pass"
  },
  "audio": {
    "unexpected_silence_count": 0,
    "boundary_click_count": 0,
    "max_commentary_true_peak_dbfs": -1.7,
    "status": "pass"
  },
  "timeline": {
    "gap_count": 0,
    "overlap_count": 0,
    "decode_ok": true,
    "status": "pass"
  },
  "repairs": [],
  "created_at": "2026-07-13T13:20:00Z",
  "warnings": []
}
~~~

Invariants:

- status is fail if any hard gate fails;
- output ratio below 0.80 is always a hard failure;
- speed, source span, or gain mismatch on any reaction is a hard failure;
- any configured visual edit operation is a hard failure;
- provider or voice mismatch is a hard failure;
- old narrator leakage is a hard failure for a commentary placement;
- thresholds such as correlation, frame similarity, drift, and ASR text match
  come from config and are recorded in QA metadata;
- repairs list every automatic repair attempt, affected IDs, previous result,
  and new result;
- decode failure, timeline gap, or overlap is a hard failure.

## 11. Proposed Sidecars

Each stage should also write a sidecar or manifest:

- reaction_source.meta.json;
- reaction_transcript.meta.json;
- reaction_blocks.meta.json;
- remix_plan.meta.json;
- commentary_script.meta.json;
- commentary_audio.manifest.json;
- remix_edl.meta.json;
- render.meta.json.

Minimum sidecar fields:

~~~json
{
  "schema_version": "reaction-remix.proposed.v1",
  "stage": "segment",
  "algorithm_version": "proposed-1",
  "input_hashes": {},
  "config_hash": "sha256:example",
  "output_hashes": {},
  "cache_hits": [],
  "created_at": "2026-07-13T12:20:00Z",
  "warnings": []
}
~~~

An output is resumable only when its schema validates, all declared output
files exist, output hashes match, and the stage identity equals current inputs
and content-affecting config.

## 12. LLM Boundary

The proposed planner and writer may emit:

- block IDs;
- turn IDs;
- cut-point IDs;
- item order;
- roles, summaries, reasons, and Japanese commentary text.

They may not emit authoritative:

- source or timeline timecodes;
- media paths;
- audio duration measurements;
- provider identity;
- preservation flags;
- render commands.

Code derives those values and rejects unknown or incompatible IDs. This follows
the existing Recap rule that an LLM selects semantic identifiers while code
owns media timecodes.
