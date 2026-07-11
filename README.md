# Recap

`Recap` là pipeline tạo video recap review từ một tập phim. Hiện project đang xây **Giai đoạn 1 — Ingest & Hiểu phim**.

## GĐ1 làm gì

Nhận `film.mp4`, tạo:

- `film_map.json`: danh sách segment `speech` và `visual` đúng contract trong `AGENTS.md`.
- `film_map.meta.json`: metadata run, model, duration, cache hits, warning count.

GĐ1 chỉ làm ingest/scene map. Không viết review, không TTS, không shot detection, không render.

## Cài đặt

Yêu cầu:

- Python 3.11+
- `ffmpeg` và `ffprobe` có trong `PATH`
- `OPENAI_API_KEY`

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e .[dev]
```


## Chạy toàn pipeline bằng `run.py`

Orchestrator chạy 6 stage bằng một lệnh, tôn trọng cache của từng stage và ghi toàn bộ artifact vào `--run-dir`. GĐ4 `shots` chạy song song với chuỗi GĐ1→GĐ3, sau đó GĐ5/GĐ6 chạy khi đủ input.

```powershell
python run.py `
  --input path\to\film.mp4 `
  --run-dir runs\ep01 `
  --config config.example.yaml
```

Preset phim lẻ ổn định hiện tại dùng trực tiếp:

```powershell
python run.py --input path\to\film.mp4 --run-dir runs\movie01 --config config.movie.stable.yaml
```

Preset phim lẻ có visual index/rerank thử nghiệm:

```powershell
python -m pip install -e .[visual-index]
python run.py --input path\to\film.mp4 --run-dir runs\movie-visual01 --config config.movie.visual.yaml
```

Preset video/phim nguồn tiếng Việt, không dịch KO→EN:

```powershell
python run.py --input path\to\video-vi.mp4 --run-dir runs\movie-vi01 --config config.vi.stable.yaml
```

- `movie` preset ưu tiên kể mạch dễ hiểu từ đầu phim: `storymap`, `hook_mode=setup`, `target_ratio=auto`.
- GĐ5 dùng `match_strategy=chronological`, `w_semantic=0.15`, `audio_delay_s=0.0`; semantic/story/intent chỉ làm tie-breaker, không kéo footage lệch nhịp nguồn.
- Không dùng cutoff intro cố định; GĐ0 `preflight` detect non-story/intro theo từng video.
- Với nguồn tiếng Việt, `config.vi.stable.yaml` dùng `source_language=vi`, `translate_mode=none` và `aligner=whisperx`; GĐ1 giữ transcript Việt trực tiếp trong `film_map.json` và forced-align bằng WhisperX khi runtime có sẵn.

Tùy chọn resume/debug:

```powershell
python run.py --input path\to\film.mp4 --run-dir runs\ep01 --config config.yaml --dry-run
python run.py --input path\to\film.mp4 --run-dir runs\ep01 --config config.yaml --from tts --to match
python run.py --input path\to\film.mp4 --run-dir runs\ep01 --config config.yaml --force-stage match
python run.py --input path\to\film.mp4 --run-dir runs\ep01 --config config.yaml --only render
```

Run directory chính:

- `film_map.json`, `review_script.json`, `voiceover.mp3`, `beats_timing.json`, `shots.json`, `edl.json`, `edl.qa.json`, `edl.review.html`, `recap.mp4`
- Khi bật visual preset: thêm `shot_visual_index.json`, `visual_index/`, `edl.visual.qa.json`.
- `*.meta.json`, `audio/`, `shots/`, `work/<stage>/`, `run.log`, `summary.json`

`summary.json` gom duration từng stage, trạng thái run/skip, warnings, `timecode_qa` và ba số calibrate: `real_ratio`, `n_beats_widened`, `duration_match`. Nếu `timecode_qa.approximate_timecodes=true`, ưu tiên kiểm tra `edl.review.html`/alignment trước khi chỉnh audio delay global.

## Chạy GĐ1

