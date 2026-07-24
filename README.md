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
python -m pip install -e ".[dev]"
```


## Chạy toàn pipeline bằng `run.py`

Orchestrator chạy 6 stage bằng một lệnh, tôn trọng cache của từng stage và ghi toàn bộ artifact vào `--run-dir`. GĐ4 `shots` chạy song song với chuỗi GĐ1→GĐ3, sau đó GĐ5/GĐ6 chạy khi đủ input.

```powershell
python run.py `
  --input path\to\film.mp4 `
  --run-dir runs\ep01 `
  --config config.example.yaml
```

Preset production cho phim Hàn dùng CUDA + WhisperX + Visual Index:

```powershell
python -m pip install -e ".[movie-visual,dev]"
python run.py --input path\to\film.mp4 --run-dir runs\movie01 --config config.movie.production.yaml
```

Preset phim lẻ ổn định không dùng Visual Index:

```powershell
python run.py --input path\to\film.mp4 --run-dir runs\movie01 --config config.movie.stable.yaml
```

Preset phim lẻ có visual index/rerank thử nghiệm:

```powershell
python -m pip install -e ".[movie-visual,dev]"
python run.py --input path\to\film.mp4 --run-dir runs\movie-visual01 --config config.movie.visual.yaml
```

`movie-visual` gom WhisperX, BGE-M3 và SigLIP2 cho preset visual. OpenCLIP intro detection vẫn là dependency riêng: `python -m pip install -e ".[video-profile]"`.

## Release Candidate Gate

Gate offline dùng chung cho local và GitHub Actions Windows:

```powershell
# CI-equivalent, không cần media/API/GPU
powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1 -SkipMediaSmoke

# Gate local bắt buộc trước khi tag, cần ffmpeg và một phim thật
powershell -ExecutionPolicy Bypass -File scripts/release_check.ps1 `
  -MediaPath "C:\path\to\movie.mp4"
