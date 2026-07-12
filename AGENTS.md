# AGENTS.md — Recap Project Source of Truth

> This is the source of truth for coding agents working on `Recap`.
> Read this file before implementing anything. General coding behavior lives in `CODING_GUIDELINES.md`.
> File context cho coding agent. Đọc file này TRƯỚC mọi task để hiểu toàn cảnh dự án, data contract giữa các stage, và quy ước chung. Mỗi task chỉ hiện thực 1 stage, nhưng phải thiết kế cho khớp với các stage khác theo contract dưới đây.

---

## 1. DỰ ÁN LÀ GÌ

Pipeline tự động end-to-end: nhận **1 tập phim** (drama Hàn) → sinh **1 video recap review** (16:9 1080p, clip full-screen, giọng đọc TTS tiếng Việt đè lên, tắt tiếng gốc, không caption, không nhạc nền).

**Cơ chế lõi:** dùng Whisper transcript **có timecode** làm cầu nối. AI viết review gắn kèm timecode nguồn cho mỗi câu → khớp footage tự động, không đoán mò.

---

## 2. TOÀN BỘ PIPELINE (7 giai đoạn)

| GĐ | Tên | Input | Output |
|----|-----|-------|--------|
| 1 | Ingest & Hiểu phim | `film.mp4` | `film_map.json` |
| 2 | Viết review (GPT) + tự duyệt | `film_map.json` | `review_script.json` |
| 3 | TTS (AI33/Genmax/OpenAI fallback) | `review_script.json` | `voiceover.mp3` + `beats_timing.json` |
| 4 | Shot library | `film.mp4` | `shots.json` (+ thumbnails) |
| 5 | Auto-match & chon footage | `review_script.json` + `beats_timing.json` + `shots.json` + optional `film_map.json` | `edl.json` + `edl.qa.json` |
| 6 | Render | `edl.json` + `voiceover.mp3` + `film.mp4` | `recap.mp4` |
| 7 | Output | `recap.mp4` | (giao file) |

**Trạng thái hiện tại:** đã hiện thực GĐ1 → GĐ6 dạng CLI local theo từng package; GĐ7 chỉ là bước giao file output.

---

## 3. DATA CONTRACT GIỮA CÁC STAGE (QUAN TRỌNG NHẤT)

Các stage giao tiếp QUA FILE JSON, không truyền object in-memory. Mỗi stage đọc file input, ghi file output. Đây là interface cố định — không được đổi tùy tiện.

### `film_map.json` (out GĐ1 → in GĐ2)
```json
[{ "id": 0, "type": "speech|visual",
   "tc_start": 3.86, "tc_end": 6.77,
   "ko": "한국어 or null", "en": "English or null",
   "scene_desc": "mô tả hình or null" }]
```
- `type=speech`: có `ko`+`en`, `scene_desc=null`.
- `type=visual`: có `scene_desc`, `ko`/`en`=null.
- Sort theo `tc_start`, không overlap, timecode ∈ [0, duration].

### `review_script.json` (out GĐ2 → in GĐ3, GĐ5)
```json
[{ "beat_id": 0, "narration": "câu review tiếng Việt",
   "src_tc_start": 12.4, "src_tc_end": 45.0,
   "is_hook": false }]
```
- `src_tc_start/end`: khoảng timecode trong PHIM GỐC mà beat này tóm (để GĐ5 lấy footage).
- `is_hook=true` cho (các) beat cold-open mở màn.
- Đã qua vòng tự duyệt (đối chiếu `film_map`).

### `beats_timing.json` (out GĐ3 → in GĐ5)
```json
[{ "beat_id": 0, "audio_path": "audio/0.mp3",
   "tl_start": 0.0, "tl_end": 3.2, "duration": 3.2 }]
```
- `tl_*`: vị trí trên TIMELINE OUTPUT (khớp voiceover).

### `shots.json` (out GĐ4 → in GĐ5)
```json
[{ "src": "film.mp4", "index": 0,
   "tc_start": 0.0, "tc_end": 4.2, "thumb": "shots/film-000.jpg" }]
```

### `edl.json` (out GĐ5 → in GĐ6)
```json
[{ "tl_start": 0.0, "tl_end": 3.2,
   "src": "film.mp4", "src_in": 14.0, "src_out": 17.2 }]
```
- Tile kín timeline; mỗi placement ≤ 5s (cắt vụn).

---

## 4. QUY ƯỚC CHUNG (áp dụng mọi stage)

- **Ngôn ngữ:** Python 3.11+, type hint đầy đủ, `pydantic` cho mọi schema I/O + validate.
- **Timecode:** luôn là giây (float), tính từ đầu phim. Một đơn vị duy nhất, không dùng frame/ms lẫn lộn ở interface.
- **Interface = JSON file.** Không truyền object giữa stage. Mỗi stage là 1 module/package độc lập, single-responsibility.
- **Resume/cache theo bước:** artifact trung gian lưu `--work-dir`; đã có thì skip; `--force` để chạy lại. Idempotent.
- **API key:** đọc từ biến môi trường, KHÔNG hardcode.
- **Gọi LLM/vision/TTS:** retry + backoff; 1 phần tử fail sau retry → log cảnh báo, gán null/placeholder, KHÔNG crash cả run.
- **Media:** dùng `ffmpeg` CLI; kiểm tra tồn tại lúc khởi động, báo lỗi rõ nếu thiếu.
- **Log:** rõ đang ở bước nào, tiến độ %.
- **Kỷ luật phạm vi:** mỗi stage CHỈ làm việc của nó. Không lấn sang stage khác.

---

## 5. QUYẾT ĐỊNH ĐÃ KHÓA (không tự đổi)

- Transcript: **Hàn gốc = chuẩn timecode** + **bản dịch Anh** (dịch per-segment, giữ nguyên timecode). KHÔNG chạy Whisper 2 lượt.
- Hiểu phim: thoại là chính; **vision chỉ chèn vào khe im lặng** > ngưỡng.
- Viết review: **GPT**, ~**1/3** độ dài phim, giọng **kịch tính kiểu recap VN**, có **cold-open hook**, gắn **timecode nguồn** mỗi beat, có **vòng tự duyệt** đối chiếu transcript.
- TTS: **AI33/VBee giọng nữ** là primary; Genmax rồi OpenAI `gpt-4o-mini-tts` là fallback theo key khả dụng; tốc độ chuẩn.
- Footage: chọn **shot động/có mặt người**; cắt vụn **3–5s**.
- Render: **16:9 1080p**, full-screen, **tắt tiếng gốc**, **không caption**, **không nhạc nền**.
- Tự động hóa: **một phát ra video** (QA nằm ở vòng tự duyệt GĐ2).
- Phạm vi: **chỉ ra file video** (không thumbnail/SEO). Xử lý **1 tập/lần**.

