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
