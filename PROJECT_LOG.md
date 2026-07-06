# PROJECT_LOG.md

Log theo dõi tiến độ dự án `Recap`.

## Mục tiêu dự án

- Dự án mới tại `D:\VibeCoding\Recap`.
- Tận dụng kiến thức, kiến trúc và pattern đã có từ:
  - `D:\VibeCoding\auto_YT`
  - `D:\VibeCoding\RUN_VEO_V1.1`

## Nguyên tắc làm việc

- Code identifiers, function/class names và comments dùng tiếng Anh.
- Trao đổi, giải thích với người dùng bằng tiếng Việt.
- Ưu tiên giải pháp đơn giản, đúng trọng tâm, không over-engineer.
- Thay đổi phải có lý do rõ ràng và gắn với mục tiêu hiện tại.
- Khi thêm feature lớn, cập nhật file này ngay sau khi hoàn tất.

## Context đã đọc từ project tham chiếu

### `auto_YT`

- Kiến trúc chính: Python/PyQt6 app + Playwright worker + Next.js dashboards + Postgres/Drizzle.
- Runtime local:
  - `web`: app `3000`, DB `5434`
  - `web_2`: app `3001`, DB `5435`
  - `web_3`: app `3002`, DB `5433`
- Pattern đáng tái sử dụng:
  - Central path config qua `paths.py`.
  - Account/session JSON trong `data/`.
  - Control server có endpoint start/stop/status.
  - Worker claim job từ DB, ghi heartbeat, update status.
  - Browser automation bằng Playwright với persistent Chrome profile.
  - Lưu conversation URL để tiếp tục phiên làm việc.
  - Dashboard callback có secret để đồng bộ pipeline.
  - Pipeline stage rõ ràng trong `web_3/lib/pipeline`.

### `RUN_VEO_V1.1`

- Kiến trúc chính: Windows-first PyQt desktop app cho Veo/Grok/Gemini automation.
- Entrypoint: `run_veo_4.0.py` bootstrap runtime rồi gọi `qt_ui.ui.main()`.
- Pattern đáng tái sử dụng:
  - `runtime_paths.py`: tách install dir, bundle dir, user data dir; hỗ trợ app frozen/PyInstaller.
  - `runtime_bootstrap.py`: resolve system Chrome hoặc managed Chrome.
  - `License.py`: machine-bound license, encrypted state, heartbeat/offline grace.
  - `SettingsManager`: quản lý settings JSON.
  - QThread workers cho workflow dài.
  - Modular PyQt tabs trong `qt_ui/`.
  - Workflow controller/job service cho pipeline nhiều bước.
  - Prompt planning, render prompt, QC và retry policy trong idea-to-video pipeline.

## Timeline

### 2026-07-02

- Khởi tạo project context cho `Recap`.
- Đọc hiểu hai project tham chiếu:
  - `D:\VibeCoding\auto_YT`
  - `D:\VibeCoding\RUN_VEO_V1.1`
- Xác định các pattern có thể tái sử dụng cho project mới.
- Tạo `PROJECT_LOG.md` để theo dõi tiến độ, quyết định kỹ thuật và next steps.

## Quyết định kỹ thuật

### Chưa quyết định

- `Recap` sẽ là desktop app, web dashboard, worker service hay hybrid.
- Chọn stack chính: Python/PyQt, Next.js, hoặc kết hợp cả hai.
- Có cần database local/Postgres hay chỉ dùng file JSON/SQLite.
- Có cần browser automation persistent profile hay không.
- Có cần license/build EXE như `RUN_VEO_V1.1` hay chỉ dùng nội bộ.

## Việc đã hoàn thành

- [x] Khảo sát project `auto_YT`.
- [x] Khảo sát project `RUN_VEO_V1.1`.
- [x] Lưu context tái sử dụng cho project `Recap`.
- [x] Tạo project log ban đầu.

## Việc tiếp theo

- [ ] Xác định mục tiêu cụ thể của `Recap`.
- [ ] Chọn kiến trúc ban đầu.
- [ ] Scaffold cấu trúc thư mục.
- [ ] Tạo README hướng dẫn chạy project.
- [ ] Tạo config/runtime path chuẩn.
- [ ] Thêm test hoặc smoke check phù hợp sau khi có code đầu tiên.

## Ghi chú cập nhật
### 2026-07-02 — Triển khai full pipeline orchestrator `run.py`