---

## 6. CẤU TRÚC REPO (mục tiêu)

```
repo/
  AGENTS.md              # file này
  README.md
  pyproject.toml
  common/
    schema.py            # pydantic models cho MỌI contract ở mục 3
    media.py             # wrapper ffmpeg dùng chung
  ingest/                # 🔨 GĐ1 CLI: python -m ingest
  review/                # GĐ2 CLI: python -m review
  tts/                   # GĐ3 CLI: python -m tts
  shots/                 # GĐ4 CLI: python -m shots
  match/                 # GĐ5 CLI: python -m match
  render/                # GĐ6 CLI: python -m render
  run.py                 # orchestrator chạy cả pipeline: python run.py
  work/                  # artifact trung gian (gitignore)
  out/                   # recap.mp4 (gitignore)
```

Đặt các pydantic model của contract ở `common/schema.py` để mọi stage import chung — tránh mỗi stage định nghĩa lại lệch nhau.

---

## 7. RỦI RO CẦN Ý THỨC KHI CODE

- **AI bịa timecode** (GĐ2): mọi `src_tc` phải validate nằm trong phim; loại/sửa beat sai.
- **KO/EN lệch timecode**: bắt buộc dịch per-segment giữ ranh giới, không transcribe 2 lượt.
- **Whisper sai do nhạc nền**: cân nhắc vocal isolation trước transcribe (flag tùy chọn).
- **Content ID**: tắt tiếng gốc + cắt vụn giảm rủi ro, không xóa bỏ — nằm ngoài phạm vi code nhưng đừng làm gì tăng rủi ro (vd đừng giữ nguyên tiếng gốc).
## 8. QUY TẮC CẬP NHẬT AGENTS.md

- `AGENTS.md` là source of truth sống của project, không phải tài liệu đóng băng.
- Khi trong quá trình code có thay đổi công nghệ, kỹ thuật, kiến trúc, data contract, pipeline stage, dependency, quy ước chạy/test/build hoặc quyết định đã khóa khác với nội dung hiện tại, phải cập nhật lại `AGENTS.md` ngay trong cùng task.
- Nếu thay đổi chỉ là tiến độ hoặc checklist, cập nhật `PROJECT_LOG.md`.
- Nếu thay đổi là nguyên tắc coding/communication chung, cập nhật `CODING_GUIDELINES.md`.
- Không để code thực tế và `AGENTS.md` lệch nhau sau khi hoàn thành task.
## 9. GĐ1 IMPLEMENTATION HIỆN TẠI

- GĐ1 là CLI thuần Python, chạy bằng `python -m ingest`.
- Provider v1: OpenAI cho KO→EN translation và vision; API key đọc từ `OPENAI_API_KEY`.
- Dependencies chính nằm trong `pyproject.toml`: `pydantic`, `openai`, `faster-whisper`, `pytest` cho dev/test.
- Package thực tế:
  - `common/schema.py`: contract/schema dùng chung.
  - `common/media.py`: wrapper `ffmpeg`/`ffprobe`.
  - `ingest/`: orchestrator, cache, transcribe, translate/vision client, gap detection, film map builder.
- Cache GĐ1 nằm trong `--work-dir`: `audio.wav`, `transcript_raw.json`, `translated.json`, `frames/`, `vision.json`.
- Test v1 dùng mock/unit; smoke test clip thật sẽ chạy khi có video mẫu.
## 10. NGUYÊN TẮC CHI PHÍ API VS PLAYWRIGHT WORKER

- Với tác vụ chi phí API thấp, ít request, dữ liệu nhỏ hoặc cần độ ổn định API cao, được phép dùng API trực tiếp.
- Với tác vụ nặng, nhiều request, dữ liệu dài, xử lý hàng loạt hoặc có nguy cơ tốn nhiều chi phí API, bắt buộc ưu tiên kỹ thuật Playwright worker theo pattern từ `D:\VibeCoding\auto_YT`.
- Pattern cần tái dụng từ `auto_YT` khi dùng Playwright worker:
  - Persistent browser profile để giữ phiên đăng nhập.
  - Worker claim job, ghi heartbeat, update status và resume an toàn.
  - Lưu conversation/session URL khi cần tiếp tục phiên làm việc.
  - Retry rõ ràng, log tiến độ, không crash toàn pipeline vì một job lỗi.
- Không tự ý chuyển một tác vụ nặng sang paid API nếu chưa có lý do kỹ thuật rõ ràng và chưa cập nhật quyết định vào `AGENTS.md` + `PROJECT_LOG.md`.
- Riêng GĐ1 hiện tại vẫn dùng OpenAI API cho translation/vision v1 theo plan đã duyệt; nếu chạy phim dài hoặc chi phí tăng cao, phải refactor sang worker/browser automation trước khi scale.
## 11. GĐ2 IMPLEMENTATION HIỆN TẠI

- GĐ2 là CLI local, chạy bằng `python -m review`.
- Runtime chính: ChatGPT qua Playwright persistent browser profile; không dùng paid API cho outline/narration/QA vì đây là tác vụ LLM nặng.
- LLM chỉ được trả segment ids (`from_seg_id`, `to_seg_id`, hook ids). Code tự suy ra `src_tc_start` và `src_tc_end` từ `film_map`; không nhận timecode do LLM viết.
- Package thực tế:
  - `review/`: orchestrator, cache, Playwright adapter, prompt flow, budget, coverage, timecode derivation.
  - `common/schema.py`: có thêm `ReviewBeat`, `ReviewMeta`, `validate_review_script`.
- Cache GĐ2 nằm trong `--work-dir`: `outline.json`, `narration.json`, `qa.json`, `revisions/`.
- `film_map.meta.json` đọc duration theo thứ tự `duration_s` rồi `duration`; nếu thiếu meta thì fallback `max(tc_end)` và ghi warning.
- Test tự động dùng mock LLM; không chạy ChatGPT/Playwright thật trong test.
## 12. GĐ3 IMPLEMENTATION HIỆN TẠI

