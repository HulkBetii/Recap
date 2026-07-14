# PROJECT_LOG.md

## 2026-07-11 - GĐ5 opening intra-beat alignment

- Added opt-in `opening_intra_beat_align` for `config.movie.visual.yaml`; stable and Vietnamese presets remain disabled.
- The first eligible non-hook opening beat is split into character-weighted sentence timings over real TTS duration. BGE-M3 scores sentence queries against shot contexts, then monotonic DP selects nondecreasing anchors with a light chronology prior.
- Only confident chunks in the first 30 seconds are replaced. Local fill stays inside the current and next anchor window, uses continuous source without repeat, snaps back to an already-correct baseline anchor when possible, and preserves every untouched placement outside the replacement range.
- QA v9 and HTML now expose sentence text/timeline, raw semantic score, anchor score, source window, selected shots, replacement range, and skip reason. Match algorithm version is now `4`.
- Real `Toan-Tri-Doc-Gia` validation replaced only TL `20.332-40.716s`: shots `51-52` cover Yoo Sang-ah/job status, shot `54` covers design/brand, and shots `55-56` introduce Han Myung-oh with a source-continuous handoff into the unchanged baseline.
- Final G5 invariants: all baseline placements outside the replaced interval remain byte-equivalent; no timeline gap/overlap, short clip, source-order mismatch, or shot-bound violation. Final cached G6 rerender encoded only 3 new clips, stayed `1920x1080`/30fps with `duration_match=true`, and black-frame detection found no black interval in TL `20-43s`.
- Contact sheet `runs/toan-tri-doc-gia.visual-v1/qa_frames/beat-1-21-42.after-intra.jpg` confirms Sang-ah in the job/design section and Han Myung-oh at the manager introduction. Playwright loaded the QA HTML and verified anchors `51`, `54`, `56` plus replacement range `20.332-40.716s`.
- Validation: targeted tests `46 passed`; full `python -m pytest -q` -> `283 passed`; compileall and `git diff --check` completed without code errors. `Ngoai-Vong-Phap-Luat` and `Gang-To-Tai-Xuat` selected no intra-beat overlay, with zero sync gaps/overlaps/short-clip warnings.

## 2026-07-11 - G5 content anchors for wide beats

- Added narration-only beat-to-segment semantic scoring and deterministic timecode cluster selection for source spans at least 4x longer than beat audio.
- Candidate fill, dark fallback, repeat ranges, chronology cursor and drift QA now stay inside selected content intervals; compact beats retain legacy behavior.
- Added `content_anchors=true`, QA/HTML diagnostics and `algorithm_version=3` so stale match/render artifacts rerun automatically.
- Approximate or invalid timecode metadata disables anchors automatically and `film_map.meta.json` participates in the match cache key; strict-timecode runs keep anchors enabled.
- Semantic scorers skip narration-to-segment encoding when anchors are disabled, preserving legacy shot scoring and avoiding unnecessary runtime on approximate-timecode runs.
- Real validation: `Toan-Tri-Doc-Gia` beat 27 selects planning shots `1042-1047`; beat 28 selects battle/reward/revive shots `1089-1092,1126-1128`, with no widen/repeat/overlap and max drift `0.917s`. Beat 23 stays at baseline max drift `11.469s`.
- Acceptance: `Ngoai-Vong-Phap-Luat` keeps 4 high-drift beats and `Gang-To-Tai-Xuat` returns to 4 after the approximate-timecode guard; both keep high-repeat, overlap and short-clip counts at 0.
- Root cause found on `Toan-Tri-Doc-Gia` beats 27-28: shot semantic included the full source transcript, causing unrelated later events and repeated subscribe text to steer footage selection.

## 2026-07-11 - GĐ5 hardening beat thiếu footage

- GĐ4 thêm optional `Shot.unusable_reasons` và feature cache schema v2 để phân biệt `too_dark`, `too_short`, `transition_spike`, `no_frames`; legacy `shots.json` vẫn parse được.
- GĐ5 dùng effective candidate capacity theo `sum(min(max_clip, source_intersection))`, loại intersection ngắn hơn `min_visual_clip`, thử dark-only story shots trong từng window trước khi widen và sửa off-by-one `max_widen`.
- Repeat fallback ưu tiên source ranges chưa dùng, sau đó span overlap thấp nhất; không chọn lại ngay shot liền trước khi còn alternative cùng chronology tier.
- Thêm `allow_dark_fallback=true` vào stable/visual presets, QA/HTML diagnostics, `EdlMeta` counters và `algorithm_version=2`; orchestrator invalidates match/render artifacts cũ.
- Regression thật `Toan-Tri-Doc-Gia` beat 23: `widen=0`, repeat ratio `0`, overlapping repeats `0`, max drift `11.469s` (trước đó widen 3+ cấp, repeat khoảng `0.5`, drift trên `90s`).
- Hai run acceptance không regress: `Ngoai-Vong-Phap-Luat` giảm high-drift beats `5 -> 4`, `Gang-To-Tai-Xuat` giữ `4`; cả hai giữ repeat/overlap/short clips bằng `0`.
- Cached rerender `Toan-Tri-Doc-Gia` chỉ encode lại 14 clips, output H.264 1920x1080/30fps dài `1418.443s`, `duration_match=true`; Playwright review HTML và preview 41s quanh beat 23 không thấy black frame, flash hoặc khựng cut.

## 2026-07-08 — Opening story visual start guard

- Added GĐ5 opening guard to avoid selecting early logo/title/credit visuals when `film_map` identifies a later story visual segment inside the opening source window.
- Used for `Gang-To-Tai-Xuat` where source `0–45s` contains Shoebox/opening credits but beat narration starts at the fish market.


## 2026-07-06 — Vietnamese WhisperX alignment preset

- Updated Vietnamese source preset to use `aligner=whisperx` on CUDA for finer timecodes.
- GĐ1 WhisperX alignment now receives `source_language` so Vietnamese runs load the `vi` align model instead of hardcoded Korean.


## 2026-07-06 — Timecode QA in run summary

- Added `summary.json.timecode_qa` from `film_map.meta.json` so runs clearly show strict vs approximate timecodes.
- Orchestrator warnings now explicitly flag `approximate_timecodes=true` because footage matching can feel less precise until forced alignment is enabled.


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


### 2026-07-08 - G?3 Vietnamese TTS text normalization

- Added deterministic Vietnamese TTS text normalization before provider submit, without changing `review_script.json`.
- G?3 now writes `tts_script.json` and `tts_normalization_report.json`; `tts_meta.json` records normalization mode, lexicon path, changed count, and warnings.
- Default `vi` keeps lowercase Vietnamese `ai` untouched while normalizing clear acronyms like `AI`, `A.I.`, `ChatGPT`, `TTS`, plus common symbols/units.
- Orchestrator/config now pass TTS normalization settings; added example pronunciation lexicon and tests.