- Đã làm:
  - Tạo `run.py` và package `orchestrator/` để chạy 6 stage bằng một lệnh.
  - Thêm config YAML/JSON một chỗ, `config.example.yaml`, DAG selection, skip/resume/force/dry-run và summary.
  - Thêm validate output sau mỗi stage và test mock cho DAG/skip/force/parallel.
  - Cập nhật README, AGENTS và `.gitignore` cho `runs/`.
- Quyết định:
  - Orchestrator gọi stage qua subprocess để giữ cache/CLI hiện có và tránh coupling nội bộ.
  - GĐ4 chạy song song với GĐ1→GĐ3; GĐ5/GĐ6 chạy sau barrier.
- File chính đã thay đổi:
  - `run.py`
  - `orchestrator/`
  - `config.example.yaml`
  - `pyproject.toml`
  - `tests/`
  - `README.md`
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Triển khai GĐ6 CLI render

- Đã làm:
  - Tạo package `render/` với CLI `python -m render`.
  - Thêm schema `RenderMeta` và helper probe video/audio stream.
  - Thêm frame quantization toàn cục, cache temp clip, cut/normalize, concat và mux voiceover.
  - Cập nhật README và AGENTS cho GĐ6.
- Quyết định:
  - GĐ6 chạy offline bằng ffmpeg/ffprobe, không dùng API và không giữ tiếng gốc.
  - Fit v1 chỉ hỗ trợ `cover`; temp clips được re-encode đồng nhất rồi concat `-c copy`.
- File chính đã thay đổi:
  - `common/schema.py`
  - `common/media.py`
  - `render/`
  - `tests/`
  - `README.md`
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Triển khai GĐ5 CLI match

- Đã làm:
  - Tạo package `match/` với CLI `python -m match`.
  - Thêm schema `EdlPlacement`, `EdlMeta` và `validate_edl` vào `common/schema.py`.
  - Thêm candidate filtering/widening, scoring, greedy fill, timeline assignment và cache `plan.json`.
  - Cập nhật README và AGENTS cho GĐ5.
- Quyết định:
  - GĐ5 chạy thuần JSON/offline, không decode video, không dùng API.
  - Face là soft bonus, không lọc cứng; thiếu footage thì widen trước, repeat sau.
- File chính đã thay đổi:
  - `common/schema.py`
  - `match/`
  - `tests/`
  - `README.md`
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Triển khai GĐ4 CLI shots

- Đã làm:
  - Tạo package `shots/` với CLI `python -m shots`.
  - Thêm schema `Shot`, `ShotsMeta` và `validate_shots` vào `common/schema.py`.
  - Thêm PySceneDetect detection, thumbnail extraction, motion/brightness/face feature computation và cache riêng.
  - Cập nhật README và AGENTS cho GĐ4.
- Quyết định:
  - GĐ4 chạy offline, không dùng API.
  - Face detection v1 dùng OpenCV Haar cascade vì không có DNN model files trong project tham chiếu.
- File chính đã thay đổi:
  - `pyproject.toml`
  - `common/schema.py`
  - `shots/`
  - `tests/`
  - `README.md`
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Triển khai GĐ3 CLI tts

- Đã làm:
  - Tạo package `tts/` với CLI `python -m tts`.
  - Thêm schema `BeatTiming`, `TtsMeta`, manifest cache và validator timing vào `common/schema.py`.
  - Thêm media helpers cho normalize, silence và concat vào `common/media.py`.
  - Thêm provider adapters AI33.PRO và Genmax theo pattern từ `auto_YT`.
  - Thêm cache theo hash narration để tránh render lại beat không đổi.
  - Cập nhật README và AGENTS cho GĐ3.
- Quyết định:
  - GĐ3 dùng AI33 primary + Genmax fallback theo yêu cầu user, thay vì ElevenLabs mặc định trong spec ban đầu.
  - `beats_timing.json` được dựng từ ffprobe duration của audio thật, không dùng ước lượng.
- File chính đã thay đổi:
  - `common/schema.py`
  - `common/media.py`
  - `tts/`
  - `tests/`
  - `README.md`
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Triển khai GĐ2 CLI review

- Đã làm:
  - Tạo package `review/` với CLI `python -m review`.
  - Thêm schema `ReviewBeat`, `ReviewMeta` và validator review script vào `common/schema.py`.
  - Thêm flow outline → narration → QA, cache riêng từng lượt và regenerate beat bị QA flag.
  - Thêm Playwright ChatGPT adapter local dùng persistent profile theo pattern từ `auto_YT`.
  - Cập nhật README, AGENTS và dependency `playwright`.
- Quyết định:
  - GĐ2 là tác vụ LLM nặng nên dùng Playwright browser automation, không dùng paid API cho đường chạy chính.
  - GĐ2 v1 chạy CLI local, chưa dùng DB/job queue.