- GĐ3 là CLI local, chạy bằng `python -m tts`.
- Provider TTS v1: AI33.PRO Vivoo V3 primary, Genmax secondary và OpenAI `gpt-4o-mini-tts` fallback cuối.
- Env vars: `VIVOO_API_KEY` cho AI33, `GENMAX_API_KEY` cho Genmax, `OPENAI_API_KEY` cho OpenAI fallback.
- Package thực tế:
  - `tts/`: provider adapters, cache manifest, sanitize, timing builder, concat, cost/meta và CLI orchestration.
  - `common/schema.py`: có thêm `BeatTiming`, `TtsMeta`, `TtsManifestEntry`, `validate_beats_timing`.
  - `common/media.py`: có thêm `normalize_audio`, `generate_silence`, `concat_audio`.
- GĐ3 cache theo hash provider/voice/model/speed/narration/normalize để không render lại beat không đổi.
- `provider_mode=auto` chỉ dùng provider có key hợp lệ theo thứ tự AI33 → Genmax → OpenAI; Genmax trong auto còn yêu cầu `genmax_voice_id`. Cache key bao gồm toàn bộ provider chain/model/voice và manifest ghi provider thực tế.
- `beats_timing.json` luôn dựng từ duration đo bằng ffprobe sau khi audio thật đã render/normalize.
- Test tự động dùng mock provider và mock ffprobe/concat; không gọi AI33/Genmax thật.
## 13. GĐ4 IMPLEMENTATION HIỆN TẠI

- GĐ4 là CLI local/offline, chạy bằng `python -m shots`.
- GĐ4 không dùng API. Dependencies: `scenedetect`, `opencv-python-headless`, `numpy`, ffmpeg/ffprobe.
- Shot detection dùng PySceneDetect `AdaptiveDetector` mặc định, có option `--detector content`; phim dài có thể dùng `--detector ffmpeg-scene` để detect bằng ffmpeg scene score nhanh hơn.
- GĐ4 có `--max-shot-len` để split scene/shot quá dài thành virtual shot ngắn hơn cho GĐ5; default `0` giữ behavior cũ, preset visual dùng giá trị ngắn hơn để đủ candidate 3-5s.
- Feature pass tính sẵn `motion_score`, `brightness`, `face_count`, `face_area`, `is_usable` cho GĐ5.
- Face detection v1 dùng Haar cascade bundled trong OpenCV; có thể tắt bằng `--face-detection off`.
- Package thực tế:
  - `shots/`: detection, thumbnail extraction, feature computation, cache và CLI orchestration.
  - `common/schema.py`: có thêm `Shot`, `ShotsMeta`, `validate_shots`.
- Cache GĐ4 nằm trong `--work-dir`: `detection.json`, `features.json`, `end_credit_marking.json`, `profile_marking.json`, `thumbs/`.
- `detection.json` và `features.json` không phụ thuộc `video_profile.json`; khi chỉ đổi profile, GĐ4 chỉ re-apply `profile_marking.json` để set `is_story=false` / `exclude_reason`.
- CLI có `--profile-only` để debug re-apply profile từ cache; thiếu cache features thì fail-fast.
- Test tự động dùng mock/frame synthetic; real clip smoke test sẽ chạy khi có video mẫu.
- GĐ4 has `--frame-sampling per-shot|batch`; default `per-shot` keeps legacy behavior, while `batch` opens the video once, samples frames in timeline order, and reuses those frames for features + thumbnails. `config.movie.visual.yaml` enables `batch`.
- `Shot.unusable_reasons` là optional/backward-compatible; GĐ4 ghi `too_dark`, `too_short`, `transition_spike` hoặc `no_frames` để GĐ5 chỉ relax đúng nguyên nhân cho phép.
- `Shot.is_end_credit` và `credit_like_score` là optional/backward-compatible. Visual preset bật tail-only OpenCV heuristic trong 600 giây cuối; classifier chỉ đánh dấu blank/credit-only, không loại cảnh post-credit có vùng story rõ.
- End-credit marking có cache riêng và chỉ sample tail shots; thay config guard không invalidate detection/features toàn phim. `--profile-only` fail-fast nếu guard bật nhưng cache marking chưa có.

## 14. GD5 IMPLEMENTATION HIEN TAI

- GD5 la CLI local/offline, chay bang `python -m match`.
- GD5 doc `review_script.json`, `beats_timing.json`, `shots.json` va optional `film_map.json`; sinh `edl.json` + `edl.meta.json` + `edl.qa.json` + `edl.review.html`; khong decode video, khong dung API.
- Semantic Phase 2 dung `BAAI/bge-m3` local multilingual embedding qua optional deps `semantic-embed`; `tfidf` van la fallback nhe. Semantic la soft bonus, khong hard filter.
- `content_anchors=true` uses narration-only beat-to-segment semantic scores for beats whose source span is at least 4x the audio duration. G5 restricts fill/chronology to the relevant timecode clusters; compact beats and failed anchors keep legacy matching.
- Content anchors require strict timecodes; G5 disables them automatically when sibling `film_map.meta.json` has `approximate_timecodes=true`.
- `opening_intra_beat_align` remains the backward-compatible opt-in flag for sentence-level alignment in `config.movie.visual.yaml`. It still analyzes at most the first 30s of the first eligible opening beat, and now prepares full-beat anchors for non-opening beats whose source/audio ratio is at least 2.5.
- Non-opening alignment runs only with strict timecodes + BGE-M3, no existing content-anchor plan, and baseline drift above `max(18s, 1.5 * max_source_drift_s)`. Same-anchor sentences are coalesced, low-confidence transitions attach to the next strong anchor, dark-only ending shots remain eligible, and source windows stay monotonic without overlap.
- G5 `--hook-min-brightness` replaces only the first hook placement when its shot-average brightness is below the configured threshold and a brighter chronological local fill exists. Stable/default config keeps `0.0`; `config.movie.visual.yaml` uses `0.10`.
- Movie matching mac dinh dung `match_strategy=chronological`: bam source timecode/chronology truoc; semantic/story/intent chi la soft tie-breaker de tranh audio mot noi hinh mot noi. `semantic` strategy chi dung cho debug/experiment.
- `edl.qa.json` la debug artifact tu `match/qa.py`, ghi provider/model/device/cache hits, selected shots, semantic rank/score, motion/brightness/face/reuse, `expected_src_position`, `source_drift_s`, `chronology_score` va warnings `low semantic match`/`high source drift` theo beat.
- `edl.review.html` la QA artifact truc quan tu `match/review_html.py`, dung thumbnails san co trong `shots.json`, hien narration/source span/selected clip metrics/drift/warnings de review nhanh bang browser.
- Face la soft bonus, khong phai hard filter. Shot `face_count=0` van duoc chon neu motion/brightness/semantic tot.
- GD5 co `--min-visual-clip` (default `0.6`) de tranh flash-cut/khung hinh giat do placement qua ngan; pause gap ngan duoc absorb bang source capacity hoac slowdown toi da 10%, co the phan bo qua ca hai placement ke nhau thay vi tao filler clip rieng.
- Sau khi absorb short clip, GD5 split placement dai hon `--max-clip` thanh cac segment lien tuc cung source/shot de giu contract moi placement <= 5s.
- Package thuc te:
  - `match/`: candidate filtering/widening, scoring, semantic adapters, greedy fill, timeline assignment, cache va CLI orchestration.
  - `common/schema.py`: co them `EdlPlacement`, `EdlMeta`, `validate_edl`.