### 2026-07-08 - Cost-aware backend policy

- Added orchestrator `quality_mode`, `text_llm_backend`, and `api_budget_guard` policy resolution.
- Runs now write `cost_policy.json` and `cost_summary.json`; dry-run prints policy/summary before commands.
- Added deterministic G?3 `tts_pronunciation_qa.json` and optional lexicon candidate output before paid TTS synthesize.
- `low_cost` uses local-first ASR and disables OpenAI vision by default; `balanced` keeps quality preset while text QA stays Playwright-first.


### 2026-07-08 - Auto low-OpenAI Vietnamese fallback presets

- Added `config.vi.low_openai.yaml` for local-first Vietnamese runs with OpenAI blocked by default.
- Added `config.vi.balanced.auto.yaml` for auto 100% local-first runs that fallback to OpenAI hybrid ASR only when G?1 timecode QA fails.
- Orchestrator now writes `fallback_plan.json` / `fallback_summary.json` and updates `cost_summary.json` with fallback possible/triggered flags.
- Fallback forces downstream selected stages after rerunning G?1 so stale story/review/TTS/match/render artifacts are not reused.

### 2026-07-10 - GĐ4.5 visual index + GĐ5 visual rerank v1

- Added optional `python -m visual_index` stage to build `shot_visual_index.json` plus keyframe/vector sidecars from `film.mp4` + `shots.json`.
- Added optional `visual-index` dependency group and `config.movie.visual.yaml`; default/stable configs keep visual index disabled.
- Extended `review_script.intent.json` with optional visual query/cue fields while keeping `review_script.json` unchanged.
- Added GĐ5 `--visual-index`, `--visual-mode off|rerank`, `--w-visual`, `--visual-cache-dir`, and `edl.visual.qa.json`.
- Visual score is a soft rerank inside the existing time-anchored candidate/widen flow; chronology remains the primary prior and missing visual index falls back to text-only matching.
- Targeted validation: `python -m pytest tests/test_visual_index.py tests/test_match_visual.py tests/test_orchestrator_graph.py tests/test_orchestrator_runner.py tests/test_match_scoring.py tests/test_match_semantic.py tests/test_match_cli.py tests/test_match_review_html.py tests/test_review_intent.py -q` -> 40 passed.
- Full validation: `python -m pytest -q` -> 198 passed.

### 2026-07-10 - GĐ1 local ASR long-video smoke fix

- While smoke testing `ngoai-vong-phap-luat.mp4`, fixed Vietnamese/offline ingest so `translate_mode=none` + `max_vision_frames=0` no longer requires `OPENAI_API_KEY`.
- Local `faster-whisper` now passes `source_language` into Whisper and chunks long audio into `work/ingest/local_asr_chunks` to avoid whole-film FFT memory spikes on ~2h videos.
- Targeted validation: `python -m pytest tests/test_cli.py tests/test_ingest_asr_cli.py tests/test_ingest_asr.py tests/test_ingest_whisperx.py -q` -> 29 passed.

### 2026-07-11 - Ngoai vong phap luat visual smoke

- Ran `ngoai-vong-phap-luat.mp4` through Vietnamese local ingest + ChatGPT Playwright review using `runs/_configs/ngoai-vong-phap-luat.vi.visual.yaml`.
- GĐ1 completed with local chunked `faster-whisper` + `whisperx`; GĐ2 Playwright completed `review_script.json` and `review_script.intent.json`.
- GĐ3 real AI33 TTS completed after runtime key was provided; final `voiceover.mp3` and real `beats_timing.json` were regenerated.
- GĐ4 PySceneDetect on the full 1080p 1h55m video was too slow for smoke testing, so generated fixed-window `shots.json` over review source windows and documented it in `shots.smoke.note.txt`.
- GĐ4.5 visual index completed with `google/siglip2-base-patch16-384` on CUDA: 1297 shots/keyframes, sidecar embeddings, no visual-index warnings.
- GĐ5 visual rerank completed with real TTS timing and wrote `edl.json`, `edl.qa.json`, `edl.sync.qa.json`, `edl.visual.qa.json`, and `edl.review.html`; `visual_enabled=true`, 40 beats, 442 placements.
- GĐ6 render completed: `recap.mp4` is 1920x1080 H.264, 1263.267s video / 1263.270s audio, `duration_match=true`.
- Playwright localhost QA opened `edl.review.html` and `recap.preview.html`; browser video metadata loaded at 1920x1080 / 1263.267s and preview screenshot was nonblank.
- Full validation after final smoke: `python -m pytest` -> 200 passed.

### 2026-07-11 - GĐ4 production shot library for Ngoai vong phap luat

- Added GĐ4 `--detector ffmpeg-scene` using ffmpeg scene score for long-video offline shot detection; PySceneDetect remains the default path.
- Added GĐ4 `--max-shot-len` to split very long detected scenes into shorter virtual shots for GĐ5 while keeping the `shots.json` contract unchanged.
- Updated `config.movie.visual.yaml` and the local run config to use `ffmpeg-scene` with `scene_threshold=0.3`, `scene_scale_width=640`, and `max_shot_len=8`.
- Reran `ngoai-vong-phap-luat.mp4` from GĐ4 through GĐ6 with production shots: 1164 shots, 1114 usable, 1164 visual-index entries, 462 EDL placements.
- GĐ5 warnings dropped from the coarse real-shot run's 30 warnings to 10 warnings; large `could not fill` / high-repeat warnings were removed except normal opening-order and pause-filler notes.
- Final render stayed duration-matched at 1920x1080 H.264, 1263.270s video/audio; Playwright loaded `edl.review.html` with 307 QA images and no broken images, and loaded `recap.preview.html` video metadata.
- Full validation: `python -m pytest` -> 203 passed.

### 2026-07-11 - GĐ4 batch frame sampling

- Added optional GĐ4 `--frame-sampling per-shot|batch`; default stays `per-shot`, while `batch` opens the video once, samples frames in timeline order, and reuses sampled frames for feature computation plus thumbnails.
- Updated GĐ4 feature cache key/meta, orchestrator config/command wiring, README/AGENTS, and `config.movie.visual.yaml` to enable `frame_sampling: batch` for long-movie visual runs.
- Smoke on `ngoai-vong-phap-luat.mp4`: `ffmpeg-scene + max_shot_len=8 + frame_sampling=batch` wrote 1164 shots and 1164 thumbnails; full detect+feature run took 422.68s, and cached-detection face-on feature/profile rerun took 211.9s.
- Validation: `python -m pytest tests/test_shots_features.py tests/test_shots_cli.py tests/test_orchestrator_runner.py -q` -> 22 passed; `python -m pytest -q` -> 207 passed.

### 2026-07-11 - GD5 anti-flash visual clip guard