- File chính đã thay đổi:
  - `pyproject.toml`
  - `README.md`
  - `common/schema.py`
  - `review/`
  - `tests/`
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Thêm nguyên tắc chi phí API

- Đã làm:
  - Ghi rõ nguyên tắc: việc nhẹ/ít tốn chi phí có thể dùng API; việc nặng/nhiều request bắt buộc ưu tiên Playwright worker theo pattern từ `D:\VibeCoding\auto_YT`.
- Quyết định:
  - Không scale tác vụ nặng bằng paid API nếu chưa có lý do kỹ thuật rõ ràng.
  - Khi chi phí GĐ1 tăng cao vì phim dài hoặc nhiều vision/translation request, cần refactor sang worker/browser automation trước khi scale.
- File chính đã thay đổi:
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Triển khai GĐ1 CLI ingest

- Đã làm:
  - Tạo Python package cho GĐ1 với CLI `python -m ingest`.
  - Thêm schema Pydantic cho `film_map.json` và metadata.
  - Thêm cache/resume cho audio, transcript, translation, frame và vision artifacts.
  - Thêm ffmpeg/ffprobe helpers, OpenAI translate/vision client, gap detection và film map builder.
  - Thêm README hướng dẫn cài đặt, chạy CLI và test.
- Quyết định:
  - GĐ1 dùng package `ingest/` thay vì `stage1_ingest/` để CLI ngắn và đúng kế hoạch đã duyệt.
  - Provider v1 là OpenAI, API key qua `OPENAI_API_KEY`.
  - Test ban đầu dùng mock/unit; chưa bắt buộc clip thật.
- File chính đã thay đổi:
  - `pyproject.toml`
  - `README.md`
  - `common/schema.py`
  - `common/media.py`
  - `ingest/`
  - `tests/`
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Thêm quy tắc cập nhật source of truth

- Đã làm:
  - Thêm quy tắc yêu cầu cập nhật `AGENTS.md` khi code thực tế thay đổi khác với công nghệ, kỹ thuật, kiến trúc, data contract hoặc quyết định đã ghi trong file.
- Quyết định:
  - `AGENTS.md` là tài liệu sống và phải luôn khớp với trạng thái kỹ thuật hiện tại của project.
- File chính đã thay đổi:
  - `AGENTS.md`
  - `PROJECT_LOG.md`
### 2026-07-02 — Chuẩn hóa file context cho AI

- Đã làm:
  - Đổi `AGENTS.md` guideline chung thành `CODING_GUIDELINES.md`.
  - Đổi `AGENTS (1).md` project context thành `AGENTS.md`.
  - Thêm header xác nhận `AGENTS.md` là source of truth cho coding agents.
  - Thêm cross-reference từ `CODING_GUIDELINES.md` về `AGENTS.md`.
- Quyết định:
  - `AGENTS.md` luôn là file AI đọc trước để hiểu project, pipeline và data contract.
  - `CODING_GUIDELINES.md` chỉ chứa nguyên tắc coding/communication chung.
- File chính đã thay đổi:
  - `AGENTS.md`
  - `CODING_GUIDELINES.md`
  - `PROJECT_LOG.md`
- Next steps:
  - Tạo `README.md` khi bắt đầu scaffold code.
  - Tạo cấu trúc repo theo contract trong `AGENTS.md`.

Khi hoàn thành một mốc mới, thêm entry theo mẫu:

```md
### YYYY-MM-DD

- Đã làm:
  - ...
- Quyết định:
  - ...
- File chính đã thay đổi:
  - `path/to/file`
- Next steps:
  - ...
```









### 2026-07-02 — Thêm transcript correction theo glossary/tên nhân vật

- Đã làm:
  - Thêm `ingest/correction.py` với glossary JSON/YAML/TXT, replacement deterministic và OpenAI correction adapter mockable.
  - Thêm CLI GĐ1: `--transcript-correction off|glossary|openai`, `--glossary`, `--correction-model`.
  - Thêm cache `transcript_corrected.json` và meta fields cho correction mode/model/warnings.
  - Cho orchestrator/config truyền các option correction xuống GĐ1.
  - Thêm tests cho glossary correction, OpenAI mock correction và orchestrator command.
- Quyết định:
  - Correction chạy sau alignment/QC và trước translation để giữ timecode ổn định nhưng giảm lỗi tên/entity trong `film_map.json`.
  - `glossary` là mặc định khuyến nghị vì gần như không tốn chi phí API; `openai` chỉ dành cho pass nhẹ.

### 2026-07-02 — Smoke test transcript glossary trên audio thật