- Fallback thiếu footage tính capacity theo `sum(min(max_clip, source_intersection))`, bỏ candidate ngắn hơn `min_visual_clip`; thử usable rồi dark-only trong window hiện tại trước khi widen đúng tối đa `max_widen` cấp.
- `allow_dark_fallback=true` mặc định cho stable/visual presets; chỉ shot story bị loại duy nhất vì `too_dark` được relax. Non-story, no-frame, transition spike và too-short luôn bị loại cứng.
- `--exclude-end-credits` hard-exclude `is_end_credit=true` trước semantic/visual, anchors, dark fallback, repeat và pause filler. Visual preset bật policy; stable/default tắt. Khi thiếu footage, GĐ5 warning/underfill thay vì dùng credit-only.
- Repeat fallback dùng phần source chưa dùng trong shot trước, sau đó mới chọn span overlap thấp nhất; tránh lặp ngay shot liền trước khi còn alternative cùng chronology tier.
- `edl.meta.json` ghi `algorithm_version` và counters dark/capacity/reuse/end-credit; algorithm version 6 invalidates artifacts created before end-credit exclusion.
- Cache GD5 nam trong `--work-dir/plan.json`; embedding cache nam trong `--semantic-cache-dir` theo hash `{model, device, text}`.
- Test tu dong dung JSON fixtures/mock; khong dung video/ffmpeg/API.


## 15. GĐ6 IMPLEMENTATION HIỆN TẠI

- GĐ6 là CLI local/offline, chạy bằng `python -m render`.
- GĐ6 chỉ đọc `edl.json`, `voiceover.mp3`, `film.mp4` và sinh `recap.mp4` + `render.meta.json`; không gọi API, không chọn lại footage, không caption, không nhạc nền, không giữ tiếng gốc.
- Render dùng `ffmpeg/ffprobe`; temp clips luôn video-only, re-encode H.264 `yuv420p`, cùng resolution/fps/codec params rồi concat bằng demuxer `-c copy`.
- Khi video-only concat ngắn hơn voiceover, GĐ6 tail-pad bằng một freeze-frame clip ngắn rồi concat copy; full-video re-encode chỉ là fallback nếu tail padding fail.
- Frame-lock toàn cục: placement chiếm frame `[round(tl_start*fps), round(tl_end*fps))`; không round duration từng clip độc lập.
- Fit v1 chỉ hỗ trợ `cover`: scale-to-cover + center crop, không letterbox.
- Package thực tế:
  - `render/`: quantize timeline, cache temp clip, cut/normalize, concat/mux và CLI orchestration.
  - `common/schema.py`: có thêm `RenderMeta`.
  - `common/media.py`: có thêm helper probe video stream/audio stream.
- Cache GĐ6 nằm trong `--work-dir/temp_clips/` theo hash source span + speed + render params; `--force` xóa cache GĐ6.
- Test tự động dùng mock ffmpeg/ffprobe; real smoke test chạy thủ công khi có `edl.json`, `voiceover.mp3`, `film.mp4` thật.
## 16. ORCHESTRATOR IMPLEMENTATION HIỆN TẠI

- Orchestrator chạy bằng `python run.py --input film.mp4 --run-dir runs/<name> --config config.yaml`.
- Preset phim lẻ ổn định hiện tại nằm ở `config.movie.stable.yaml`: `content_type=movie`, `hook_mode=setup`, `target_ratio=auto`, GĐ5 `match_strategy=chronological`, `w_semantic=0.15`, render `audio_delay_s=0.0`.
- Khi chạy phim lẻ mới, ưu tiên dùng preset này trước khi tinh chỉnh; không hardcode cutoff intro và không chỉnh global audio delay nếu lỗi thực chất là matching.
- Không sửa logic stage con; gọi từng stage qua subprocess `python -m ingest/review/tts/shots/match/render`.
- DAG hiện tại: `shots` chạy song song với chuỗi `ingest → review → tts`; `match` chờ `review + tts + shots`; `render` chạy cuối.
- Config chính là YAML/JSON một chỗ; mẫu đầy đủ nằm ở `config.example.yaml`. Dependency mới: `PyYAML`.
- Resume/idempotent: output hợp lệ thì skip; `--force` chạy lại selected stages; `--force-stage <stage>` chạy lại stage đó và downstream selected stages.
- Hỗ trợ `--from`, `--to`, `--only`, `--dry-run`; dry-run chỉ in plan, không gọi subprocess.
- Validate output sau mỗi stage bằng schema chung trước khi chuyển stage kế tiếp. GĐ5 output hiện gồm cả `edl.review.html` khi `match.review_html=true`.
- `runs/` là artifact output/cache và không commit vào git.
## 17. G?1 ASR/TIMECODE UPDATE HI?N T?I