```

Gate kiểm tra secret trong tracked tree + Git history, full test/compile, editable metadata, wheel content/install/import, CLI help, production dry-run và optional real-media GĐ0/GĐ1 cache smoke không API. Báo cáo nằm ở `work/release-gate/report.json`; tiêu chí chốt release nằm trong `RELEASE_CHECKLIST.md`.

Preset video/phim nguồn tiếng Việt, không dịch KO→EN:

```powershell
python run.py --input path\to\video-vi.mp4 --run-dir runs\movie-vi01 --config config.vi.stable.yaml
```

- `movie` preset ưu tiên kể mạch dễ hiểu từ đầu phim: `storymap`, `hook_mode=setup`, `target_ratio=auto`.
- GĐ5 dùng `match_strategy=chronological`, `w_semantic=0.15`, `audio_delay_s=0.0`; semantic/story/intent chỉ làm tie-breaker, không kéo footage lệch nhịp nguồn.
- Không dùng cutoff intro cố định; GĐ0 `preflight` detect non-story/intro theo từng video.
- Với nguồn tiếng Việt, `config.vi.stable.yaml` dùng `source_language=vi`, `translate_mode=none` và `aligner=whisperx`; GĐ1 giữ transcript Việt trực tiếp trong `film_map.json` và forced-align bằng WhisperX khi runtime có sẵn.

Anime recap V1 có hai preset local:

```powershell
python run.py --input path\to\anime.mp4 --run-dir runs\anime-series01 --config config.anime.series.yaml
python run.py --input path\to\anime-movie.mp4 --run-dir runs\anime-movie01 --config config.anime.movie.yaml
python -m series_recap --manifest examples\anime\series_manifest.example.yaml --config config.anime.series.practical.yaml --episodes 1-12
```

- `config.anime.series.yaml`: `content_type=anime_series`, `source_language=ja`, `translate_mode=ja-en`, `shots.face_detection=off`, `match.w_face=0.0`, `match.w_visual=0.0`, `exclude_non_story=true`, `orchestrator.recap_mode=auto`.
- `config.anime.series.practical.yaml`: preset season 12 tap thuc dung. OpenAI API chi dung cho JA->EN transcript translation (`translation_required=true`, `translation_min_success_ratio=0.95`), `vision_provider="off"` va `max_vision_frames=0`; review/composer/QA dung ChatGPT Playwright, ASR/shots/match/render local, TTS dung provider configured.
- `config.anime.series.localvision.yaml`: opt-in local Qwen vision (`vision_provider=local_qwen2_5_vl`, `Qwen/Qwen2.5-VL-7B-Instruct`, `max_vision_frames=30`, resize long edge 768). Cai optional deps bang `python -m pip install -e ".[anime-vision]"`; neu local model/deps thieu thi ingest warning va tiep tuc khong co visual gap descriptions.
- Khong dung Playwright cho batch vision automation; Playwright chi la duong text/review/composer/QA. Batch vision phai la OpenAI API co cap ro rang hoac local model opt-in.
- `config.anime.movie.yaml`: `content_type=anime_movie`, cùng ingest defaults tiếng Nhật, `hook_mode=setup`, và cùng posture strict OP/ED/preview guard.
- `preflight.manual_ranges` và `preflight.anime_context` nhận YAML/JSON local, rồi merge vào `video_profile.non_story_ranges`; `review.context_file` nạp cùng anime context để giữ glossary, continuity và spoiler guard nhất quán.
- Anime context là metadata local only, không dùng AniList/API. OP/ED/theme/preview/recap zones phải đi qua manual ranges hoặc detector tin cậy, không hardcode duration.

- Anime dai tap dung `series_manifest.yaml` local lam source of truth cho `series_id`, `episode_key`, `episode_number`, `title`, `source_path`, `arc`, `spoiler_limit_episode`; xem `examples/anime/series_manifest.example.yaml`.
- Khi `orchestrator.recap_mode=auto`, `episode_planner` ghi `episode_meta.json`, `episode_memory.json` va append `series_memory_index.jsonl`. Mode: `>=0.70 full`, `0.35-0.69 quick`, `0.15-0.34 merge`, `<0.15 skip`.
- `quick` tu ha review `target_ratio` ve `0.12` va tap trung "dieu can nho cho tap sau"; `merge/skip` van ingest/storymap/memory nhung short-circuit review/TTS/match/render.
- Season recap V1 dung CLI rieng de ra mot video chung cho nhieu tap:

```powershell
python -m series_recap --manifest examples\anime\series_manifest.example.yaml --config config.anime.series.yaml --episodes 1-3
```

- Mac dinh anime series dung `series_recap.format=episode_arc_chaptered` va `detail_level=detailed`: mot script/voiceover/EDL tong, nhung composer viet theo arc khoang 3 tap de recap season dai khong bi roi rac. `episode_chaptered` va `compact` van co san cho batch ngan hoac season summary ngan.
- Anime series preset dat target 12 tap 24 phut vao khoang 35-45 phut (`target_total_min_s=2100`, `target_total_max_s=2700`) va hard cap 50 phut (`target_total_hard_cap_s=3000`). Moi tap canon non-skip van co budget rieng (`episode_min_s=90`, `episode_normal_s=180`, `episode_high_s=300`, `arc_size=3`).
- Cac target ratio cu `quick=0.14`, `full=0.22`, `merge=0.05`, `skip=0.0` va `series_recap.tts_cps=24.0` van giu cho `episode_chaptered`/tham chieu, nhung mode detailed moi uu tien season/arc target plan.
- `series_recap` khong noi raw video truoc. Moi tap duoc chay episode-first toi `episode_planner` + `shots`, sau do `series_composer -> tts -> series_match -> render` tao `runs\<series_id>\series_recap\series_recap.mp4`.
- Artifact moi gom `series_event_bank.json`, `series_review_script.json`, `series_tts_script.json`, `series_chapters.json`, `youtube_chapters.txt`, va `edl.source_map.json`. `edl.json` van dung contract cu, nhung `src` co the tro toi nhieu tap; GĐ6 render voi `--source-map` de cat dung file nguon.
- Detailed season recap ghi them `series_arc_plan.json` va `series_composer.qa.json` de audit ngan sach arc/episode, prompt count, revision count va QA warnings.
- Neu `preflight.manual_ranges` khong duoc set trong config, `series_recap` tu tim sidecar canh manifest theo convention `manual_ranges.<episode_key>.yaml|yml|json` de giu OP/ED/preview guard rieng tung tap.

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
- `transcript_aligned.json` + `transcript_quality.json`
- `transcript_corrected.json` + `transcript_correction.meta.json`
- `translated.json`
- `frames/`
- `vision.json`
- `cache_manifest.json`

GĐ1 chỉ reuse cache khi manifest khớp film/config/artifact. Đổi ASR giữ audio; đổi glossary/correction giữ transcript aligned; đổi translation giữ transcript; đổi `video_profile.json` hoặc vision config chỉ rebuild vision. Cache legacy thiếu manifest sẽ rebuild một lần. Thêm `--force` để xóa toàn bộ cache GĐ1.

## Test

```powershell
python -m pytest -q
```

Test hiện tại dùng mock/unit, chưa yêu cầu clip thật. Khi có clip ngắn, chạy lệnh GĐ1 ở trên để smoke test real pipeline.

## Quality tooling

Development installs include `ruff` and `tach`:

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m tach check --dependencies
```