- Đã làm:
  - Tạo `glossary.example.yaml` với các alias đã quan sát từ audio mẫu: `문지현/문준현 -> 황준현`, các biến thể `최성 FC`.
  - Chạy smoke GĐ1 với `openai-gpt4o-hybrid + whisperx + --transcript-correction glossary` trên `test-audio-recap.MP3`.
  - Output hợp lệ tại `runs/test-audio-ingest-corrected-v2/film_map.json` với `timecode_quality=strict`, `approximate_timecodes=false`, `speech_count=24`.
- Nhận xét chất lượng:
  - Glossary pass sửa được các lỗi nằm trong replacement list mà không đổi timecode/id.
  - Vẫn cần bổ sung glossary theo phim thật vì ASR có thể sinh alias mới như `최송F15`; đã thêm alias này vào glossary mẫu.

### 2026-07-02 — Smoke test MP4 thật `test-recap.mp4`

- Đã làm:
  - Test `C:\Users\HulkBeoti\Downloads\test-recap.mp4` duration `402.217s`, H.264 1080p30 + AAC audio.
  - GĐ1 pass với `openai-gpt4o-hybrid + whisperx + glossary`, sinh `runs/test-recap-video/film_map.json`.
  - GĐ4 ban đầu lỗi do PySceneDetect runtime không expose `VideoManager` ở root; đã thêm compatibility path `open_video` + fallback legacy.
  - GĐ4 pass sau patch, sinh `169` shots, `167` usable thumbnails/features.
- Cần theo dõi:
  - Segment đầu bị warning non-Korean CJK/Japanese; cần thêm policy skip/filter intro nếu phim thật có opening song/credit.
  - OpenCV runtime hiện là `cv2 5.0.0` và không có `CascadeClassifier`, nên face detection fallback về zero face metrics; nếu cần face bonus thật, cài OpenCV 4.x headless đúng constraint.

### 2026-07-02 — Thêm filter intro non-Korean cho GĐ1

- Đã làm:
  - Thêm `--drop-non-korean-intro-s` mặc định `30s` để bỏ segment CJK/Japanese không phải Korean trong intro/opening/credit.
  - Cho orchestrator/config truyền option này xuống GĐ1.
  - Thêm unit tests cho filter và command wiring.
- Lý do:
  - Smoke test `test-recap.mp4` phát hiện segment đầu là Japanese/opening song, gây warning và làm bẩn `film_map`/review.

### 2026-07-02 — Validate lại GĐ1 sau intro language filter

- Đã chạy lại `test-recap.mp4` với `--drop-non-korean-intro-s 30`.
- Kết quả: segment Japanese/opening gần `8.529s` được drop khỏi speech; `film_map` bắt đầu bằng visual gap rồi speech Korean tại `123.564s`.
- Output smoke: `runs/test-recap-video-filtered/film_map.json` với `speech_count=26`, `visual_count=7`, `timecode_quality=strict`.
- Cần cân nhắc tiếp: visual gap đầu phim dài `0–123.564s`; nếu review cần nhiều chi tiết intro/race footage hơn, nên thêm option split long visual gaps hoặc shot-aware visual summaries.

### 2026-07-02 — Thêm split visual gap dài cho GĐ1

- Đã làm:
  - Thêm `--max-visual-gap-s` mặc định `20s` để chia silent/visual gap dài trước vision.
  - Cho orchestrator/config truyền option này xuống GĐ1.
  - Thêm tests cho split gap và command wiring.
- Lý do:
  - Sau khi filter opening song, `test-recap.mp4` có visual gap đầu `0–123.564s`, quá thô cho GĐ2/GĐ5.

### 2026-07-02 — Validate GĐ1 split visual gaps trên `test-recap.mp4`

- Đã chạy lại GĐ1 với `--max-visual-gap-s 20` và `--max-vision-frames 20`.
- Kết quả: đầu phim không còn visual gap `0–123.564s`; đã split thành các visual segments `0–20`, `20–40`, `40–60`, `60–80`, `80–100`, `100–120`, `120–123.564`.
- Output smoke: `runs/test-recap-video-split-visual/film_map.json` với `visual_count=20`, `speech_count=27`, `max_visual_gap_s=20`.
- Ghi chú: vision cap chọn `20/21` split gaps; nếu cần mô tả mọi visual chunk, tăng `--max-vision-frames` tương ứng.

### 2026-07-02 — Smoke test GĐ2 review trên `test-recap.mp4`