- G?1 gi? contract `film_map.json` nh?ng meta c? th?m `asr_provider`, `aligner_provider`, `timecode_quality`, `approximate_timecodes`, `asr_warnings`.
- GĐ1 hỗ trợ nguồn tiếng Việt bằng `--source-language vi --translate-mode none`; transcript Việt được giữ nguyên vào cả `ko` và `en` để không phá contract cũ, và không gọi KO→EN translation.
- Preset tiếng Việt ổn định nằm ở `config.vi.stable.yaml`; dùng cho video đã là recap/phim tiếng Việt trước khi cân nhắc pipeline Korean drama mặc định.
- Preset tiếng Việt mặc định bật `aligner=whisperx` với `source_language=vi`; WhisperX phải dùng align model `vi`, không được hardcode `ko`.
- ASR provider hi?n c?: `faster-whisper` default, `openai-gpt4o`, `openai-gpt4o-hybrid`, v? `manual` ?? import transcript Markdown/JSON.
- Cache transcript m?i trong `--work-dir`: `transcript_text.json`, `transcript_aligned.json`, `transcript_quality.json`.
- Local `faster-whisper` chunks long audio into `work/ingest/local_asr_chunks` to avoid whole-movie FFT memory spikes; it must pass `source_language` (`ko`/`vi`) into Whisper instead of hardcoding Korean.
- Vietnamese/offline ingest with `translate_mode=none` and `max_vision_frames=0` must not require `OPENAI_API_KEY`; only translation, vision, OpenAI ASR, or OpenAI transcript correction require it.
- Manual transcript d?ng `[MM:SS] text` ch? c? timestamp start; end-time ???c suy lu?n n?n lu?n ??nh d?u approximate n?u ch?a align.
- `--aligner whisperx` ch?y WhisperX forced alignment th?t khi runtime `torch`/`whisperx` c? s?n; n?u l?i th? fallback timestamp hi?n t?i v? ghi warning. `qwen3` v?n l? placeholder an to?n.
- Segment qu? d?i ???c split theo c?u/max duration ?? G?2/G?5 c? source windows m?n h?n.
- G?2 ??c meta G?1 v? ghi warning khi timecode approximate; kh?ng ??i contract `review_script.json`.
## 18. WHISPERX ALIGNMENT RUNTIME

- M?y hi?n ?? ???c smoke test v?i RTX 3060 12GB, Torch CUDA v? WhisperX.
- K?t qu? t?t nh?t cho audio th?t l? `openai-gpt4o-hybrid` t?o chunk rough windows r?i `whisperx` refine timestamp trong t?ng chunk.
- Kh?ng d?ng OpenAI full-span m?t m?nh l?m timestamp v? API kh?ng tr? segment timestamp.
- Kh?ng d?ng WhisperX full-span text kh?ng c? rough windows v? test th?c t? b? n?n timeline v? ??u audio.
- Transcript QC sau alignment clamp theo duration, merge segment qu? ng?n, split/flag segment qu? d?i v? c?nh b?o subtitle/credit artifacts.
- Runtime aligner n?n xem l? optional/heavy; automated tests mock WhisperX, kh?ng y?u c?u GPU.


## 19. TRANSCRIPT CORRECTION / GLOSSARY HIỆN TẠI

- GĐ1 có pass sửa transcript sau ASR/alignment/QC và trước KO→EN translation; pass này không được đổi `id`, `tc_start`, `tc_end`.
- CLI: `--transcript-correction off|glossary|openai`, `--glossary path`, `--correction-model gpt-4.1-mini`.
- `glossary` là đường chạy rẻ/deterministic để sửa tên nhân vật/entity bằng replacements; nên dùng trước khi cân nhắc API.
- `openai` chỉ dùng cho correction nhẹ theo glossary/homophone rõ ràng; không được summarize, translate, merge/split segment hoặc thêm tình tiết mới.
- Cache artifact mới: `transcript_corrected.json`; meta GĐ1 ghi `transcript_correction_mode`, `transcript_correction_model`, `transcript_correction_warnings`.
- Nếu sau này đổi sang correction nặng bằng worker/browser hoặc thêm glossary tự động từ phim dài, phải cập nhật file này, README và PROJECT_LOG cùng task.

## 20. GĐ1 INTRO LANGUAGE FILTER

- GĐ1 mặc định dùng `--drop-non-korean-intro-s 30` để bỏ segment non-Korean CJK/Japanese trong intro/opening/credit đầu phim.
- Filter này chỉ chạy trong QC transcript sau alignment, trước glossary correction/translation; không áp dụng ngoài cửa sổ intro để tránh xóa thoại thật.
- Có thể tắt bằng `--drop-non-korean-intro-s 0` nếu phim có thoại tiếng Nhật/Trung thật ở đầu.

## 21. GĐ1 LONG VISUAL GAP SPLITTING

- GĐ1 mặc định dùng `--max-visual-gap-s 20` để chia silent/visual gap dài thành nhiều visual segments nhỏ trước vision.
- Split chỉ thay đổi visual gaps, không thay đổi speech timecodes từ ASR/alignment.
- `--max-vision-frames` vẫn là cap tổng số frame gửi vision; nếu split tạo nhiều gap hơn cap, chọn gap dài nhất nhưng giữ thứ tự timeline.
- Có thể tắt bằng `--max-visual-gap-s 0` khi muốn giữ behavior cũ.

## 22. GĐ2 NARRATION CONSISTENCY PASS

- GĐ2 chạy deterministic consistency pass sau narration và sau mỗi regeneration QA.
- Pass này dùng glossary do outline tạo ra, cộng alias phổ biến, để chuẩn hóa tên/entity trong narration; không gọi API thêm.
- Artifact cache: `narration_consistent.json`.
- Pass chỉ sửa text narration, không đổi beat ids, segment spans hoặc timecode derived từ `film_map`.

## 23. GĐ2 PER-VIDEO CHAT SESSION MANAGEMENT

- Một video/run GĐ2 phải dùng một ChatGPT conversation riêng để giữ ngữ cảnh outline/narration/QA đồng bộ và tránh lẫn video khác.
- CLI: `--chat-session-policy auto|new|resume`, `--chat-session-meta path`, `--chat-title title`.
- Mặc định `auto`: nếu có `work/review/chat_session_meta.json` thì mở lại `chat_url`; nếu chưa có thì bắt đầu từ `https://chatgpt.com/`.
- `new` ép mở chat mới nhưng vẫn ghi đè metadata sau run; `resume` yêu cầu ưu tiên URL cũ và warning nếu meta chưa tồn tại.
- Metadata chỉ lưu URL/profile/title/path, không lưu prompt hoặc nội dung trả lời; artifacts LLM vẫn là `outline.json`, `narration.json`, `qa.json`.