- Added GĐ5 `--min-visual-clip` / `match.min_visual_clip` default `0.6s` to avoid rendered flash cuts from ultra-short EDL placements.
- Short inter-beat pause gaps are now absorbed into the previous placement instead of creating a separate 0.15s pause filler clip; short fragments inside a beat are coalesced into adjacent visuals.
- Added long-placement splitting after coalescing so every final placement stays `<= --max-clip` while preserving continuous source/shot spans.
- `edl.qa.json` and `edl.sync.qa.json` now report placement duration and `short_clip` warnings when clips fall under the configured threshold.
- Reran `runs/ngoai-vong-phap-luat.visual-v2` from GĐ5 through GĐ6: placements changed from 464 original to 397 final, min clip `0.613s`, max clip `5.000s`, no timeline gaps/overlaps, no sync QA warnings, render `duration_match=true`.
- Browser localhost QA loaded `edl.review.html` with 296 images and 0 broken images; contact sheet around TL `47.8-49.6s` no longer shows the old 0.055s/0.15s flash placement.
- Validation: `python -m pytest -q` -> 212 passed.

### 2026-07-11 - Visual Index v1.1 correctness and match hardening

- Added film/shots/config/preprocessing identity, SigLIP calibration parameters, and SHA-256 checksums for keyframe plus pooled embedding sidecars.
- Visual Index validation now allows non-story supersets but requires every G5 candidate to match shot timecodes and have finite vectors with the declared dimension; legacy v1.0 indexes rebuild/fallback.
- Visual queries now use Vietnamese word-boundary intent detection, compact deterministic VI/EN text, fixed 64-token preprocessing, deduplicated query encoding, CUDA FP16, and NumPy matrix scoring.
- Corrected visual scoring to combine query weights on the same keyframe before selecting the shot maximum; QA reports raw cosine, calibrated probability, combined score, drift tier, selected keyframe, and candidate-window alternatives.
- Corrected chronology tier ordering so all inside-drift candidates precede outside-drift candidates, and diversity selection cannot jump tiers. Source cursor starts at the beat anchor rather than the widened-window edge.
- Hardened anti-flash/source bounds: underfilled beats no longer extend the last source clip; source-bounded fillers close timeline gaps and every final placement is validated against its shot.
- Match cache fingerprints actual visual sidecars. Orchestrator invalidation now propagates to selected downstream stages when an upstream artifact is stale or corrupt.
- Added `python -m match.calibrate_visual` for a labeled two-video golden set with NDCG@5/top-1 optimization and non-regression constraints.
- Added local ASR chunk overlap, atomic per-chunk transcript cache, boundary dedupe, and source/model cache validation.
- Hardened G6 fallback: dynamic full-padding duration and pre-mux duration validation for both tail and legacy paths.

### 2026-07-11 - GD6 tail-padding render optimization

- Replaced the normal full-video `tpad` re-encode with a frame-locked tail freeze-frame clip encoded for only `ceil(shortage_s * fps)` frames, then appended with concat demuxer `-c copy`.
- Kept the previous full-video padding path as a fallback when tail extraction/encoding/concat raises an ffmpeg media error.
- Added custom concat list filenames plus unit/CLI coverage for frame rounding, tail ffmpeg commands, tolerance bypass, audio delay, and legacy fallback.
- Validation: `python -m pytest tests/test_render.py tests/test_render_cli.py -q` -> 18 passed; `python -m pytest -q` -> 219 passed.
- Real cached render smoke on `runs/ngoai-vong-phap-luat.visual-v2`: shortage `1.524s` became 46 tail frames; all 397 temp clips were cache hits and concat + tail pad + mux completed in `43.433s`.
- Final output stayed `1920x1080`, H.264/AAC, `30fps`, `1263.270s`, `duration_match=true`; visual checks at `1261.3s`, `1261.9s`, `1262.5s`, and `1263.1s` showed the expected freeze-frame tail with no black frame.

### 2026-07-11 - Visual hardening two-movie acceptance

- Completed Visual Index v1.1 real validation on `ngoai-vong-phap-luat.visual-v2` (30fps source workflow) and `gang-to-tai-xuat-vi-align` (23.976fps source, rendered at 30fps).
- Tightened deterministic intent after visual review: generic `secret`/story-section labels no longer force reveal/location queries, while visible verbs such as `chống trả`, `khống chế`, and `đập tan` correctly classify climax fights as action.
- Hand-labeled four representative beats across both movies and ran `python -m match.calibrate_visual`; `w_visual=0.15` won with NDCG@5 `0.9341` and acceptable top-1 `1.0`, versus NDCG@5 `0.8880` at `0.20`.
- Final G5 invariants: first movie `323` placements, second movie `427`; both have zero source-bound violations, zero timeline gaps/overlaps, minimum clip `0.600s`, and maximum clip `5.000s`.
- Fixed sync QA float/pause accounting so exact-threshold clips and 150ms pauses absorbed into either adjacent beat do not create false warnings. Final `edl.sync.qa.json` warning counts are empty for both movies.
- Final renders: first movie `1263.270s` with tail pad from `1262.660s` (`461.4s` render); second movie `1518.034s` with tail pad from `1516.578s` (`602.5s` render). Both are H.264/AAC, `1920x1080`, `30fps`, `duration_match=true`, with nonblack matching tail frames.
- Final validation: `python -m pytest -q` -> `251 passed`; `python -m compileall -q ...` and `git diff --check` completed without code errors.

### 2026-07-11 - Toan Tri Doc Gia visual E2E

- Ran `Toan-Tri-Doc-Gia.mp4` end to end with Vietnamese local Faster Whisper + WhisperX, ChatGPT Playwright review, AI33 TTS, production ffmpeg-scene shots, SigLIP2 Visual Index, BGE-M3 matching, and G6 render.
- AI33 returned a transient HTTP 502 after 7/29 beats. Added retry/backoff for transient HTTP/network failures and incremental per-beat TTS manifest persistence; resumed the seven completed files and finished all 29 beats with concurrency reduced to 1 for this long run.
- G4 produced 1,187 shots (1,096 usable). G4.5 indexed 2,374 keyframes and wrote 3,561 keyframe/pooled sidecars in 264.7s. G5 produced 353 placements for a 1,418.462s voiceover.
- EDL QA found one 0.15s pause filler where slowing only the previous clip would exceed the 10% limit. G5 now distributes a short pause across both adjacent placements when their combined slowdown capacity is sufficient.
- Final invariants: zero timeline gaps/overlaps, zero shot-bound violations, minimum clip `0.600s`, maximum clip `5.000s`; sync QA only retains the known high-reuse warning for beat 23.
- Final render is H.264/AAC, `1920x1080`, approximately `30fps`, `1418.443s`, `duration_match=true`, with a 21-frame nonblack freeze tail. Cached rerender encoded only 3 changed clips.
- Playwright reviewed the HTML opening, action, warning-heavy beat 23, climax, and ending; thumbnails matched the narrated subject, while beat 23 remains the weakest section because it widened three times and reached repeat ratio `0.500`.
- Validation: targeted TTS/match tests passed; full `python -m pytest -q` -> `254 passed`.