```powershell
$env:OPENAI_API_KEY = "sk-..."

python -m ingest `
  --input path\to\film.mp4 `
  --output out\film_map.json `
  --whisper-model large-v3 `
  --gap-threshold 4.0 `
  --max-vision-frames 200 `
  --translate-model gpt-4.1-mini `
  --vision-model gpt-4.1-mini `
  --device cpu `
  --work-dir work
```

Dùng `--device cuda` nếu máy có CUDA phù hợp với `faster-whisper`.

## Cache / resume

Artifacts trung gian nằm trong `--work-dir`:

- `audio.wav`
- `transcript_raw.json`
- `translated.json`
- `frames/`
- `vision.json`

Chạy lại sẽ dùng cache nếu artifact tồn tại. Thêm `--force` để chạy lại toàn bộ artifacts GĐ1.

## Test

```powershell
python -m pytest -q
```

Test hiện tại dùng mock/unit, chưa yêu cầu clip thật. Khi có clip ngắn, chạy lệnh GĐ1 ở trên để smoke test real pipeline.
## Chạy GĐ2

GĐ2 nhận `film_map.json` và tạo `review_script.json` + `review_script.meta.json`.

GĐ2 là tác vụ LLM nặng nên mặc định dùng ChatGPT qua Playwright persistent browser, không dùng paid API.

Chuẩn bị lần đầu:

```powershell
python -m playwright install chromium
```

Đăng nhập ChatGPT bằng profile dùng cho GĐ2 trước khi chạy thật. Nếu chưa login, CLI sẽ báo lỗi rõ.

```powershell
python -m review `
  --film-map out\film_map.json `
  --output out\review_script.json `
  --target-ratio 0.33 `
  --tts-cps 15 `
  --min-coverage 0.85 `
  --max-qa-iterations 3 `
  --work-dir work\review `
  --chatgpt-profile-dir data\chrome_user_data\PROFILE_GPT_1
```

Artifacts cache GĐ2:

- `work/review/outline.json`
- `work/review/narration.json`
- `work/review/qa.json`
- `work/review/style_qa.json`
- `work/review/narration_style_checked.json`
- `work/review/revisions/`
- `work/review/style_revisions/`

Thêm `--force` để rebuild cache GĐ2.
## Chạy GĐ3

GĐ3 nhận `review_script.json` và tạo `audio/<beat_id>.mp3`, `voiceover.mp3`, `beats_timing.json`, `tts_meta.json`.

Provider mặc định là `auto`: AI33.PRO Vivoo V3 trước, Genmax fallback nếu AI33 lỗi. GĐ3 dùng cache theo hash narration để tránh tốn lại chi phí TTS khi chạy lại.

Env vars:

- `VIVOO_API_KEY`: dùng cho AI33.PRO.
- `GENMAX_API_KEY`: dùng cho Genmax fallback hoặc `--provider-mode genmax`.

```powershell
python -m tts `
  --review-script out\review_script.json `
  --output-audio out\voiceover.mp3 `
  --output-timing out\beats_timing.json `
  --voice-id <ai33_voice_id> `
  --provider-mode auto `
  --model eleven_multilingual_v2 `
  --inter-beat-pause 0.15 `
  --concurrency 3 `
  --tts-text-normalization vi `
  --film-meta out\film_map.meta.json `
  --work-dir work\tts
```

Nếu chỉ dùng Genmax:

```powershell
python -m tts `
  --review-script out\review_script.json `
  --output-audio out\voiceover.mp3 `
  --output-timing out\beats_timing.json `
  --voice-id <ai33_or_fallback_voice_id> `
  --provider-mode genmax `
  --genmax-voice-id <genmax_voice_id>
```

Artifacts/cache GĐ3:

- `work/tts/raw/`
- `work/tts/audio/`
- `work/tts/manifest.json`
- `out/audio/<beat_id>.mp3`
- `out/voiceover.mp3`
- `out/beats_timing.json`
- `out/tts_meta.json`

Thêm `--force` để render lại toàn bộ beat.
## Chạy GĐ4