## 24. GĐ3 AI33/VBEE TTS RUNTIME NOTES

- AI33 polling phải coi status `doing` là running, ngoài các status pending/queued/processing/running/in_progress.
- AI33 submit có thể trả `task_id` hoặc `id`; adapter phải chấp nhận cả hai.
- AI33/Genmax HTTP JSON calls retry transient `429/5xx` and network errors with exponential backoff.
- GĐ3 saves `work/tts/manifest.json` after every completed beat so a later provider failure can resume successful audio instead of synthesizing the whole batch again.
- CDN `https://cdn.ai33.pro/...` có thể trả `403` nếu tải không có `User-Agent`; downloader hiện gửi `User-Agent: Mozilla/5.0` và thêm `xi-api-key` cho domain `ai33.pro` khi có `VIVOO_API_KEY`.
- Genmax/AI33 JSON requests cũng phải gửi `User-Agent: Mozilla/5.0`; Genmax Cloudflare có thể trả `403 error 1010` cho Python urllib request thiếu header này.
- Không commit `VIVOO_API_KEY`; chỉ set env runtime hoặc `.env` gitignored.

## 25. EPISODE INTRO / OPENING EXCLUSION

- Không dùng cutoff cứng làm cơ chế mặc định vì mỗi video có intro/opening dài ngắn khác nhau.
- GĐ0 `preflight` phải sinh `video_profile.json` với `non_story_ranges` trước khi GĐ1/GĐ4/GĐ5 xử lý footage.
- GĐ1 nhận `--video-profile` để bỏ visual-only gaps trong non-story ranges nhưng vẫn giữ speech thật.
- GĐ4 nhận `--video-profile` để gắn shot overlap non-story thành `is_story=false` và `exclude_reason` tương ứng.
- GĐ5 mặc định `--exclude-non-story`; shot intro/opening không được dùng cho candidate, repeat fallback hoặc pause filler.
- `--drop-visual-before-s` và `--skip-intro` chỉ còn là debug/override khẩn cấp, không nằm trong default config.

## 26. GĐ2 STYLE PRESET / READABILITY QA HIỆN TẠI

- GĐ2 mặc định dùng style preset `viral-recap-vi` để viết recap tiếng Việt đời thường, drama, TTS-friendly; không dùng raw `content.text` vì file đó thiếu dấu câu/câu quá dài.
- Style sample sạch nằm ở `examples/style/viral_recap_vi.cleaned.txt` và được đưa vào prompt như guide tham khảo tone, không phải contract cứng.
- CLI GĐ2 có `--style-preset`, `--style-strength`, `--style-qa/--no-style-qa`, `--target-sentence-chars`, `--max-sentence-chars`.
- Readability QA deterministic reject câu quá dài, thiếu dấu câu, beat một câu dài/run-on; nếu fail thì chỉ regenerate beat lỗi, giữ nguyên `beat_id` và source span.
- Review meta ghi `style_preset`, `style_strength`, `style_sample_path`, `style_qa_report`, `n_style_rewrites`, `readability_warnings`.

## 27. GĐ2 CHATGPT SESSION RUNTIME NOTES

- GĐ2 vẫn chạy task viết nặng bằng ChatGPT qua Playwright persistent profile, không dùng paid API.
- CLI có `--reply-timeout-s` vì phim lẻ dài có response outline/narration/QA vượt 240s; default runtime hiện là 600s, run phim dài có thể dùng 900s.
- CLI có `--chatgpt-session-file` để restore cookies từ `auto_YT` khi cần; chỉ dùng session file mới capture, không dùng cookie cũ vì có thể bật modal `expired-session`.
- Nếu profile đã login tốt, ưu tiên dùng trực tiếp `--chatgpt-profile-dir` và đảm bảo không có Chrome/Playwright khác đang giữ lock profile.
- Runtime browser profile/cookies là artifact local, không commit; `data/`, `.env`, `runs/` phải luôn gitignored.

## 28. PHIM LẺ VS PHIM BỘ RUNTIME MODE

- Phim lẻ dùng mode single-video: một input phim tạo một recap độc lập; GĐ2 phải kể arc trọn vẹn từ mở đầu, twist, cao trào đến kết.
- Phim bộ nhiều tập cần series memory riêng ở bước sau: glossary/entity bible + episode summaries để mỗi tập vẫn ra một review riêng nhưng giữ tên nhân vật và mạch truyện xuyên suốt.
- Config phim lẻ nên cân nhắc `target_ratio` khoảng `0.22–0.28` nếu muốn video gọn; test thực tế `DemThanhDoiSanQuy` đạt ratio khoảng `0.253`.

## 29. E2E LESSONS TỪ PHIM LẺ DÀI

- `openai-gpt4o-hybrid` chunked ASR phải skip/cache chunk audio quá nhỏ/invalid để không crash ở chunk cuối phim dài.
- `film_map` approximate timecode vẫn là rủi ro chính cho footage matching; phim dài nên ưu tiên alignment/QC tốt hơn khi cần sát hình.
- GĐ4 hiện vẫn có thể mất face feature nếu OpenCV runtime thiếu `CascadeClassifier`; GĐ5 không được hard-filter theo face.
- GĐ6 padding video-only giữ `duration_match=true` bằng tail freeze-frame clip ngắn + concat copy; full-video re-encode chỉ còn là fallback khi tail path fail.


## 30. GĐ0 VIDEO PROFILE / INTRO DETECTION

- Không hardcode intro duration trong default pipeline; `skip_intro` và `drop_visual_before_s` chỉ là debug override thủ công.
- GĐ0 chạy bằng `python -m preflight`, sinh `video_profile.json` trước GĐ1/GĐ4.
- `video_profile.json` chứa `intro` và `non_story_ranges`; chỉ hard-exclude khi confidence đủ cao.
- Default classifier trong config là `heuristic` an toàn/không loại cứng; `openclip` là optional local classifier qua dependency group `video-profile`.
- GĐ1 dùng `video_profile.json` để bỏ visual-only gaps trong non-story ranges nhưng vẫn giữ speech thật.
- GĐ4 gắn shot overlap non-story thành `is_story=false`, `exclude_reason="intro_opening"`; thumbnails/features vẫn giữ để debug.
- GĐ5 mặc định `exclude_non_story=true`, không chọn shot non-story cho candidate, repeat fallback hoặc pause filler.