- Đã làm:
  - Cài runtime `playwright` + Chromium cho môi trường Python hiện tại.
  - Chạy GĐ2 thật bằng ChatGPT Playwright profile từ `D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1`.
  - Input: `runs/test-recap-video-split-visual/film_map.json`.
  - Output: `runs/test-recap-video-split-visual/review_script.json` và `review_script.meta.json`.
- Kết quả:
  - `beats=7`, `coverage_pct≈0.915`, `n_qa_iterations=1`, `char_budget=1991`, `est_total_chars=2013`.
  - Cache GĐ2 gồm `outline.json`, `narration.json`, `qa.json`, `revisions/narration-1.json`, `revisions/qa-1.json`.
- Cần theo dõi:
  - Tên/entity tiếng Việt/Latin trong narration còn chưa nhất quán (`Choi Seong/Choi Seon`), nên GĐ2 cần nhận glossary canonical mạnh hơn hoặc post-QA consistency check.

### 2026-07-02 — Thêm GĐ2 narration consistency pass

- Đã làm:
  - Thêm `review/consistency.py` để chuẩn hóa alias tên/entity từ glossary trong narration.
  - GĐ2 chạy consistency pass sau narration và sau mỗi regeneration QA, trước khi derive `review_script.json`.
  - Thêm cache `narration_consistent.json` và meta `consistency_warnings`.
  - Thêm unit tests cho alias như `Choi Seon/Sung -> Choi Seong`, `Hwang Junhyun -> Hwang Jun-hyun`.
- Lý do:
  - Smoke GĐ2 trên `test-recap.mp4` phát hiện narration dùng lẫn `Choi Seong/Choi Seon`.

### 2026-07-02 — Validate GĐ2 consistency pass bằng cache smoke

- Đã chạy lại GĐ2 trên cache `test-recap-video-split-visual` với `--max-qa-iterations 0` để không gọi ChatGPT thêm.
- Output kiểm tra: `runs/test-recap-video-split-visual/review_script_consistent.json`.
- Kết quả: narration dùng canonical `Hwang Jun-hyun` và `Choi Seong`; cache hit `outline.json`, `narration.json`, `narration_consistent.json`, `qa.json`.
- Lưu ý: chạy lại với QA iteration cũ có thể trigger regeneration qua ChatGPT và bị timeout streaming; khi chỉ cần validate deterministic consistency, dùng `--max-qa-iterations 0`.

### 2026-07-02 — Thêm GĐ2 per-video ChatGPT session management

- Đã làm:
  - Thêm `review/session.py` và `chat_session_meta.json` để lưu/khôi phục ChatGPT conversation URL cho từng video/run.
  - Thêm CLI GĐ2: `--chat-session-policy auto|new|resume`, `--chat-session-meta`, `--chat-title`.
  - Mở rộng Playwright adapter nhận `initial_url` và expose `current_url` sau run.
  - Cho orchestrator/config truyền các option session xuống GĐ2.
  - Thêm unit tests cho session policy và command wiring.
- Quyết định:
  - Metadata không lưu prompt/nội dung ChatGPT; chỉ lưu URL/profile/title/path để điều hướng đúng conversation.

### 2026-07-02 — Smoke test GĐ3 AI33/VBee TTS thật

- Đã làm:
  - Đối chiếu AI33 docs và `auto_YT`: vá adapter để nhận status `doing`, submit response `task_id|id`, và tải CDN bằng `User-Agent` để tránh HTTP 403.
  - Kiểm tra AI33 `/v1/health-check` OK và `/v1/credits` còn credits.
  - Chạy GĐ3 thật với voice `vbee_hn_female_ngochuyen_full_24k-st`, provider mode `ai33`, concurrency `2`.
  - Output: `runs/test-recap-video-split-visual/voiceover.mp3`, `beats_timing.json`, `tts_meta.json`.
- Kết quả:
  - `7` beat audio, provider `ai33`, total voiceover khoảng `117.48s`, real_ratio khoảng `0.292` so với phim `402.217s`.
  - Rerun không `--force` hit cache đủ `audio/0.mp3` đến `audio/6.mp3`, không cần gọi API lại.
- Lưu ý:
  - Lần chạy đầu trước khi vá downloader đã tạo 3 task VBee nhỏ nhưng không tải được do CDN 403; sau vá đã chạy thành công.

### 2026-07-02 — Smoke test GĐ5/GĐ6 tạo recap đầu tiên