GĐ4 nhận file phim và tạo `shots.json`, thumbnails, `shots.meta.json`. Stage này chạy offline, không dùng API.

Dependencies chính:

- `scenedetect`
- `opencv-python-headless`
- `numpy`
- `ffmpeg/ffprobe` trong `PATH`

```powershell
python -m shots `
  --input path\to\film.mp4 `
  --output out\shots.json `
  --thumb-dir out\shots `
  --detector adaptive `
  --min-shot-len 0.4 `
  --sample-frames 5 `
  --face-detection on `
  --min-brightness 0.06 `
  --work-dir work\shots
```

Face detection v1 dùng Haar cascade bundled trong OpenCV. Nếu không cần face metrics:

```powershell
python -m shots --input path\to\film.mp4 --output out\shots.json --face-detection off
```

Với phim dài, có thể dùng detector ffmpeg nhanh hơn và split scene dài để GĐ5 có đủ candidate granular:

```powershell
python -m shots `
  --input path\to\film.mp4 `
  --output out\shots.json `
  --thumb-dir out\shots `
  --detector ffmpeg-scene `
  --scene-threshold 0.3 `
  --scene-scale-width 640 `
  --max-shot-len 8 `
  --frame-sampling batch `
  --face-detection off
```

`--frame-sampling batch` opens the video once and samples frames in timeline order, then reuses those frames for features and thumbnails. The default remains `per-shot` for compatibility; `config.movie.visual.yaml` enables `batch` for long-movie visual runs.

Cache GĐ4:

- `work/shots/detection.json` — shot spans, không phụ thuộc `video_profile.json`.
- `work/shots/features.json` — motion/brightness/face/thumb feature, không phụ thuộc `video_profile.json`.
- `work/shots/profile_marking.json` — apply `video_profile.json` để set `is_story=false` / `exclude_reason`.
- `work/shots/thumbs/`

Khi chỉ đổi `video_profile.json`, GĐ4 chỉ re-apply profile marking và không re-detect/recompute features. Thêm `--profile-only` để debug re-apply từ cache; thêm `--force` để detect/tính feature lại toàn bộ.

## Chạy GĐ4.5 Visual Index

GĐ4.5 là optional stage để GĐ5 có tín hiệu hình thật thay vì chỉ text-text semantic. Stage này trích keyframe theo shot, encode bằng SigLIP2 mặc định, và lưu vector sidecar `.npy` float16.

```powershell
python -m pip install -e .[visual-index]

python -m visual_index `
  --film path\to\film.mp4 `
  --shots out\shots.json `
  --output out\shot_visual_index.json `
  --asset-dir out\visual_index `
  --embedding-mode siglip2 `
  --embedding-model google/siglip2-base-patch16-384 `
  --device auto `
  --keyframes-per-shot 2 `
  --work-dir work\visual_index
```

GĐ5 dùng visual index bằng:

```powershell
python -m match `
  --review-script out\review_script.json `
  --beats-timing out\beats_timing.json `
  --shots out\shots.json `
  --visual-index out\shot_visual_index.json `
  --visual-mode rerank `
  --w-visual 0.20 `
  --output out\edl.json
```

Visual score chỉ rerank candidates trong source window/widen hiện có; nếu thiếu index/model thì GĐ5 fallback text-only và ghi `edl.visual.qa.json` với `visual_enabled=false`.
## Chạy GĐ5

GD5 nhan `review_script.json`, `beats_timing.json`, `shots.json` va tao `edl.json` + `edl.meta.json` + `edl.qa.json`. Neu truyen them `--film-map`, GD5 co the dung semantic offline: `bge-m3` multilingual embedding la default orchestrator, con `tfidf` la fallback nhe. Stage nay chi xu ly JSON, khong decode video va khong dung API.