`ruff` is check-only and starts with critical syntax/name rules so it can run on the existing codebase without a repo-wide formatting diff. Tach records the desired runtime package boundaries in `tach.toml`; release gate runs Tach as a blocking check and writes `work/release-gate/tach-report.txt`. Tach's pytest plugin is disabled by default so `python -m pytest -q` keeps running the full test selection.
## Chạy GĐ2

GĐ2 nhận `film_map.json` và tạo `review_script.json` + `review_script.meta.json`.

GĐ2 là tác vụ LLM nặng nên luôn dùng ChatGPT qua Playwright persistent browser làm backend chính. Chạy thẳng `openai_api` hoặc tắt backend bằng `off` không còn được hỗ trợ; hai giá trị legacy này sẽ báo lỗi config.

OpenAI review fallback là opt-in qua `review.openai_fallback_model`. Fallback chỉ được phép khi Playwright đã hết retry/recovery với lỗi browser được phân loại là cho phép fallback, `api_budget_guard` không phải `block`, và `OPENAI_API_KEY` tồn tại. Playwright thành công không khởi tạo OpenAI client và không yêu cầu API key.

Config mặc định:

```yaml
orchestrator:
  text_llm_backend: chatgpt_playwright
  api_budget_guard: warn
review:
  playwright_max_attempts: 2
  playwright_recovery_timeout_s: 60
  openai_fallback_model: null
```

Sau khi prompt đã submit, recovery chỉ đợi tiếp response hiện tại, không gửi lại prompt. Lỗi login/profile/config hoặc parse/validate output không kích hoạt OpenAI. Khi fallback được cấu hình, `work/review/openai_usage.json` ghi trạng thái configured/allowed/blocked/triggered, lý do, số lần Playwright thử, model và token usage.

Chuẩn bị lần đầu:

```powershell
python -m playwright install chromium
```

GĐ2 khóa profile ChatGPT tại `D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1`. Đăng nhập bằng đúng profile này trước khi chạy thật; CLI sẽ từ chối path khác hoặc báo lỗi rõ nếu session chưa login.

```powershell
python -m review `
  --film-map out\film_map.json `
  --output out\review_script.json `
  --target-ratio 0.33 `
  --tts-cps 15 `
  --min-coverage 0.85 `
  --max-qa-iterations 3 `
  --work-dir work\review `
  --chatgpt-profile-dir D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1
```