- Đã làm:
  - Chạy GĐ5 match từ `review_script_consistent.json`, `beats_timing.json`, `shots.json` và sinh `edl.json`.
  - Phát hiện GĐ5 chưa hỗ trợ `inter_beat_pause_s` từ GĐ3; đã vá để đọc `tts_meta.json`, infer pause khi thiếu meta, và chèn pause filler placements để EDL kín timeline.
  - Chạy GĐ6 render từ `edl.json`, `voiceover.mp3`, `test-recap.mp4` và sinh `recap.mp4`.
  - Điều chỉnh render duration tolerance thực tế để tránh false negative do ffmpeg rounding nhỏ.
- Kết quả:
  - Output cuối: `runs/test-recap-video-split-visual/recap.mp4`.
  - `edl.meta.json`: `n_placements=52`, `coverage_ok=true`, warning chỉ là `6` pause filler placements.
  - `render.meta.json`: `1920x1080`, `30fps`, H.264 + AAC, `duration_match=true`, no warnings.
  - Full tests sau vá: `105 passed`.

### 2026-07-03 — QA fix: loại intro 2 phút đầu khỏi recap footage

- User QA:
  - Video là đoạn đầu tập 1, khoảng 2 phút đầu là intro/opening chỉ có hình ảnh, không voice/story chính.
  - Recap cũ tại khoảng `12–21s` lấy hình VIU/intro nên không liên quan narration.
  - Voice rõ, nhưng footage các đoạn khác còn khó sát narration.
- Đã làm:
  - Thêm GĐ1 `--drop-visual-before-s` để không đưa visual intro vào `film_map`.
  - Rerun GĐ1 với `--drop-visual-before-s 120`, tái dụng cache ASR/translation cũ để giảm API.
  - Rerun GĐ4 với `--skip-intro 120`, shot library còn `120` shots, `min_src_in` trong EDL mới là `120.4`.
  - Rerun GĐ2/GĐ3/GĐ5/GĐ6 và tạo bản mới: `runs/test-recap-video-no-intro/recap.mp4`.
  - QA frame mới tại `13s` và `21s` không còn logo/footage intro; bắt đầu từ cảnh đường đua/nhân vật.
- Kết quả mới:
  - `recap.mp4` duration khoảng `110.86s`, 1080p30 H.264 + AAC.
  - `render.meta.json`: `duration_match=true`, no warnings.
- Cần theo dõi:
  - Source video có phụ đề Việt lớn sẵn ở một số đoạn; nếu muốn output sạch hơn cần thêm crop/blur subtitle region hoặc chọn source không hard-sub.
  - Footage vẫn có thể chưa sát narration vì GĐ5 scoring còn semantic yếu; bước sau nên cải thiện match bằng keyword/segment-window scoring và face detection runtime.

### 2026-07-03 ? GD5 Phase 1 semantic matching + edl.qa.json

- Da lam:
  - Them `match/semantic.py` de build context tu `review_script.json` + optional `film_map.json` va tinh TF-IDF/cosine offline cho tung cap beat-shot.
  - Them `match/qa.py` de sinh `edl.qa.json`, gom selected shots, semantic score, motion/brightness/face/reuse va warning `low semantic match`.
  - CLI `python -m match` co them `--film-map`, `--output-qa`, `--semantic-mode off|tfidf`, `--w-semantic`, `--min-semantic-score`.
  - Orchestrator/config mac dinh truyen `film_map.json`, bat `semantic_mode: tfidf`, va ghi `edl.qa.json` trong run-dir.
  - Cache GD5 include hash `film_map.json` va config semantic.
- Ghi chu ky thuat:
  - Semantic chi la soft bonus, khong hard filter; khong dung API/ChatGPT/embedding model nang o Phase 1.
  - `edl.qa.json` la artifact debug, khong phai input bat buoc cua GD6.
- Validation:
  - Targeted tests: `pytest tests/test_match_cli.py tests/test_match_scoring.py tests/test_match_semantic.py tests/test_orchestrator_runner.py -q` -> 14 passed.

### 2026-07-03 ? GD5 Phase 2 multilingual embedding

- Da lam:
  - Them adapter semantic `off|tfidf|bge-m3`, default orchestrator dung `BAAI/bge-m3` local embedding; CLI package van default `off`.
  - Them optional deps `semantic-embed` gom `torch` va `sentence-transformers`; thieu deps khi chay `bge-m3` se fail-fast voi huong dan cai.
  - Them cache embedding theo hash `{model, device, text}` trong `--semantic-cache-dir`; rerun chi encode text moi.
  - Mo rong `edl.qa.json` voi `semantic_provider`, `semantic_model`, `semantic_device`, `semantic_cache_hits`, va per-shot `semantic_rank`.
  - Orchestrator/config bat `semantic_mode: bge-m3`, `w_semantic: 0.45`, `min_semantic_score: 0.22`.
