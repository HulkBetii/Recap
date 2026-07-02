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
| 3 | TTS (ElevenLabs) | `review_script.json` | `voiceover.mp3` + `beats_timing.json` |
| 4 | Shot library | `film.mp4` | `shots.json` (+ thumbnails) |
| 5 | Auto-match & chọn footage | `review_script.json` + `beats_timing.json` + `shots.json` | `edl.json` |
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
- TTS: **ElevenLabs**, giọng **nữ**, tốc độ **chuẩn**.
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
- Provider TTS v1: AI33.PRO Vivoo V3 primary, Genmax fallback/secondary theo pattern từ `D:\VibeCoding\auto_YT\web_2\lib\pipeline\tts.ts`.
- Env vars: `VIVOO_API_KEY` cho AI33, `GENMAX_API_KEY` cho Genmax.
- Package thực tế:
  - `tts/`: provider adapters, cache manifest, sanitize, timing builder, concat, cost/meta và CLI orchestration.
  - `common/schema.py`: có thêm `BeatTiming`, `TtsMeta`, `TtsManifestEntry`, `validate_beats_timing`.
  - `common/media.py`: có thêm `normalize_audio`, `generate_silence`, `concat_audio`.
- GĐ3 cache theo hash provider/voice/model/speed/narration/normalize để không render lại beat không đổi.
- `beats_timing.json` luôn dựng từ duration đo bằng ffprobe sau khi audio thật đã render/normalize.
- Test tự động dùng mock provider và mock ffprobe/concat; không gọi AI33/Genmax thật.
## 13. GĐ4 IMPLEMENTATION HIỆN TẠI

- GĐ4 là CLI local/offline, chạy bằng `python -m shots`.
- GĐ4 không dùng API. Dependencies: `scenedetect`, `opencv-python-headless`, `numpy`, ffmpeg/ffprobe.
- Shot detection dùng PySceneDetect `AdaptiveDetector` mặc định, có option `--detector content`.
- Feature pass tính sẵn `motion_score`, `brightness`, `face_count`, `face_area`, `is_usable` cho GĐ5.
- Face detection v1 dùng Haar cascade bundled trong OpenCV; có thể tắt bằng `--face-detection off`.
- Package thực tế:
  - `shots/`: detection, thumbnail extraction, feature computation, cache và CLI orchestration.
  - `common/schema.py`: có thêm `Shot`, `ShotsMeta`, `validate_shots`.
- Cache GĐ4 nằm trong `--work-dir`: `detection.json`, `features.json`, `thumbs/`.
- Test tự động dùng mock/frame synthetic; real clip smoke test sẽ chạy khi có video mẫu.
## 14. GĐ5 IMPLEMENTATION HIỆN TẠI

- GĐ5 là CLI local/offline, chạy bằng `python -m match`.
- GĐ5 chỉ đọc `review_script.json`, `beats_timing.json`, `shots.json` và sinh `edl.json` + `edl.meta.json`; không decode video, không dùng API.
- Face là soft bonus, không phải hard filter. Shot `face_count=0` vẫn được chọn nếu motion/brightness tốt.
- Package thực tế:
  - `match/`: candidate filtering/widening, scoring, greedy fill, timeline assignment, cache và CLI orchestration.
  - `common/schema.py`: có thêm `EdlPlacement`, `EdlMeta`, `validate_edl`.
- Fallback thiếu footage: nới cửa sổ nguồn trước, sau đó repeat có kiểm soát; speedfit mặc định tắt.
- Cache GĐ5 nằm trong `--work-dir/plan.json` theo hash của 3 input JSON + config CLI.
- Test tự động dùng JSON fixtures/mock; không dùng video/ffmpeg/API.

## 15. GĐ6 IMPLEMENTATION HIỆN TẠI

- GĐ6 là CLI local/offline, chạy bằng `python -m render`.
- GĐ6 chỉ đọc `edl.json`, `voiceover.mp3`, `film.mp4` và sinh `recap.mp4` + `render.meta.json`; không gọi API, không chọn lại footage, không caption, không nhạc nền, không giữ tiếng gốc.
- Render dùng `ffmpeg/ffprobe`; temp clips luôn video-only, re-encode H.264 `yuv420p`, cùng resolution/fps/codec params rồi concat bằng demuxer `-c copy`.
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
- Không sửa logic stage con; gọi từng stage qua subprocess `python -m ingest/review/tts/shots/match/render`.
- DAG hiện tại: `shots` chạy song song với chuỗi `ingest → review → tts`; `match` chờ `review + tts + shots`; `render` chạy cuối.
- Config chính là YAML/JSON một chỗ; mẫu đầy đủ nằm ở `config.example.yaml`. Dependency mới: `PyYAML`.
- Resume/idempotent: output hợp lệ thì skip; `--force` chạy lại selected stages; `--force-stage <stage>` chạy lại stage đó và downstream selected stages.
- Hỗ trợ `--from`, `--to`, `--only`, `--dry-run`; dry-run chỉ in plan, không gọi subprocess.
- Validate output sau mỗi stage bằng schema chung trước khi chuyển stage kế tiếp.
- `runs/` là artifact output/cache và không commit vào git.
## 17. G?1 ASR/TIMECODE UPDATE HI?N T?I

- G?1 gi? contract `film_map.json` nh?ng meta c? th?m `asr_provider`, `aligner_provider`, `timecode_quality`, `approximate_timecodes`, `asr_warnings`.
- ASR provider hi?n c?: `faster-whisper` default, `openai-gpt4o`, `openai-gpt4o-hybrid`, v? `manual` ?? import transcript Markdown/JSON.
- Cache transcript m?i trong `--work-dir`: `transcript_text.json`, `transcript_aligned.json`, `transcript_quality.json`.
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
- CDN `https://cdn.ai33.pro/...` có thể trả `403` nếu tải không có `User-Agent`; downloader hiện gửi `User-Agent: Mozilla/5.0` và thêm `xi-api-key` cho domain `ai33.pro` khi có `VIVOO_API_KEY`.
- Không commit `VIVOO_API_KEY`; chỉ set env runtime hoặc `.env` gitignored.