Artifacts cache GĐ2:

- `work/review/outline.json`
- `work/review/narration.json`
- `work/review/qa.json`
- `work/review/style_qa.json`
- `work/review/narration_style_checked.json`
- `work/review/revisions/`
- `work/review/style_revisions/`
- `work/review/cache_manifest.json`

Manifest GĐ2 hash nội dung `film_map`, metadata, story map, video profile, style sample và generation config. Thay đổi semantic input sẽ xóa đồng nhất toàn bộ artifact review; browser profile/headless/timeout không làm đổi cache. Thêm `--force` để rebuild cache GĐ2.
## Chạy GĐ3

GĐ3 nhận `review_script.json` và tạo `audio/<beat_id>.mp3`, `voiceover.mp3`, `beats_timing.json`, `tts_meta.json`.

Provider mặc định là `auto`: thử AI33.PRO Vivoo V3, sau đó Genmax khi có key + voice riêng, rồi OpenAI `gpt-4o-mini-tts` khi có `OPENAI_API_KEY`. Production preset dùng Genmax voice `VU16byTywsWv5JpI8rbc`; provider thiếu key được bỏ qua, và GĐ3 fail-fast nếu không có provider nào.

Env vars:

- `VIVOO_API_KEY`: dùng cho AI33.PRO.
- `GENMAX_API_KEY`: dùng cho Genmax fallback hoặc `--provider-mode genmax`.
- `OPENAI_API_KEY`: dùng cho OpenAI fallback hoặc `--provider-mode openai`.

```powershell
python -m tts `
  --review-script out\review_script.json `
  --output-audio out\voiceover.mp3 `
  --output-timing out\beats_timing.json `
  --voice-id <ai33_voice_id> `
  --provider-mode auto `
  --model eleven_multilingual_v2 `
  --openai-model gpt-4o-mini-tts `
  --openai-voice coral `
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

`tts_meta.json` ghi `providers_used`, số beat theo provider và `fallback_count`; manifest từng beat luôn ghi model/voice thực tế đã tạo audio.
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
  --end-credit-guard `
  --end-credit-tail-s 600 `
  --end-credit-threshold 0.60 `
  --face-detection off
```

`--frame-sampling batch` opens the video once and samples frames in timeline order, then reuses those frames for features and thumbnails. The default remains `per-shot` for compatibility; `config.movie.visual.yaml` enables `batch` for long-movie visual runs.

Cache GĐ4:

- `work/shots/detection.json` — shot spans, không phụ thuộc `video_profile.json`.
- `work/shots/features.json` — motion/brightness/face/thumb feature, không phụ thuộc `video_profile.json`.
- `work/shots/end_credit_marking.json` — tail-only deterministic credit score; đổi guard không recompute detection/features.
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
  --content-anchors `
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
- Beat co source span rat rong dung narration-only semantic de tao content timecode anchors. Candidate fill va chronology chi chay trong cac anchor interval; beat compact hoac anchor khong du capacity se fallback behavior cu.
- Content anchors tu dong tat khi `film_map.meta.json` ghi `approximate_timecodes=true`, vi anchor hep khong an toan khi segment timecode con tho.
- Cai embedding deps khi dung `bge-m3`: `pip install -e ".[semantic-embed]"`.
- `edl.qa.json` ghi provider/model/device/cache hits, từng beat chọn shot nào, semantic rank/score, `expected_src_position`, `source_drift_s`, `chronology_score` và warning `low semantic match`/`high source drift`.
- `edl.review.html` là QA artifact trực quan để mở bằng browser: narration, selected thumbnails, source span, semantic/motion/brightness/face/reuse/drift và warnings theo beat.
- Face l? ?i?m c?ng m?m, kh?ng l?c c?ng.
- Placement m?c ??nh 1:1 speed `1.0`.
- `min_visual_clip` mac dinh `0.6s` de tranh flash-cut; pause gap ngan duoc absorb bang source capacity hoac slowdown toi da 10% tren hai clip ke nhau, va placement dai hon `max_clip` se duoc split lien tuc cung source/shot.
- Khi thiếu footage, GĐ5 tính diversity capacity theo tối đa một clip `max_clip` cho mỗi shot. Nó thử shot usable rồi shot story chỉ bị loại vì tối trong cùng source window trước khi widen, và không vượt quá `max_widen`.
- Repeat fallback ưu tiên phần source chưa dùng của các shot đã chọn, tránh lặp ngay shot liền trước khi còn alternative cùng chronology tier, rồi mới dùng span có overlap thấp nhất.
- `edl.qa.json`/HTML hiển thị capacity, widen count, dark fallback, unused-source reuse và overlapping repeat; `edl.meta.json.algorithm_version` làm stale artifact tự rebuild qua orchestrator.
- Visual preset bật `opening_intra_beat_align` cho cả opening và long-beat hardening. Opening vẫn chỉ phân tích 30 giây đầu; non-opening beat chỉ splice khi timecode strict, dùng BGE-M3, chưa có content-anchor plan và baseline drift vượt `max(18s, 1.5 * max_source_drift_s)`. Các câu cùng anchor được gộp, transition confidence thấp gắn vào anchor mạnh kế tiếp, source window giữ thứ tự và không overlap.
- `hook_min_brightness=0.10` trong visual preset tránh mở video bằng placement quá tối; stable/default giữ `0.0`. QA HTML/JSON ghi mode, trigger drift, replaced ranges, source windows và shot thay thế hook.
- Visual preset bật `end_credit_guard` ở GĐ4 và `exclude_end_credits` ở GĐ5. Guard chỉ hard-exclude blank/credit-only trong 600 giây cuối; cảnh post-credit có story image và credit overlay vẫn được giữ. Credit-only không quay lại qua dark fallback, repeat hoặc pause filler.
- Cache nằm ở `work/match/plan.json`; hash cache gồm `film_map.json`, config semantic, `content_anchors`, `opening_intra_beat_align` và config review HTML; thêm `--force` để recompute. Nếu EDL lấy từ cache, GĐ5 vẫn ghi lại `edl.qa.json` và `edl.review.html`.

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

GĐ2 lưu session ChatGPT theo từng video/run để tránh trộn ngữ cảnh giữa video khác nhau. Mặc định `--chat-session-policy auto` chỉ resume `work/review/chat_session_meta.json` khi core input hash không đổi; nếu `film_map`, metadata, story map hoặc video profile đổi thì tự mở chat mới. Policy `resume` vẫn giữ URL cũ nhưng ghi warning khi input đã đổi. Có thể ép chat mới bằng:

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
- GĐ2 defaults to two Playwright attempts with a 60-second same-response recovery window. Only classified browser timeout/disconnect failures may reach the opt-in OpenAI fallback; login, profile, config, parse, validation, and programming errors fail directly.
- `api_budget_guard=block` still allows Playwright but blocks OpenAI fallback in every quality mode. ASR/vision/TTS remain local or dedicated-provider workflows because Playwright cannot reliably produce timestamped transcript, frame-analysis, or audio contracts.
- Local runtime artifacts are ignored: `.env`, `data/`, `runs/`, `work/`, and `out/` must not be committed.
- Movie mode is independent per video. Anime series Episode V1 stays episode-first and uses `episode_memory.json` plus `series_memory_index.jsonl` instead of relying on one giant chat history.


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

`video_profile.json` records film identity, preflight config hash, and cache version. GĐ1 receives this artifact through `--video-profile`; legacy profiles without integrity metadata are rebuilt once. Preflight frame samples are also invalidated when the film or sampling/classifier config changes.

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