### 2026-07-12 - Ba Mat Lat Keo Korean E2E

- Ran the Korean visual preset on `Ba-Mat-Lat-Keo.mp4`; G0, strict Faster Whisper + WhisperX ingest, 200-frame vision, G2 ChatGPT Playwright review, G4, SigLIP2 Visual Index, BGE-M3 match, and G6 completed.
- G3 exposed a direct socket `TimeoutError` during AI33 submit. Extended the production HTTP retry/backoff path to cover socket timeouts and added a regression test. AI33 still timed out for a tiny live smoke request, so this run used a gitignored OpenAI TTS fallback to produce 23 cached beats and a `2173.204s` voiceover.
- G4 produced 1,332 shots; Visual Index completed in `259.442s`; G5 produced 528 placements in `69.694s` with zero reuse, zero widen, zero timeline gaps/overlaps, and one `0.117s` sync warning at the beat 1/2 boundary.
- G6 completed in `628.209s`; output is H.264/AAC, `1920x1080`, `30fps`, `2173.204s`, `duration_match=true`, with a `1.034s` nonblack freeze tail.
- Playwright loaded all 296 QA thumbnails with zero broken images and sought the rendered MP4 through a range-capable localhost player. Confirmed a black opening interval at `0.30-3.33s` and an ending mismatch: `2092-2120s` uses an unrelated Superman/arrest scene, while the final phone-home source at `7198-7202s` is never selected.
- Validation: targeted TTS provider tests -> `7 passed`; full `python -m pytest -q` -> `284 passed`.
- G5 algorithm v5 generalized the opt-in sentence alignment to high-drift long beats without touching beats that already have content-anchor plans. Beat 22 now follows monotonic election, three-billion-won, action, and phone-home blocks; max QA drift dropped from `46.081s` to `0.019s` with no repeat, overlap, source-order mismatch, or sub-0.6s clip.
- Added visual-preset `hook_min_brightness=0.10`; beat 0 replaces dark shot 9 with brighter chronological shot 10, removing the measured `0.30-3.33s` black opening.
- Final forced G6 rerender completed in `605.936s`: H.264/AAC, `1920x1080`, `30fps`, `2173.204s`, `duration_match=true`, with a nonblack `1.136s` freeze tail.
- Playwright verified visible opening footage at `1.0s`, the election at `2092.3s`, the final action sequence at `2124.3-2144.3s`, and phone-home footage at `2169.3s`; the old Superman scene is gone. Native end-credit frames remain interleaved with the movie's post-credit action source.
- Final HTML audit loaded `296/296` thumbnails with zero broken images and no browser console errors. Full validation: `python -m pytest -q` -> `290 passed`; opening blackdetect reported no black interval.

### 2026-07-12 - GĐ4/GĐ5 end-credit guard

- Added backward-compatible `Shot.is_end_credit` / `credit_like_score` plus a deterministic OpenCV tail classifier. It samples only the final 600 seconds, detects blank or credit-only frames, and preserves post-credit scenes with a substantial story-image region.
- Added `end_credit_marking.json` as a separate GĐ4 cache. Tail sampling now seeks directly to the first requested frame instead of decoding from frame zero; changing guard settings does not invalidate shot detection/features.
- GĐ5 algorithm v6 hard-excludes marked credits before semantic/visual scoring, anchors, dark fallback, repeat, and pause filler. Visual preset enables the guard; stable/default presets remain off.
- Real GĐ4 acceptance marked 24 shots in `Ba-Mat-Lat-Keo`, 40 in `Toan-Tri-Doc-Gia`, 0 in `Ngoai-Vong-Phap-Luat`, and 11 in `Gang-To-Tai-Xuat`; no previously selected shot was falsely removed in the three regression runs.
- `Ba-Mat-Lat-Keo` rebuilt Visual Index in `270.162s`, GĐ5 in `42.824s`, and GĐ6 in `614.427s`. The final EDL has 534 placements, excludes all 24 marked shots, keeps repeat at zero, minimum clip `0.601s`, and beat-22 max drift `8.554s`.
- Playwright verified `2107.3s` now shows the money scene instead of a credit-only frame, while `2124.3s` preserves the action scene with credit overlay. HTML loaded `296/296` thumbnails with no console errors; render is `1920x1080`, `30fps`, `2173.204s`, `duration_match=true`.
- Validation: targeted suites -> `85 passed`; full `python -m pytest -q` -> `298 passed`; compileall and `git diff --check` completed without code errors.

### 2026-07-12 - Local editable packaging hardening

- Replaced setuptools flat-layout auto-discovery with an explicit runtime package allowlist and included `run.py` as a py-module, preventing local artifact/build directories from being treated as packages or dirtying git status.
- Added `movie-visual` as the deduplicated dependency union for WhisperX, BGE-M3, and SigLIP2; OpenCLIP remains opt-in through `video-profile`.
- Updated install guidance and added static packaging regression tests for package discovery, exclusions, and dependency composition.
- Validation: editable metadata dry-run succeeded; wheel build/import and `ingest`/`match`/`visual_index` help smokes passed; wheel contained all 12 required runtime roots and no excluded roots; full `python -m pytest -q` -> `300 passed`; compileall and `git diff --check` passed.

### 2026-07-12 - Production movie preset and resilient TTS runtime

- Added a Korean movie production preset using CUDA Faster Whisper + WhisperX, SigLIP2, BGE-M3, End-Credit Guard, opening intra-beat alignment, and the accepted 1080p render settings.
- Extended TTS provider mode with OpenAI and changed auto selection to AI33 → configured Genmax → OpenAI, skipping providers without usable credentials instead of requiring every key.
- TTS cache/manifest/meta now track the actual provider/model/voice and provider-chain fingerprint; optional diagnostics report provider counts and fallback beats while legacy metadata remains valid.
- Added production-only dependency/CUDA preflight plus cost-summary provider availability without exposing credentials.
- Live smoke succeeded for AI33/VBee (`2.482s`) and OpenAI `gpt-4o-mini-tts/coral` (`2.256s`); both outputs were valid non-empty MP3 files under `work/tts-live-smoke`.
- Validation: targeted TTS/orchestrator suites passed; full `python -m pytest -q` -> `316 passed`; production dependency/CUDA preflight, editable metadata, compileall, and `git diff --check` passed.
- Genmax live integration reused the `auto_YT` contract and voice `VU16byTywsWv5JpI8rbc`. Initial Python submit hit Cloudflare `403 error 1010`; adding `User-Agent: Mozilla/5.0` to JSON requests fixed it, and the rerun produced a valid `3.318s` MP3.