- Ghi chu:
  - Phase 2 van offline/local, khong dung API/ChatGPT; `edl.json` khong doi contract.
- Validation:
  - `pytest -q` -> 116 passed.
  - Smoke tren `runs/test-recap-video-no-intro`: `semantic_provider=bge-m3`, `semantic_model=BAAI/bge-m3`, `semantic_device=cuda`, `min_src_in=120.4`, `duration_match=true`.
  - Rerun GD5 voi `--force` xac nhan embedding cache hit 129 entries.

### 2026-07-03 ? GD2 style preset + readability QA

- Da lam:
  - Them style preset `viral-recap-vi` va sample sach `examples/style/viral_recap_vi.cleaned.txt`.
  - Them `review/style.py` de build style guide va check readability/TTS-friendly.
  - Prompt outline/narration/regenerate nhan style guide; khong dung raw `content.text` lam runtime sample.
  - GD2 tu rewrite beat bi loi cau qua dai, thieu dau cau, hoac run-on sentence; giu nguyen beat id/source span.
  - Meta review them style preset/strength/sample path, style QA report, rewrite count va readability warnings.
  - Orchestrator/config bat style preset/readability QA mac dinh.
- Validation:
  - Targeted tests: `pytest tests/test_review_cli.py tests/test_review_style.py tests/test_orchestrator_runner.py -q` -> 12 passed.
  - Full suite: `pytest -q` -> 120 passed.

### 2026-07-04 — E2E phim lẻ thật: DemThanhDoiSanQuy

- Input: `C:\Users\HulkBeoti\Downloads\DemThanhDoiSanQuy.mp4`, duration `5503.456s` (~91m43s).
- Run dir: `runs/dem-thanh-doi-san-quy`.
- Kết quả: pipeline GĐ1→GĐ6 chạy xong, output `runs/dem-thanh-doi-san-quy/recap.mp4`.
- Output final: `1920x1080`, `30fps`, H.264, dung lượng khoảng `515 MB`, duration video `1390.929s`, audio `1390.930s`, `duration_match=true`.
- Counts: `film_map=273`, `review_beats=40`, `shots=1272`, `edl=442`; TTS `24701` chars, `real_ratio=0.2527`.
- Ghi chú runtime:
  - GĐ2 dùng ChatGPT `PROFILE_GPT_1` + session mới từ `auto_YT`; stale session cookie gây modal `expired-session`, cần dùng cookie mới hoặc profile đã login không bị lock.
  - GĐ2 phim dài cần `reply_timeout_s=900`; review mất khoảng `1382s` do narration + QA/regenerate.
  - GĐ3 AI33/VBee chạy ổn với 40 beats, không warning.
  - GĐ4 vẫn warning face detection disabled vì `cv2` thiếu `CascadeClassifier`.
  - GĐ6 pad video-only từ `1389.865s` lên audio `1390.930s`; final sync đạt.
- Bài học:
  - Phim lẻ nên có config/movie preset riêng (`target_ratio` khoảng `0.22–0.28` nếu muốn gọn).
  - Timecode approximate từ ASR chunked vẫn là rủi ro chính; cần alignment/QC tốt hơn nếu footage chưa sát narration.
  - Runtime profile/session ChatGPT cần logic rõ: profile lock, fresh session file, và message lỗi dễ hiểu.


### 2026-07-05 — GĐ0 Video Profile / intro detection plan implemented

- Thêm GĐ0 `python -m preflight` để sinh `video_profile.json` và detect `non_story_ranges` theo từng video.
- Bỏ tư duy cutoff cứng `120s` khỏi default; manual cutoff chỉ còn debug override.
- GĐ1/GĐ4/GĐ5 đọc `video_profile.json`: bỏ visual gap non-story, gắn `Shot.is_story=false`, và hard-exclude trong matching.
- Default classifier là `heuristic` an toàn; `openclip` là optional local classifier qua group `video-profile`.

### 2026-07-05 — Smoke test GĐ0 trên DemThanhDoiSanQuy

- Cài/runtime optional `open-clip-torch` local để chạy `python -m preflight --classifier openclip`.
- Preflight detect intro/opening tự động: `0.0–185.0s`, confidence `0.917`, reasons `opening credits`, `title card`, `intercut_opening_sequence`.
- Cập nhật detector để nhận intro/opening xen kẽ cảnh phim thật: không chỉ dựa prefix liên tục, mà chấp nhận nhiều frame non-story confidence cao rồi kết thúc bằng story run ổn định.
- Smoke artifact `runs/dem-thanh-doi-san-quy`: `shots.meta.json n_non_story=11`, `edl.meta.json n_intro_excluded=11`, `edl.qa.json selected_from_non_story=false`.
- EDL mới có `min_src_in=186.353` và `intro placements=0`; render lại `recap.mp4`, `duration_match=true`.
- Regression: `pytest -q` -> `126 passed`.