## Current Movie Intro/Synchronization Rule

- Not every movie has an intro; the pipeline may exclude intro/non-story footage only when G0 `video_profile.json` contains confident `non_story_ranges`.
- If the classifier is `heuristic` or confidence is below threshold, `video_profile.json` must record `uncertain_intro` and downstream stages must keep opening footage.
- `skip_intro` and `drop_visual_before_s` are manual debug overrides only, not default pipeline behavior.
- Movie micro-beats are experimental opt-in (`micro_beats=false` by default) because real smoke testing showed whole-film splitting can make audio run ahead of visuals.
- For localized opening image/voice ordering issues, prefer a G5 ordered/diversity fill inside `opening_guard_s`; do not hardcode a cutoff and do not split the whole film.
- G5 `opening_story_visual_start` may skip early logo/title/credit shots only when `film_map` has a later story visual segment inside the opening source window; this is not a fixed intro cutoff.

## Movie-First Story Structure / Visual Intent

- Stage G1.5 `storymap` runs with `python -m storymap` and writes `story_map.json`, `story_map.meta.json`, and `story_map.qa.json` from `film_map.json` plus optional `video_profile.json`.
- `story_map.json` is optional/backward-compatible and splits movie story into coarse sections such as setup, inciting incident, conflict, reveal, climax, ending, and non_story.
- G2 `review` accepts `--story-map` and writes optional `review_script.intent.json`; the required `review_script.json` contract is unchanged.
- G5 `match` accepts `--review-intent` and `--story-map`; in `opening_guard_s`, `--opening-ordered-fill` prefers source chronology before score to reduce opening voice/image ordering issues.
- Storymap and review-intent are movie-first defaults in orchestrator; episode behavior remains compatible and can opt out by config.

## G5/G6 Sync QA Report

- G5 writes optional `edl.sync.qa.json` next to `edl.json`; it compares `beats_timing.json` against EDL placements per beat.
- Sync QA flags beat start/end/duration deltas, placements outside the beat timing window, source-order mismatch, high reuse, short clips, long clips, and timeline gaps/overlaps.
- Use this report before changing global audio offset; if only a few beats are flagged, fix matching/timing locally instead of delaying the whole voiceover.
- `summary.json` includes `timecode_qa` from `film_map.meta.json`; if `approximate_timecodes=true`, treat footage/narration mismatch as an ASR/alignment risk first, not a render audio-delay problem.


## 31. G?3 TTS TEXT NORMALIZATION / PRONUNCIATION QA

- G?3 kh?ng s?a `review_script.json`; text g?c v?n d?ng cho QA/G?5, c?n text g?i provider n?m trong `tts_script.json`.
- CLI h? tr? `--tts-text-normalization off|basic|vi`, `--tts-pronunciation-lexicon`, `--tts-normalized-script-output`, v? `--tts-normalization-report`.
- Default `vi` x? l? acronym/k? hi?u/s? nh? cho ti?ng Vi?t: `AI`, `A.I.`, `ChatGPT`, `TTS`, `%`, `/`, URL/email/emoji; kh?ng ???c ??i ch? th??ng `ai` th?nh acronym.
- `tts_meta.json` ghi `text_normalization`, `pronunciation_lexicon_path`, `n_text_normalized`, v? `normalization_warnings`.
- Cache G?3 ph?i d?ng `tts_text` ?? normalize ?? ??i lexicon/rule th? render l?i ??ng beat.
- Lexicon m?u kh?ng ch?a secret n?m ? `examples/tts/vi_pronunciation_lexicon.example.yaml`; key API v?n ch? ??c env/.env gitignored.


## VISUAL INDEX V1.1 CORRECTNESS / MATCH HARDENING

- Visual Index v1.1 metadata records film identity, `shots.json` hash, config hash, preprocessing version, SigLIP logit scale/bias, and SHA-256 checksums for every keyframe/pooled embedding sidecar.
- A legacy v1.0 or uncalibrated index may still parse, but G5 must rebuild or fall back to text-only matching instead of using raw cosine silently.
- The index may contain non-story shots as a superset. Every filtered G5 candidate must exist with matching timecodes and finite vectors of the declared dimension.
- G2 deterministic visual intent emits at most two compact VI/EN queries. SigLIP text preprocessing is fixed to 64 tokens; G5 deduplicates query encoding and caches by model/device/query/preprocessing version.
- G5 computes calibrated probability as `sigmoid(cosine * logit_scale + logit_bias)`, combines VI/EN weights per keyframe, then selects the best keyframe for the shot.
- Chronological matching ranks candidates by drift tier before visual/base score. Candidates outside `max_source_drift_s` are used only after candidates inside the drift limit cannot provide enough footage. Opening ordered fill remains strict.
- G5 must not extend `src_in/src_out` outside the owning shot. Short pauses use available source capacity, at most 10 percent slowdown for absorption, or a source-bounded filler with a warning.
- `edl.visual.qa.json`, `edl.qa.json`, and `edl.review.html` expose raw cosine, calibrated probability, combined score, selected keyframe, actual candidate alternatives, and drift tier.
- Orchestrator skip validation includes Visual Index metadata and sidecars. Any invalid upstream artifact forces selected downstream stages to rerun.
- Visual weight calibration runs with `python -m match.calibrate_visual` on a labeled set containing at least two videos; ties choose the lower visual weight and drift/reuse/short-clip metrics may not regress from weight zero.
- The current experimental `config.movie.visual.yaml` weight is `w_visual=0.15`, calibrated on hand-reviewed beats from one 30fps movie and one 23.976fps movie. Stable presets remain visual-off.
- Story-section labels are only fallbacks for deterministic intent. Explicit visible narration cues such as action/reveal/reaction take priority; generic setup/ending labels must not override plain dialogue or a visible fight.
- Sync QA treats short inter-beat pauses absorbed into either the previous or following placement as expected filler and uses a 1ms epsilon at the minimum-clip threshold.
- Local long-video ASR chunks overlap at boundaries, cache validated per-chunk transcripts atomically, and deduplicate repeated boundary segments.
- G6 validates tail-pad duration before mux. Legacy full re-encode padding uses a dynamic `tpad` duration and is validated before continuing.