### 2026-07-12 - GĐ0 → GĐ1 and review cache integrity

- Connected orchestrator preflight output to GĐ1 through `--video-profile`; GĐ1 now suppresses only non-story visual gaps while preserving speech.
- Added film/config/cache identity to GĐ0 and selective GĐ1 manifest keys for audio, transcript, correction, translation, and vision. Legacy/missing/corrupt caches rebuild once; changed profile or vision config keeps ASR/translation cache.
- Split aligned transcript from correction output so glossary/model changes reuse ASR. Manifest keys are written atomically only after successful artifact writes.
- Added content-hash integrity to film-map/story-map/review metadata and orchestrator skip validation. Upstream invalidation reruns downstream without clearing stage caches unless the user explicitly forces them.
- Replaced partial GĐ2 style invalidation with one review input manifest covering every generated artifact. ChatGPT `auto` sessions start fresh when core input changes; explicit `resume` warns and continues.
- Validation includes selective invalidation, legacy/corrupt artifact recovery, profile-only vision invalidation, review cache cleanup, session rollover, and orchestrator propagation tests; full `python -m pytest -q` -> `333 passed`, compileall/diff check and production dry-run passed.

### 2026-07-12 - Release candidate gate before v1.0.0

- Added a single Windows PowerShell release gate for secret/history scanning, tests, compileall, editable metadata, wheel build/content/install/import, CLI help, production dry-run, and optional real-media cache smoke.
- Added Windows/Python 3.11 GitHub Actions with read-only repository permission, full history checkout, no secrets, no browser install, and no GPU extras.
- Added a no-API 30-second GĐ0/GĐ1 smoke using manual transcript. Subprocesses remove OpenAI/AI33/Genmax keys and assert unchanged reuse, profile-only vision invalidation, glossary-only downstream invalidation, and full rebuild after film identity change.
- The first real-media smoke exposed GĐ0 seeking a frame at exact EOF when clip duration equaled `max_intro_s`; frame sampling now stops before EOF and has regression coverage.
- Release artifacts are written under `work/release-gate`; project version remains `0.1.0` until CI and a clean local media gate pass for the release commit.
- Validation: full `python -m pytest -q` -> `343 passed`; CI-mode gate passed wheel/import/CLI/dry-run checks; `Ba-Mat-Lat-Keo.mp4` media gate passed all 33 cache assertions on a 30-second clip with no API environment available.
- The first GitHub Actions run exposed a test-only dependency leak: semantic device coverage imported Torch even though the offline CI contract intentionally excludes GPU extras. The test now mocks both missing-Torch and CUDA-unavailable states; the no-Torch suite passes with `344 passed`.

### 2026-07-12 - v1.0.0 release

- Bumped the package version from `0.1.0` to `1.0.0` and added `RELEASE_NOTES.md`.
- Confirmed the GitHub Release Gate passed for commit `3327fa8`, with `main` synchronized to `origin/main` before the release commit.
- Re-ran the clean local media gate on commit `3327fa8`: secret scan found zero issues, all `344` tests passed, packaging/CLI/dry-run checks passed, and media smoke passed all `33` cache assertions without `-AllowDirty`.

### 2026-07-13 - Review browser timeout hardening and opt-in API fallback

- Fixed two ChatGPT Playwright races: the client now waits for a new assistant message before treating streaming as complete, and text-stability polling has a bounded deadline.
- Added regression coverage for delayed assistant creation and stable-text collection.
- Added opt-in `review.openai_fallback_model`: Playwright remains primary, but one proven browser failure activates an OpenAI circuit breaker for the remaining GĐ2 requests.
- The fallback reads `OPENAI_API_KEY`, retries transient failures, logs model/token usage, and writes `work/review/openai_usage.json`.

### 2026-07-13 - Toan Tri Doc Gia Playwright-first E2E rerun

- Completed the E2E rerun for `Toan-Tri-Doc-Gia.mp4`; Playwright remained the primary GĐ2 backend, while the opt-in OpenAI circuit breaker handled only the remaining revision/QA calls after repeated browser timeouts.
- OpenAI fallback usage: `gpt-4.1-mini`, 18 requests, 140,132 input tokens, and 7,128 output tokens. TTS remained AI33; OpenAI TTS was not used.
- Final GĐ2 QA passed with zero issues at 24,246 characters versus the 24,392-character target.
- Hardened GĐ5 intra-beat splicing so replacement boundaries preserve the minimum visual clip length instead of leaving tiny baseline fragments; regression coverage was added.
- Final EDL has 391 placements, no timeline gaps/overlaps, and 52.641s maximum source drift. Intra-beat alignment ran on beats 1, 10, 11, 13, 14, and 15; 12 beats still carry high-drift warnings and beat 1 retains a chronology mismatch warning.
- Final render is H.264 1920x1080 at 30fps with AAC stereo, duration 1307.791s, and `duration_match=true`. Playwright QA loaded all 138 EDL thumbnails and four representative 1920x1080 frames without broken images.
- Validation: full `python -m pytest -q` -> `350 passed`; compileall and `git diff --check` passed.

### 2026-07-13 - v1.0.1 patch release

- Bumped the package version from `1.0.0` to `1.0.1` and prepended patch release notes while preserving the historical v1.0.0 notes.
- The patch includes Playwright response-race and bounded-wait fixes, opt-in OpenAI review circuit-breaker/usage reporting, historical fallback reporting across partial reruns, and minimum-length-safe GĐ5 intra-beat splicing.
- Stage JSON contracts remain unchanged; OpenAI review fallback remains disabled by default.
- Tag `v1.0.0` is immutable and remains attached to its original release commit; `v1.0.1` may be tagged only after the clean local media gate and GitHub Release Gate pass for the intended release commit.

### 2026-07-13 - Playwright-first backend policy lock

- Locked GĐ2 to `chatgpt_playwright` as the only primary text backend; legacy direct `openai_api` and `off` configurations are rejected.
- Added bounded Playwright retry/recovery policy: two attempts by default, a 60-second same-response recovery window, and no duplicate prompt submission after submit is confirmed.
- Restricted OpenAI review fallback to classified retry-exhausted browser failures, explicit fallback configuration, an allowing budget guard, and a runtime API key. OpenAI initialization remains lazy.
- Extended fallback reporting to distinguish configured/allowed/blocked/triggered state and record Playwright attempts, failure reason/code, model, and token usage without changing stage JSON contracts.
- Clarified that timestamped ASR, vision, TTS, and media use local or dedicated providers; paid ASR auto-fallback requires a severe classified timecode/alignment failure rather than approximate metadata alone.
- Locked every GĐ2 runtime/preset to `D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1`; direct review execution rejects any other ChatGPT profile path.
- Live canonical-profile smoke completed outline, narration, and QA on a 24-segment film map in one new ChatGPT conversation; output validated with 5 beats and `coverage_pct=0.7917`.
- The smoke process had no `OPENAI_API_KEY` while fallback remained configured. `openai_usage.json` recorded `triggered=false`, `request_count=0`, `playwright_attempts=1`, and no browser error. QA was intentionally audit-only (`max_qa_iterations=0`), so this validates runtime/session routing rather than final narration quality.
- Validation: targeted Playwright/fallback/orchestrator suites passed; full `python -m pytest -q` -> `383 passed`; review CLI help, compileall, production dry-run, live canonical-profile smoke, and `git diff --check` passed.