```powershell
python -m match `
  --review-script out\review_script.json `
  --beats-timing out\beats_timing.json `
  --shots out\shots.json `
  --film-map out\film_map.json `
  --output out\edl.json `
  --output-qa out\edl.qa.json `
  --output-review-html out\edl.review.html `
  --review-asset-dir out\edl.review `
  --review-thumbs-per-beat 8 `
  --semantic-mode bge-m3 `
  --semantic-model BAAI/bge-m3 `
  --semantic-device auto `
  --semantic-batch-size 16 `
  --semantic-cache-dir work\match\semantic `
  --match-strategy chronological `
  --chronology-weight 0.70 `
  --max-source-drift-s 12 `
  --w-semantic 0.15 `
  --min-semantic-score 0.22 `
  --min-clip 3.0 `
  --max-clip 5.0 `
  --min-visual-clip 0.6 `
  --widen-margin 15 `
  --max-widen 3 `
  --allow-dark-fallback `
  --allow-repeat `
  --seed 1234 `
  --work-dir work\match
```

Nguyên tắc GĐ5:

- Semantic Phase 2 dung `BAAI/bge-m3` local multilingual embedding; `tfidf` van giu lam fallback khong can dependency nang.
- Movie matching mac dinh la `chronological`: bam timecode/source chronology truoc, semantic/story/intent chi lam soft tie-breaker de tranh audio mot noi hinh mot noi.
- Cai embedding deps khi dung `bge-m3`: `pip install -e ".[semantic-embed]"`.
- `edl.qa.json` ghi provider/model/device/cache hits, từng beat chọn shot nào, semantic rank/score, `expected_src_position`, `source_drift_s`, `chronology_score` và warning `low semantic match`/`high source drift`.
- `edl.review.html` là QA artifact trực quan để mở bằng browser: narration, selected thumbnails, source span, semantic/motion/brightness/face/reuse/drift và warnings theo beat.
- Face l? ?i?m c?ng m?m, kh?ng l?c c?ng.
- Placement m?c ??nh 1:1 speed `1.0`.
- `min_visual_clip` mac dinh `0.6s` de tranh flash-cut; pause gap ngan duoc absorb bang source capacity hoac slowdown toi da 10% tren hai clip ke nhau, va placement dai hon `max_clip` se duoc split lien tuc cung source/shot.
- Khi thiếu footage, GĐ5 tính diversity capacity theo tối đa một clip `max_clip` cho mỗi shot. Nó thử shot usable rồi shot story chỉ bị loại vì tối trong cùng source window trước khi widen, và không vượt quá `max_widen`.
- Repeat fallback ưu tiên phần source chưa dùng của các shot đã chọn, tránh lặp ngay shot liền trước khi còn alternative cùng chronology tier, rồi mới dùng span có overlap thấp nhất.
- `edl.qa.json`/HTML hiển thị capacity, widen count, dark fallback, unused-source reuse và overlapping repeat; `edl.meta.json.algorithm_version` làm stale artifact tự rebuild qua orchestrator.
- Cache nằm ở `work/match/plan.json`; hash cache gồm `film_map.json`, config semantic và config review HTML; thêm `--force` để recompute. Nếu EDL lấy từ cache, GĐ5 vẫn ghi lại `edl.qa.json` và `edl.review.html`.

## Chạy GĐ6

GĐ6 nhận `edl.json`, `voiceover.mp3`, `film.mp4` và tạo `recap.mp4` + `render.meta.json`. Stage này chỉ render offline bằng `ffmpeg/ffprobe`, tắt hoàn toàn tiếng gốc và không tạo caption/nhạc nền.

```powershell
python -m render `
  --edl out\edl.json `
  --voiceover out\voiceover.mp3 `
  --film path\to\film.mp4 `
  --output out\recap.mp4 `
  --width 1920 --height 1080 --fps 30 `
  --fit cover `
  --crf 20 --preset medium `
  --concurrency 4 `
  --work-dir work\render