## 32. COST-AWARE BACKEND POLICY

- Orchestrator c? `quality_mode: low_cost|balanced|max_quality`, `text_llm_backend`, v? `api_budget_guard` ?? ch?n backend theo chi ph?/ch?t l??ng.
- Default `balanced`: text-heavy QA/review d?ng ChatGPT Playwright; ASR/vision theo preset; TTS v?n d?ng provider tr? ph? nh?ng cache theo beat.
- `low_cost` ?u ti?n local-first ASR, t?t OpenAI vision m?c ??nh, v? kh?ng t? fallback sang OpenAI khi `api_budget_guard=block`.
- `cost_policy.json` v? `cost_summary.json` l? artifact b?t bu?c c?a run orchestrator ?? review stage n?o c? th? t?n API/TTS tr??c khi ch?y th?t.
- G?3 pronunciation QA deterministic ch?y tr??c synthesize v? ghi `tts_pronunciation_qa.json`; backend suggestion ch? sinh candidate lexicon, kh?ng t? s?a `review_script.json` v? kh?ng t? g?i TTS l?i.
- Playwright ch? d?ng cho text/QA d?i; kh?ng d?ng thay th? TTS audio th?t ho?c media artifact c?n provider/local runtime ?n ??nh.


## 33. AUTO LOW-OPENAI VIETNAMESE PIPELINE

- `config.vi.low_openai.yaml` l? preset test r? cho video ti?ng Vi?t: local-first ASR (`faster-whisper` + `whisperx`), `translate_mode=none`, `max_vision_frames=0`, v? `api_budget_guard=block`.
- `config.vi.balanced.auto.yaml` v?n auto 100%: ch?y local-first tr??c, n?u G?1 timecode QA warn/fail th? t? rerun G?1 b?ng `openai-gpt4o-hybrid` r?i force rerun downstream selected stages.
- Fallback ch? d?a tr?n `film_map.meta.json`: `timecode_quality != strict`, `approximate_timecodes=true`, ho?c warning alignment/timecode nghi?m tr?ng.
- `fallback_plan.json` ghi trigger/block reason; `fallback_summary.json` ghi k?t qu? sau fallback; `cost_summary.json` c? `openai_fallback_possible` v? `openai_fallback_triggered`.
- N?u `api_budget_guard=block`, fallback OpenAI ph?i b? ch?n r? r?ng thay v? ?m th?m t?n API.

## 34. GĐ4.5 VISUAL INDEX / TIME-ANCHORED VISUAL MATCHING

- GĐ4.5 là optional local/offline stage, chạy bằng `python -m visual_index`, nhận `film.mp4` + `shots.json` và sinh `shot_visual_index.json` + keyframes/vector sidecars trong `visual_index/`.
- Default pipeline vẫn tắt GĐ4.5 (`visual_index.enabled=false`); preset thử nghiệm nằm ở `config.movie.visual.yaml`.
- Optional dependency group: `visual-index` gồm `torch`, `transformers`, `Pillow`, `safetensors`; model mặc định là `google/siglip2-base-patch16-384`.
- `shot_visual_index.json` không thay contract bắt buộc của `shots.json`; vector dài lưu sidecar `.npy` float16 qua `embedding_ref`/`shot_embedding_ref`.
- GĐ2 `review_script.intent.json` có thêm optional visual query/cue fields (`visual_query_vi`, `visual_query_en`, `characters`, `action_cues`, `emotion_cues`, `location_cues`, `object_cues`, `negative_visual_cues`, `preferred_shot_traits`) nhưng `review_script.json` không đổi.
- GĐ5 nhận optional `--visual-index`, `--visual-mode off|rerank`, `--w-visual`, `--visual-cache-dir`; visual score chỉ rerank candidates trong source window/widen hiện có, không semantic search tự do toàn phim.
- GĐ5 luôn ghi `edl.visual.qa.json`; khi visual disabled/missing thì artifact ghi `visual_enabled=false` và matching fallback text-only.
- GĐ5 `edl.qa.json` và `edl.review.html` hiển thị thêm visual score/rank/query để debug hình có khớp narration hay không.
- V1 chưa làm VLM caption top-K, OCR đầy đủ, hoặc face/entity cluster; các hướng này là phase sau và vẫn phải giữ chronology/timecode là prior chính.

## 35. LOCAL EDITABLE PACKAGING

- Setuptools package discovery dùng allowlist cho các runtime package: `common`, `ingest`, `match`, `orchestrator`, `preflight`, `render`, `review`, `shots`, `storymap`, `tts`, `visual_index`; `run.py` được đóng gói như py-module.
- Không package `tests`, `runs`, `work`, `data`, `broll`, `tts_align`, `build`, `dist`, egg-info hoặc cache directories. Runtime/build artifacts phải nằm trong các thư mục gitignored hiện có.
- Extra `movie-visual` là bộ dependency local đầy đủ cho WhisperX + BGE-M3 + SigLIP2. OpenCLIP/video profile vẫn cài riêng bằng extra `video-profile`.
- Packaging v1 chỉ hỗ trợ editable install và wheel/import smoke trong repo; không cung cấp global `recap` console command và không package config/style assets để chạy từ thư mục bất kỳ.

## 36. PRODUCTION KOREAN MOVIE PRESET / TTS RUNTIME

- `config.movie.production.yaml` là preset production cho phim Hàn trên máy CUDA: Faster Whisper + WhisperX, ChatGPT Playwright review, AI33/Genmax/OpenAI TTS, ffmpeg-scene batch shots, End-Credit Guard, SigLIP2 Visual Index, BGE-M3 matching và GĐ6 1080p.
- Production preset bật `orchestrator.runtime_preflight=true`; thiếu optional module trong `movie-visual` hoặc CUDA phải fail-fast trước khi chạy stage nặng.
- TTS production dùng voice AI33 `vbee_hn_female_ngochuyen_full_24k-st`, Genmax `VU16byTywsWv5JpI8rbc`, OpenAI fallback `gpt-4o-mini-tts/coral`, concurrency `1`. `TtsMeta` có diagnostics optional/backward-compatible cho provider usage và fallback count.
- Stable/visual presets cũ vẫn tương thích và không bị tự động chuyển sang production behavior.