### 2026-07-13 - Full production G2 QA rewrite run

- Ran G2 against the full `Toan-Tri-Doc-Gia` film map with production QA rewriting enabled and the canonical Playwright profile. The dedicated ChatGPT conversation is `https://chatgpt.com/c/6a545601-2a18-83ec-a32d-178f7e2d354b`.
- Produced 18 validated beats at `runs/toan-tri-doc-gia-gd2-production-qa-20260713/review_script.json`; coverage is `0.8841`, above the required `0.85`, and deterministic opening/readability checks passed with no readability warnings.
- QA rewriting reduced the report from 14 issues to 2, then 3, then 1 after three rewrite iterations. Final model QA still flags beat 0 as an unclear movie opening, although the deterministic opening check passes and the beat names the protagonist, train setting, TLS123 warning, 7 PM deadline, and imminent catastrophe.
- Final narration is 27,055 characters versus a 24,392-character budget (`10.9%` over). Beats 6, 10, and 8 are the only beats above 2,000 characters, at 2,450, 2,402, and 2,200 characters respectively.
- Playwright completed outline, narration, and initial QA. A revision response failed to appear after the primary wait and same-response recovery, so the classified `response_not_started` failure exhausted two Playwright attempts and correctly activated the configured OpenAI circuit breaker.
- OpenAI fallback used `gpt-4.1-mini` for 12 requests: 126,954 input tokens and 3,615 output tokens. Usage and trigger details are recorded in `work/review/openai_usage.json`; no API request occurred before the classified Playwright failure.

### 2026-07-13 - Targeted G2 production cleanup

- Cleaned beat 0 and shortened beats 6, 8, and 10 in a separate preserved run at `runs/toan-tri-doc-gia-gd2-production-qa-cleanup-20260713`.
- The cleanup exposed a resume race: the prompt box could appear before existing conversation history, causing a previously loaded assistant message to be mistaken for the new response. Playwright now waits for resumed history to stabilize before counting assistant messages; regression coverage was added.
- The canonical Playwright request received no new response after the full 900-second wait plus 60-second same-response recovery. Only then did the persisted circuit breaker activate `gpt-4.1-mini` for the remaining cleanup and exact-artifact QA requests.
- Final narration has 18 beats and 24,530 characters versus the 24,392-character budget (`0.57%` over). Beat lengths are 421 for beat 0, 1,465 for beat 6, 1,507 for beat 8, and 1,436 for beat 10.
- Final exact-artifact QA passed with zero issues. Coverage remains `0.8841`; schema, review intents, deterministic opening coherence, and readability all pass.
- Cleanup fallback totals: 6 requests, 108,604 input tokens, and 2,433 output tokens. Full validation: `python -m pytest -q` -> `384 passed`; compileall and `git diff --check` passed.

### 2026-07-13 - Cleanup narration G3-G6 production rerun

- Promoted the QA-passed cleanup narration into `runs/toan-tri-doc-gia-v1-no-openai` and preserved the previous downstream artifacts under `backups/downstream-before-cleanup-20260713-111715`.
- AI33 initially returned `HTTP 429: Task polling temporarily busy` at concurrency 3 and again at concurrency 1. Fixed AI33/Genmax polling so exhausted transient HTTP/network retries continue polling the same provider task until its deadline instead of failing the whole beat; regression coverage was added.
- G3 then completed with AI33 only and no provider fallback. The new 18-beat voiceover is `1324.105s`, with 24,533 normalized TTS characters and no TTS warnings.
- G5 rebuilt with BGE-M3 on CUDA, chronological matching, content anchors disabled, and opening intra-beat alignment enabled. The EDL has 394 placements, zero gaps/overlaps, zero reuse/widen/capacity exhaustion, and clip lengths within `0.600-5.000s`; source-order mismatch warnings improved from five beats to three.
- G6 encoded 334 changed temp clips, reused 60 cached clips, concatenated 394 placements, and tail-padded 37 frames. Final output is H.264/AAC, `1920x1080`, `30fps`, stereo `48kHz`, `1324.092s`, `duration_match=true`, and 633,622,046 bytes.
- ffprobe validation passed; sampled opening and tail frames are both visible/nonblack. Orchestrator resume recognized G3/G5/G6 as valid and refreshed `summary.json` without rerunning them.
- Validation: TTS provider suites -> `16 passed`; full `python -m pytest -q` -> `386 passed`; compileall and `git diff --check` passed.
- Playwright visual QA used a local byte-range video server and sampled 20 timestamps across the opening, ending, cleaned beats 6/8/10, and warning beats 2/11/15. Every seek reached the requested timestamp at `readyState=4`, with no media error or browser console error.
- Opening and ending frames are visible/nonblack. Beats 2, 11, and 15 remain visually coherent with their narration despite G5 source-order warnings; beats 6, 8, and 10 match the cleaned narration. Detailed evidence is in `video.cleanup.qa.json` and `qa-cleanup-browser/`.
- Local listening QA transcribed beats 2/6/8/10/11/15 with Faster Whisper `large-v3` on CUDA and checked long silence with ffmpeg. No beat is truncated, no silence interval exceeds 0.8s, and no leading/trailing silence was detected.
- No-autojunk transcript similarity is `0.9692-0.9885` and word recall is `0.9444-0.9881`. Beat 10 is recognized as `50.000 xu`; foreign character names remain intelligible enough for ASR to map to the intended name, with expected spelling variation. Evidence is in `tts.key_beats.qa.json`; no OpenAI API was used.

### 2026-07-13 - v1.0.2 patch release

- Bumped the package version from `1.0.1` to `1.0.2` and prepended release notes while preserving the historical `v1.0.0` and `v1.0.1` notes.
- The patch locks every browser-suitable review path to Playwright-first with the canonical persistent profile, classified retry/recovery, lazy gated OpenAI fallback, and complete fallback-state reporting.
- Fixed resumed ChatGPT history stabilization so stale assistant messages cannot satisfy a new request, and hardened AI33/Genmax polling so transient request exhaustion does not abandon an active provider task before its deadline.
- Stage JSON contracts remain unchanged. ASR, vision, TTS, and media remain local/provider-first, with paid fallback limited by each stage policy.
- Tags `v1.0.0` and `v1.0.1` are immutable; `v1.0.2` may be tagged only after the clean local media gate and GitHub Release Gate pass for the release commit.