```

Nguyên tắc GĐ6:

- Frame-lock toàn cục: quantize mốc timeline theo frame trước khi cắt để tránh trôi sync.
- Mỗi placement được re-encode thành temp clip video-only cùng resolution/fps/codec/pix_fmt.
- Temp clip dùng cache trong `work/render/temp_clips/`; thêm `--force` để render lại toàn bộ cache GĐ6.
- Concat temp clips bằng demuxer `-c copy`, sau đó mux `voiceover.mp3` thành audio duy nhất.
- Nếu video-only concat ngắn hơn voiceover, GĐ6 chỉ encode một tail freeze-frame clip ngắn rồi concat copy; không re-encode toàn bộ video-only trong đường bình thường.
- `render.meta.json` ghi duration video/audio, số temp clips, cache hits và warnings.


### Transcript correction / glossary

GĐ1 có thể sửa tên nhân vật/entity trước bước dịch KO→EN mà không đổi timecode hoặc id segment.

```powershell
python -m ingest `
  --input path\to\film.mp4 `
  --output out\film_map.json `
  --asr-provider openai-gpt4o-hybrid `
  --aligner whisperx `
  --transcript-correction glossary `
  --glossary glossary.example.yaml `
  --work-dir work\ingest
```

`--transcript-correction`:

- `off`: mặc định, không sửa transcript.
- `glossary`: sửa deterministic bằng replacements trong glossary, rẻ và nên dùng trước.
- `openai`: dùng glossary + OpenAI để sửa lỗi tên/entity/homophone rõ ràng; chỉ dùng cho pass nhẹ vì vẫn tốn API.

Glossary có thể là JSON/YAML/TXT. Repo có mẫu `glossary.example.yaml`. Ví dụ YAML:

```yaml
context: Korean movie transcript. Keep Korean text, do not translate.
names:
  - 황준현
  - 최성
replacements:
  문지현: 황준현
  최정의 부식: 최성 FC
```

Artifact cache mới: `transcript_corrected.json`. Meta GĐ1 ghi `transcript_correction_mode`, `transcript_correction_model`, và `transcript_correction_warnings`.


GĐ1 mặc định bỏ các segment non-Korean CJK/Japanese trong `30s` đầu để tránh opening song/credit làm bẩn review. Có thể chỉnh hoặc tắt:

```powershell
python -m ingest --input film.mp4 --output out\film_map.json --drop-non-korean-intro-s 0
```

GĐ1 cũng split visual/silent gaps dài trước khi gọi vision để tránh một visual segment bao trùm quá nhiều cảnh. Mặc định `--max-visual-gap-s 20`; đặt `0` để tắt:

```powershell
python -m ingest --input film.mp4 --output out\film_map.json --max-visual-gap-s 12
```

GĐ2 có deterministic narration consistency pass sau khi ChatGPT viết narration: pass này dùng glossary để chuẩn hóa alias tên/entity như `Choi Seon/Sung/Song -> Choi Seong` hoặc `Hwang Junhyun -> Hwang Jun-hyun`. Artifact cache: `work/review/narration_consistent.json`.

GĐ2 lưu session ChatGPT theo từng video/run để tránh trộn ngữ cảnh giữa video khác nhau. Mặc định `--chat-session-policy auto` sẽ resume `work/review/chat_session_meta.json` nếu có, nếu chưa có thì mở chat mới. Có thể ép chat mới bằng:

```powershell
python -m review `
  --film-map runs\ep01\film_map.json `
  --output runs\ep01\review_script.json `
  --chat-session-policy new `
  --chat-title ep01
```

Các policy: `auto`, `new`, `resume`. Metadata được lưu ở `work/review/chat_session_meta.json` hoặc path từ `--chat-session-meta`.

Nếu phim/tập có intro/opening chỉ có hình ảnh không liên quan narration, chạy GĐ1/GĐ4 với cutoff cùng giá trị. Ví dụ tập này dùng `120s`:

```powershell
python -m preflight --input film.mp4 --output runs\ep01\video_profile.json --classifier openclip
python -m ingest --input film.mp4 --output runs\ep01\film_map.json --video-profile runs\ep01\video_profile.json
python -m shots --input film.mp4 --output runs\ep01\shots.json --thumb-dir runs\ep01\shots --video-profile runs\ep01\video_profile.json
```

## Visual Index v1.1 and calibration