### 2026-07-05 — GĐ5 QA Review HTML + GĐ4 profile cache re-apply

- Thêm GĐ5 `edl.review.html` + `edl.review/` để review trực quan từng beat: narration, source window, selected thumbnails, semantic rank/score, motion/brightness/face/reuse, `is_story`, `exclude_reason`, warnings.
- `python -m match` có thêm `--output-review-html`, `--review-asset-dir`, `--review-thumbs-per-beat`, `--no-review-html`; orchestrator mặc định ghi artifact này trong run-dir.
- Tối ưu GĐ4 cache: `detection.json` và `features.json` không còn phụ thuộc `video_profile.json`; profile marking tách riêng vào `profile_marking.json`.
- Thêm `shots/profile.py` và CLI `--profile-only` để debug re-apply `video_profile` từ cache, tránh re-detect/recompute phim dài khi chỉ đổi intro/non-story ranges.
- Cập nhật `README.md`, `AGENTS.md`, `config.example.yaml`; thêm tests cho profile marking và review HTML.

### 2026-07-05 - Movie intro/sync default correction

- Disabled default `micro_beats` in G2/orchestrator/config after real smoke testing showed whole-film splitting can make audio run ahead of visuals.
- Kept G0 preflight as the per-video intro check: hard-exclude only when `video_profile.non_story_ranges` exists with sufficient confidence; uncertain intro keeps footage.
- Next fix direction for the 0:30-1:39 issue is localized G5 opening ordered/diversity fill, not a hard cutoff and not whole-film splitting.

### 2026-07-05 - Movie-first story map and visual intent

- Added G1.5 `storymap` CLI with `story_map.json`, meta, and QA artifacts built from `film_map.json` plus optional `video_profile.json`.
- G2 now accepts `--story-map`, includes story context in movie prompts, and writes backward-compatible `review_script.intent.json` with story section, visual intent, and chronology mode per beat.
- G5 now accepts `--review-intent`/`--story-map` and supports `--opening-ordered-fill` so opening matching prefers source chronology before score.
- Orchestrator DAG now includes `storymap` between `ingest` and `review`; `shots` still runs in parallel with the ingest/story/review/TTS chain.
- Regression suite: `pytest -q` -> 154 passed.

### 2026-07-06 - G5/G6 sync QA report

- Added `edl.sync.qa.json` generation in G5 to inspect beat-level sync without rerendering or changing the required EDL contract.
- Report includes per-beat timing deltas, timeline gaps/overlaps, source-order mismatch, reuse ratio, and placement-outside-timing warnings.
- Orchestrator now treats `edl.sync.qa.json` as a match output so reruns recreate it automatically.

### 2026-07-06 - G5 movie chronological-first mapping

- Added G5 `match_strategy=chronological|hybrid|semantic`; movie defaults now use `chronological` to prioritize source timecode/chronology over semantic/story/intent score.
- Lowered movie default `w_semantic` to `0.15` and added `chronology_weight`, `max_source_drift_s`, and `ordered_fill_by_audio_progress` config wiring.
- Extended `edl.qa.json` and `edl.review.html` with `expected_src_position`, `source_drift_s`, `chronology_score`, plus `high source drift` / `semantic overrode chronology` warnings.
- Rationale: fix perceived audio/visual mismatch caused by selecting semantically related footage from before/after the narration's expected source position, without using global audio delay or hardcoded intro cutoffs.

### 2026-07-06 - Stable movie preset locked

- Added `config.movie.stable.yaml` as the current known-good movie preset after real-video validation.
- Preset locks movie behavior to `storymap`, `hook_mode=setup`, `target_ratio=auto`, G5 `match_strategy=chronological`, `w_semantic=0.15`, and G6 `audio_delay_s=0.0`.
- Guidance: use this preset as the baseline for the next movie smoke test before tuning new per-video parameters.

### 2026-07-06 - Vietnamese source video preset

- Added G1 `source_language=vi` and `translate_mode=none` so Vietnamese source videos skip KO→EN translation and keep transcript text directly in `film_map.json`.
- Added `config.vi.stable.yaml` for Vietnamese movie/video smoke tests, based on the stable movie preset with OpenAI chunked ASR and no translation.
- Fixed G1 force cache cleanup to remove transcript/alignment/chunk artifacts when rerunning with a different language mode.