### 2026-07-13 - Reaction remix planning branch

- Created documentation branch `codex/reaction-remix-plan` from clean `main` at `v1.0.2`; this task intentionally adds no runtime code.
- Locked a parallel reaction-remix mode that may reorder complete reaction blocks while preserving each reaction's picture, original audio, speed, burned subtitles, channel logo, mascot, and source branding.
- Locked Japanese editorial replacement to AI33 voice `elevenlabs_QPtBgsg1dxKTQHNpHrHt`; no subtitle masking, replacement subtitle, blur, delogo, drawtext, or visual overlay is allowed.
- Locked output duration to `80–100%` of source, targeting `85–90%`. For the `18:49` reference video, the preferred output is `16:00–16:30` and the hard floor is `15:03`.
- The 09:30–12:30 exploratory POC confirmed that reaction audio can remain sample-aligned while old commentary is replaced, and also showed why visual subtitle editing should be excluded from the product scope.
- Added staged design documents under `docs/reaction-remix/` for scope, analysis, architecture, contracts, editorial planning, audio/TTS, render/QA, examples, and implementation roadmap.
- Existing recap contracts and DAG remain unchanged; implementation will start only after the proposed contracts and acceptance gates are reviewed.

### 2026-07-13 - Reaction Remix MVP implementation