Visual Index v1.1 validates film/shots/config identity plus every embedding sidecar checksum and dimension before reuse. Legacy v1.0 indexes are treated as uncalibrated and must be rebuilt. SigLIP scoring uses learned calibration (`sigmoid(cosine * logit_scale + logit_bias)`), combines VI/EN query weights on each keyframe, then picks the best keyframe in the shot.

For long videos, use `--frame-sampling batch`. G5 can use `--visual-device cuda --visual-batch-size 32`. Candidate ordering is drift-tier first, so visual relevance only reranks within a chronology-valid tier.

Calibrate `w_visual` from a hand-labeled two-video golden set:

```powershell
python -m match.calibrate_visual `
  --golden examples\visual_golden.example.json `
  --weights 0,0.05,0.10,0.15,0.20,0.25,0.30,0.40 `
  --output work\visual_calibration.json
```

The report maximizes NDCG@5 and acceptable top-1 rate while preventing regressions in drift, high-drift rate, reuse, and short clips. Equal results select the lower weight.

The current two-movie calibration selects `w_visual=0.15` for `config.movie.visual.yaml` (`NDCG@5 0.9341`, top-1 `1.0`). Stable presets keep visual matching disabled.

Long local ASR now overlaps chunks and atomically caches validated per-chunk transcripts to avoid boundary loss after interrupted runs. G6 validates both tail padding and legacy full re-encode padding before muxing audio.

## Runtime Notes From Real E2E

- For long movie reviews, use the logged-in ChatGPT persistent profile and keep other Chrome windows for that profile closed before running GĐ2.
- If using cookies from `auto_YT`, pass only a freshly captured `session_chatgpt.json`; stale cookies can trigger ChatGPT `expired-session` modal.
- GĐ2 supports `--reply-timeout-s`; long movie outline/narration/QA can require `900` seconds per response.
- Local runtime artifacts are ignored: `.env`, `data/`, `runs/`, `work/`, and `out/` must not be committed.
- Movie mode is independent per video. Series mode should later add shared glossary/entity bible and episode summaries rather than relying on one giant chat history.


## GĐ0 Video Profile / Intro Detection

Run preflight before ingest/shots to detect per-video intro/opening/title/logo ranges instead of hardcoding a cutoff:

```powershell
python -m preflight `
  --input film.mp4 `
  --output video_profile.json `
  --max-intro-s 240 `
  --sample-every-s 5 `
  --classifier heuristic
```

`heuristic` is conservative and does not hard-exclude without enough evidence. For local visual classification, install `pip install -e ".[video-profile]"` and run `--classifier openclip`. Manual `--skip-intro` / `--drop-visual-before-s` remain debug overrides only.

### Current intro/opening policy

The pipeline does not assume every movie has an intro. Run G?0 `python -m preflight` per video and only exclude opening/non-story footage when `video_profile.json` contains confident `non_story_ranges`. If detection is uncertain, the profile records `uncertain_intro` and downstream stages keep the opening footage. `skip_intro` / `drop_visual_before_s` remain debug overrides only, not default behavior. Movie micro-beats are experimental opt-in (`micro_beats: false` by default); use opening match QA/ordered fill for localized sync issues instead of splitting the whole film.

### Movie story map and visual intent

Movie runs now include an optional G1.5 story structure stage:

```powershell
python -m storymap `
  --film-map runs\movieilm_map.json `
  --video-profile runs\movieideo_profile.json `
  --output runs\movie\story_map.json `
  --output-qa runs\movie\story_map.qa.json
```

`python -m review` can consume `--story-map` and writes `review_script.intent.json` without changing `review_script.json`. `python -m match` can consume `--review-intent` and `--story-map`; opening ordered fill is enabled by default to keep early footage chronological when the voiceover is setting up the film.

### Sync QA report

G?5 writes `edl.sync.qa.json` for debugging perceived audio/video sync. The report compares `beats_timing.json` with actual EDL placements per beat and flags source-order mismatch, beat timing deltas, reuse-heavy beats, short clips, long clips, and placements outside the beat timing window. Check this before applying a global render audio delay.