- Created `codex/reaction-remix-mvp` from design commit `917fe40` and implemented the separate `reaction_remix/` R0-R8 pipeline plus `run_reaction.py`; recap `run.py` and existing recap contracts remain unchanged.
- Added `reaction-remix.v1` Pydantic contracts and validators for source, multilingual transcript, safe blocks, editorial plan, Japanese script/audio, stems, audio-aware EDL, render timeline/command manifest and QA. Cross-stage provenance uses raw 64-character SHA-256 file hashes.
- Added Faster Whisper `large-v3` mixed-language analysis with 6-second refinement windows, Lingua, SpeechBrain ECAPA clustering, conservative commentary classification and `reaction_blocks.review.html`.
- Added Playwright-only R3/R4 with the canonical profile, semantic annotations, evidence-bound Japanese writing, duration/retention restoration and no OpenAI text fallback.
- Added strict AI33 R5 with voice `elevenlabs_QPtBgsg1dxKTQHNpHrHt`, `ja_basic`, per-slot atomic cache, loudness/true-peak/JA-ASR validation and selective fit repair.
- Added Demucs stems, audio-aware compose, source-compatible frame/sample-locked H.264 CRF18/AAC renderer, forbidden-filter manifest, deterministic media QA and bounded repair overrides.
- Added strict reaction config, parallel DAG, resume, stage ranges, force-stage, dry-run, summary reporting, TTS-fit repair and QA repair wiring. Added extras `reaction-analysis`, `reaction-audio`, and `reaction-remix` while keeping package version `1.0.2`.
- Real R0-R2 POC on source `09:30-12:30` detected all three expected commentary spans within one second. The 184.202-second clip produced 43 full-coverage blocks with zero gaps, overlaps or reaction preservation violations.
- R6-R8 synthetic FFmpeg smoke produced a fully decodable 5.500-second timeline (`5.5055s` CFR container duration) with reaction correlation `0.999999`, `0ms` drift and frame similarity `0.9970`; all four placement assets were reused on the cache rerun.
- Audit hardening binds `remix_edl.json` to exact plan/audio file hashes, rejects VFR at orchestrator and render boundaries, checks every reaction/mixed/unknown placement, and compares actually decoded frames/samples with `render.timeline.json` using one-frame media tolerance.
- Hardened R1 with silence-midpoint primary regions capped at 30 seconds plus full-timeline 6-second multilingual refinement. On the full authorized 1129.302-second source, R0-R2 now detects 12 segments covering all 11 known narrator blocks; the POC anchors resolve to `579.50-588.96`, `700.72-704.42`, and `742.82-748.18` with no analysis gaps.
- Playwright editorial recovery now reuses matching historical prompt/assistant pairs (including ChatGPT's collapsed `Show more` chrome) so a response that arrives after process timeout can be resumed without resending earlier prompts.
- Added a real synthetic FFmpeg regression: exact 120-frame PCM timeline, only one final AAC priming packet, full decode, reaction correlation/lag/gain and frame-similarity hard gates, click/silence checks, and forbidden-filter manifest validation.
- Full authorized-video audit/render remains a release gate. No PR, tag or release is authorized until POC R0-R8 and full-video hard gates pass.

### 2026-07-13 - Reaction Remix acceptance hardening

- Fixed six independent review findings: FFmpeg commentary limiter auto-leveling, partial ChatGPT recovery JSON, partial-refinement speech loss, unsafe zero-handle commentary boundaries, missing TTS normalization cache identity, and global instead of per-slot leakage repair.
- R1 now retains uncovered fragments of primary ASR turns; R2 caps boundaries without full 120 ms handles below the commentary gate and conservatively merges events whose derived safe spans would be shorter than `min_cut_spacing`.
- Limiter uses `level=false` with latency compensation; the synthetic FFmpeg regression drives a hot TTS input and verifies the encoded commentary peak remains within the `-1.5 dB` ceiling tolerance.
- QA now records `old_narrator_leakage_slot_ids`, so repair switches only measured leaking slots to TTS-only. TTS cache keys include the audio-normalization version and codec headroom.
- The strict POC R1/R2 rerun produced 39 full-coverage blocks with zero gaps/overlaps, but zero replaceable commentary blocks. Two known narrator candidates lack complete 120 ms boundary handles; the final anchor overlaps another speaker/language and remains protected `mixed`/`unknown`.
- The stale R3/R4 fit-repair run was stopped before render. The earlier full-source 12-commentary audit predates hardening and must not be used as acceptance evidence. POC/full acceptance is blocked by conflicting locked constraints rather than an implementation crash.
- Validation after hardening: full suite `472 passed`; focused segment/schema suite `16 passed`; compileall, `git diff --check`, packaging tests, wheel build/install/import smoke, and real POC R1/R2 rerun completed.

### 2026-07-14 - Reaction Remix preservation-first POC acceptance

- Replaced the full-handle-only interpretation with `segment.commentary_boundary_policy=strict_or_word_edge`. Segment v7 records additive cut-point safety mode and real left/right handles; a single Japanese narrator may use a `word_edge` at confidence `0.90` when no word/content overlap exists, while overlap remains protected `mixed/unknown`.
- The `184.201633s` POC now has 39 full-coverage blocks. `block-0003` and `block-0026` are replaceable commentary; narrator overlap remains untouched in protected `block-0038` and `block-0039` and is reported as warning-only audit evidence.
- Preservation-first R3 kept every non-commentary block, excluded only the two commentary cores and retained unique reaction speech at `1.0`. R4 wrote `日米、チップで温度差。` and `米国系なら、試すか。`; R5 synthesized both with AI33 voice `elevenlabs_QPtBgsg1dxKTQHNpHrHt`, model `eleven_multilingual_v2`, speed `1.0`, no fallback, and Japanese ASR similarity `1.0` per slot.
- Renderer v4 uses actual quantized source frame/sample indexes, preserves protected source audio without filters, resamples TTS before sample trimming, pads commentary PCM after limiter latency, and encodes the final AAC timeline once. QA v7 decodes each protected placement once and checks its head, middle and tail, clamps probes to decoded media counts, reports `failed_placement_ids`, and treats protected narrator overlap separately from replaced-commentary leakage.
- POC R0-R8 passed with output `176.609767s` (`0.958785x`), 35 protected placements, minimum reaction correlation `0.9986287014`, maximum lag `0ms`, maximum gain delta `0.0691078578dB`, minimum frame similarity `0.9950234368`, zero replaced-commentary narrator leakage, zero click/silence failures, zero forbidden visual operations and peak increase `0.1dB`.
- Decoded timeline delta is `-1` frame and `-1318` samples, both inside one-frame tolerance (`1471` samples). Output ratio remains above the preferred `0.85–0.90` range because preservation takes priority, but passes the locked `0.80–1.00` hard range and the POC `144–180s` gate.
- Follow-up review hardening requires both commentary boundaries to be `full_handle|word_edge`, forbids multi-core commentary slots, validates preservation of every non-commentary kind, and represents low-handle non-narrator cuts as protected `safety_mode=null` edges instead of merging reaction blocks or mislabeling them as `word_edge`.
- QA now handles identical silent windows, clamps boundary probes to decoded source/output frame counts, localizes boundary failures, and remaps reaction repair IDs after plan/compose changes. Durable duration/bed repairs use immutable accepted requests plus a source/request/QA-bound ledger; stale pending repair files remain audit-only.
- Windows Demucs subprocesses now force UTF-8 stdout/stderr so the authorized Japanese source path works without copying or renaming media. Full-source `no_vocals.wav` was generated and validated at `1129.302494s`, stereo `44.1kHz`.
- Full R0-R2 v5/v7 rerun produced 191 full-coverage blocks and audited all 11 narrator anchors: every anchor has a replaceable core, while overlap portions at anchors 6, 8, and 11 remain protected `unknown/mixed`.
- Validation after final hardening: full suite `512 passed`, focused preservation suite `96 passed`, compileall and `git diff --check` passed. POC R3-R8 is being rerun against the new block hash before full R3-R8; no PR, tag or release is authorized before full acceptance.

### 2026-07-14 - Reaction Remix full-source hard QA pass

- Completed full authorized-video R3-R8 in `work/reaction-remix-full-r02` after the preservation-first POC gate. The source duration is `1129.302494s`; final `reaction_remix.mp4` is `1017.959002s` (`0.901405x`) and decodes successfully.
- R3 planning produced 191 timeline items with unique reaction speech retention `1.0`. R4 wrote 12 Japanese commentary slots through the canonical ChatGPT Playwright session, with deterministic style/evidence QA pass. R5 synthesized all 12 slots through AI33 only, using voice `elevenlabs_QPtBgsg1dxKTQHNpHrHt`, model label `eleven_multilingual_v2`, speed `1.0`; one slot required a fit/similarity repair and then cleared `commentary_fit_requests.json`.
- R6/R7 rendered source-compatible H.264/AAC without visual subtitle processing. Protected reaction/mixed/unknown placements keep source video/audio spans, speed `1.0`, gain `0dB`, and no filters.
- Full R8 QA status is `pass`: 161 protected placements checked, failed placement IDs `[]`, min reaction correlation `0.9975820`, max lag `0ms`, max gain delta `0.0896575dB`, min frame similarity `0.9953462`, old narrator leakage `0`, visual forbidden operations `0`, clicks/silences `0`, full-output peak increase `-0.1dB`, decoded video delta `-1` frame and audio delta `173` samples inside one-frame tolerance.
- Warnings are expected/auditable: protected narrator overlap remains in `block-0093`, `block-0127`, and `block-0186`; output ratio is just above preferred `0.85-0.90` and outside the earlier `960-990s` target, though it passes the hard `0.80-1.00` duration gate and is shorter than the source.
- Fixed Windows Unicode stderr handling for FFmpeg/ffprobe in reaction render and QA so Japanese source paths do not turn subprocess stderr into `None`. Fixed QA visual preservation extraction to prefer exact CFR frame-index reads via OpenCV; timestamp seeking near H.264/concat keyframes had produced false boundary-frame failures even when the quantized output frame matched.
- Validation after the full-source pass: focused reaction write/render/QA suite `28 passed`, full `python -m pytest -q` -> `513 passed`, compileall passed, and `git diff --check` passed with Windows CRLF warnings only.
- User accepted the duration tradeoff: exact target length is not important as long as the output is shorter than the source. No R3 trim pass is required for this acceptance; the next step is release hygiene on a clean worktree, then commit/PR only if the user asks for it.

### 2026-07-14 - Reaction Remix manual drop after listening QA

- Manual listening QA found original commentary still audible around `04:05` in the full output. The offending placement mapped to protected `block-0045` (`unknown`, source `289.92-297.50s`), so the MVP policy was extended to support explicit `plan.manual_drop_block_ids` for user-reported hard segments instead of forcing all non-commentary blocks to remain.
- Added `manual_drop` as an explicit `excluded_blocks` category. It is allowed only for non-commentary source blocks, must come from config/CLI, and still has to satisfy unique reaction speech retention `>=0.90`. Planner cache validation now requires manual-drop IDs in the plan to match config exactly.
- Added a plan fast path that applies manual drops to an existing `remix_plan.json` without resubmitting ChatGPT prompts, plus a write fast path that refreshes script adjacency/plan hash without rewriting Japanese text when commentary slots are unchanged.
- Configured `manual_drop_block_ids: [block-0045]`, reran from `plan`, reused existing script/TTS cache, rerendered and reran full QA. New output is `1010.379002s` (`0.894693x`) from `1129.302494s` source. QA passed: 160 protected placements, failed placement IDs `[]`, min reaction correlation `0.9971666`, max lag `0ms`, max gain delta `0.0931131dB`, min frame similarity `0.9954148`, old narrator leakage `0`, forbidden visual operations `0`, click/silence `0`, decoded video delta `-1` frame and audio delta `627` samples within one-frame tolerance.
