# AGENTS.md — Recap Project Source of Truth

> This is the source of truth for coding agents working on `Recap`.
> Read this file before implementing anything. General coding behavior lives in `CODING_GUIDELINES.md`.
> File context cho coding agent. Đọc file này TRƯỚC mọi task để hiểu toàn cảnh dự án, data contract giữa các stage, và quy ước chung. Mỗi task chỉ hiện thực 1 stage, nhưng phải thiết kế cho khớp với các stage khác theo contract dưới đây.

---

## 1. DỰ ÁN LÀ GÌ

Pipeline tự động end-to-end: nhận **1 tập phim** (drama Hàn) → sinh **1 video recap review** (16:9 1080p, clip full-screen, giọng đọc TTS tiếng Việt đè lên, tắt tiếng gốc, không caption, mặc định không nhạc nền). Anime season enhanced là opt-in riêng ở mục 50.

**Cơ chế lõi:** dùng Whisper transcript **có timecode** làm cầu nối. AI viết review gắn kèm timecode nguồn cho mỗi câu → khớp footage tự động, không đoán mò.

---

## 2. TOÀN BỘ PIPELINE (7 giai đoạn)

| GĐ | Tên | Input | Output |
|----|-----|-------|--------|
| 1 | Ingest & Hiểu phim | `film.mp4` | `film_map.json` |
| 2 | Viết review (GPT) + tự duyệt | `film_map.json` | `review_script.json` |
| 3 | TTS (CapCut / AI33 / Genmax / OpenAI / VieNeu) | `review_script.json` | `voiceover.mp3` + `beats_timing.json` |
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

### Sidecar tùy chọn cho khớp theo câu (GĐ2 → GĐ5, GĐ3 → GĐ5)
- `review_script.sentences.json` (`SentenceRef`): mỗi câu narration (tách bằng `common.text.split_sentences`) có `beat_id`, `idx`, `text`, `from_seg_id`/`to_seg_id` và `src_tc_start`/`src_tc_end` (null với câu bình luận), `grounded`. Segment nằm trong span của beat; timecode do code suy ra từ `film_map`.
- `sentence_timing.json` (`SentenceTimestamp`): `beat_id`, `idx`, `tl_start`, `tl_end`, `method` (`silence` | `proportional`); các câu lát kín cửa sổ audio của beat, khoảng ngắt sau câu thuộc về câu đó.
- Hai file không thay contract bắt buộc; thiếu hoặc không khớp số câu thì GĐ5 dùng khớp theo beat như cũ.
- `sentence_shot_scores.json` (`SentenceShotScores`, stage tùy chọn `shot_pick`): điểm 0..1 của các shot ứng viên trong cửa sổ nguồn của từng câu; GĐ5 đọc qua `--sentence-shot-scores`.
- Đường mùa: `series_review_script.sentences.json` (`SeriesSentenceRef`: thêm `ref_index`, `event_id`, `episode_key`; id segment là của film_map tập đó) và `series_recap/sentence_timing.json`.

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
- Viết review: **LLM qua 9router gateway** (preset dùng `ag/gemini-3.8-flash-high`; ChatGPT Playwright là backend chọn được), ~**1/3** độ dài phim, giọng **kịch tính kiểu recap VN**, có **cold-open hook**, gắn **timecode nguồn** mỗi beat, có **vòng tự duyệt** đối chiếu transcript. Xem mục 52.
- TTS: preset production dùng **VieNeu v3 Turbo clone giọng Ngọc Huyền** (`provider_mode=vieneu`, `voice_id=data/voices/ngochuyen_ref.mp3`, clip gitignored); preset example dùng **CapCut `BV074_streaming`** (`provider_mode=capcut`); `provider_mode=auto` vẫn là AI33/VBee → Genmax → OpenAI `gpt-4o-mini-tts` theo key khả dụng; tốc độ chuẩn.
- Footage: chọn **shot động/có mặt người**; cắt vụn **3–5s**.
- Render mặc định/legacy: **16:9 1080p**, full-screen, **tắt tiếng gốc**, **không caption**, **không nhạc nền**, fps theo nguồn (`render.fps: source`; preset enhanced giữ 30). Chỉ preset anime season enhanced ở mục 50 được phép thêm music/SFX local theo manifest.
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
- Provider: KO→EN translation và vision chạy qua 9router (`translation_provider`/`vision_provider: ninerouter`, key `NINEROUTER_API_KEY`) trong các preset hiện tại; `openai_api` vẫn dùng được với `OPENAI_API_KEY`. Xem mục 52.
- Dependencies chính nằm trong `pyproject.toml`: `pydantic`, `openai`, `faster-whisper`, `pytest` cho dev/test.
- Package thực tế:
  - `common/schema.py`: contract/schema dùng chung.
  - `common/media.py`: wrapper `ffmpeg`/`ffprobe`.
  - `ingest/`: orchestrator, cache, transcribe, translate/vision client, gap detection, film map builder.
- Cache GĐ1 nằm trong `--work-dir`: `audio.wav`, aligned/corrected transcript, translation, frames/vision và `cache_manifest.json`; cache được invalidation chọn lọc theo identity từng pass.
- Test v1 dùng mock/unit; smoke test clip thật sẽ chạy khi có video mẫu.
## 10. NGUYÊN TẮC BACKEND TEXT VÀ CHI PHÍ API

- Từ đợt review 2026-11 (mục 52), backend text mặc định của mọi preset là **9router gateway** (`ninerouter`): GĐ1 translation/vision, GĐ2 review và `series_composer`. ChatGPT Playwright vẫn là backend chọn được bằng config. **Không có fallback tự động giữa 9router và Playwright**; backend nào được chọn thì lỗi của backend đó fail rõ ràng.
- Các quy tắc Playwright dưới đây vẫn áp dụng khi preset chọn `chatgpt_playwright`; không chọn paid OpenAI API chỉ vì tiện hoặc nhanh hơn.
- OpenAI cho text/review/QA chỉ là fallback cuối của đường Playwright, phải đồng thời thỏa cả bốn điều kiện: fallback được cấu hình rõ, Playwright đã thử trước và hết retry/recovery với lỗi được phân loại là cho phép fallback, `api_budget_guard` cho phép, và `OPENAI_API_KEY` tồn tại.
- Không được bắt `Exception` rộng để kích hoạt paid API. Lỗi đăng nhập/profile/config, lỗi parse/validate output và lỗi lập trình phải fail rõ ràng, không chuyển sang OpenAI.
- OpenAI client/key phải được khởi tạo lazy tại thời điểm fallback thực sự kích hoạt; đường Playwright thành công không được yêu cầu key hoặc tạo OpenAI request.
- ASR có timecode, vision, TTS và media artifact không phải tác vụ browser-suitable: ưu tiên local hoặc provider chuyên dụng theo policy từng stage. OpenAI chỉ dùng khi là fallback cuối đã cấu hình và loại artifact đó không thể được tạo tin cậy bằng Playwright.
- Với tác vụ nặng, nhiều request, dữ liệu dài, xử lý hàng loạt hoặc có nguy cơ tốn nhiều chi phí API, tái dụng kỹ thuật Playwright worker theo pattern từ `D:\VibeCoding\auto_YT`.
- Pattern cần tái dụng từ `auto_YT` khi dùng Playwright worker:
  - Persistent browser profile để giữ phiên đăng nhập.
  - Worker claim job, ghi heartbeat, update status và resume an toàn.
  - Lưu conversation/session URL khi cần tiếp tục phiên làm việc.
  - Retry rõ ràng, log tiến độ, không crash toàn pipeline vì một job lỗi.
- Không tự ý chuyển một tác vụ nặng sang paid API nếu chưa có lý do kỹ thuật rõ ràng và chưa cập nhật quyết định vào `AGENTS.md` + `PROJECT_LOG.md`.
- Riêng GĐ1, translation/vision chỉ được bật khi preset thực sự cần các artifact đó; local/manual/no-translation/no-vision phải được ưu tiên khi đáp ứng yêu cầu. Không ép ASR timecode, vision frame hoặc TTS audio qua Playwright vì browser không cung cấp contract media ổn định.
## 11. GĐ2 IMPLEMENTATION HIỆN TẠI

- GĐ2 là CLI local, chạy bằng `python -m review`.
- Backend GĐ2: `ninerouter` (mặc định của preset, `--llm-backend ninerouter --llm-model ...`) hoặc `chatgpt_playwright`; giá trị legacy `openai_api` và `off` bị từ chối thay vì chạy API trực tiếp hoặc âm thầm bỏ review.
- Với 9router: narration được chia batch khoảng 8.000 ký tự (`narration_batches/`) để tránh `502 response_too_large`; beat ngắn hơn 75% mục tiêu được viết lại một lần (`narration_length_checked.json`); outline phải có ít nhất `ceil(char_budget / 700)` beat vì model viết khoảng 700–1.350 ký tự/beat bất kể mục tiêu. Model nằm trong cache identity (`review-prompts-v5`).
- Bám nguồn (từ `review-prompts-v5`): outline phải tóm tắt đúng sự kiện nằm trong `from_seg_id..to_seg_id` của chính beat (mỗi sự kiện thuộc đúng một beat); mỗi beat trong prompt narration mang theo `source` là các dòng FILM_MAP của span đó (mỗi dòng cắt ở 220 ký tự); mọi prompt viết lại beat (QA, style, length, opening coherence) có khối `SOURCE` gồm span của beat cộng 3 segment mỗi bên gắn nhãn `[other beat]`, và beat 0 của phim lẻ được nhắc quy tắc mở đầu (ai/ở đâu/vấn đề/vì sao quan trọng).
- Vòng QA: lượt đầu chấm toàn kịch bản; các lượt sau chỉ kiểm lại beat vừa viết lại (kèm 1 beat liền kề mỗi bên làm ngữ cảnh chỉ đọc, FILM_MAP chỉ gồm span của chúng) và bỏ mọi lỗi báo cho beat khác. Nhiều lỗi của cùng một beat được gộp thành một lần viết lại; lỗi vượt trần rewrite hoặc lần viết lại hỏng được mang sang lượt sau. Lý do: chấm lại toàn bộ 53 beat mỗi lượt chỉ làm LLM bắt các lỗi lặt vặt mới nên vòng lặp không hội tụ.
- Runtime Playwright: ChatGPT qua Playwright persistent browser profile cố định `D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1`. Code và mọi preset phát hành phải dùng đúng path này; `python -m review` từ chối profile khác. Mặc định `review.playwright_max_attempts=2` và `review.playwright_recovery_timeout_s=60`.
- Nếu prompt đã submit thành công, recovery chỉ tiếp tục quan sát cùng response và không gửi prompt lần hai. Chỉ lỗi xảy ra trước khi submit mới được phép thử lại request.
- Long ChatGPT prompts must not use clipboard paste because the web UI can convert them into truncated `Pasted text` attachments. The adapter clears pending attachments, uses verified composer fill with chunked `insert_text` fallback, and compares canonical whitespace-normalized content before dispatch.
- If prompt verification fails or any attachment remains in the composer, Playwright must raise a classified pre-submit error and must not click Send.
- For ChatGPT High extended reasoning, the adapter may click the visible `Answer now` control once after 300 seconds without an assistant message. This only completes the already-submitted response and must never resubmit the prompt.
- `review.openai_fallback_model` là opt-in. Fallback chỉ kích hoạt sau khi lỗi Playwright được phân loại là retryable/fallback-eligible và đã hết attempt; circuit breaker sau đó xử lý các request GĐ2 còn lại.
- `api_budget_guard=block` luôn chặn OpenAI fallback ở mọi quality mode nhưng vẫn cho Playwright chạy. Thiếu key hoặc budget bị chặn phải fail với lý do rõ ràng, không tạo API request.
- `work/review/openai_usage.json` và báo cáo orchestrator ghi trạng thái configured/allowed/blocked/triggered, mã lỗi, lý do, số lần thử Playwright, model và token usage; không thay đổi contract JSON giữa các stage.
- LLM chỉ được trả segment ids (`from_seg_id`, `to_seg_id`, hook ids). Code tự suy ra `src_tc_start` và `src_tc_end` từ `film_map`; không nhận timecode do LLM viết.
- `--sentence-refs` (config `review.sentence_refs`, bật trong preset production): sau khi narration cuối xong, một pass riêng (`review/sentence_refs.py`, batch ~8.000 ký tự narration, mỗi beat kèm dòng `source`) hỏi LLM segment id cho từng câu, clamp vào span beat và ghi `review_script.sentences.json`. Cache riêng `work/review/sentence_refs.json` theo hash narration/span/film_map/model, nên bật pass này không chạy lại outline/narration; batch lỗi để câu `grounded=false` và không cache. Mỗi batch được thử lại một lần khi JSON hỏng. `--sentence-refs-model` (config `review.sentence_refs_model`, preset production `ag/gemini-3.8-flash-low`) chạy pass này bằng client 9router riêng với model chính làm fallback: trên phim benchmark `-high` mất ~11 phút (mỗi request suy luận ~10 phút, song song không giúp), `-low` 12 s, tỷ lệ khớp audit tương đương (0,647 so với 0,642); usage ghi ở `work/review/sentence_refs_gateway_usage.json`. Với `--sentence-visual-queries` (config `review.sentence_visual_queries`, mặc định tắt), mỗi câu có nguồn còn kèm `visual_en`: mô tả tiếng Anh ≤ 15 từ về thứ máy quay nên thấy (không tên riêng, không lời thoại), cắt ở 20 từ, dùng làm truy vấn SigLIP cho `--sentence-visual` ở GĐ5.
- Package thực tế:
  - `review/`: orchestrator, cache, Playwright adapter, prompt flow, budget, coverage, timecode derivation.
  - `common/schema.py`: có thêm `ReviewBeat`, `ReviewMeta`, `validate_review_script`.
- Cache GĐ2 nằm trong `--work-dir`: `outline.json`, `narration.json`, `qa.json`, `revisions/`.
- `film_map.meta.json` đọc duration theo thứ tự `duration_s` rồi `duration`; nếu thiếu meta thì fallback `max(tc_end)` và ghi warning.
- Test tự động dùng mock LLM; không chạy ChatGPT/Playwright thật trong test.
## 12. GĐ3 IMPLEMENTATION HIỆN TẠI

- GĐ3 là CLI local, chạy bằng `python -m tts`.
- Provider TTS: `provider_mode=auto` dùng AI33.PRO Vivoo V3 primary, Genmax secondary và OpenAI `gpt-4o-mini-tts` fallback cuối. `provider_mode=vieneu` (preset production: clone giọng từ `data/voices/ngochuyen_ref.mp3`, CUDA) và `capcut` (preset example, giọng `BV074_streaming`, concurrency 4) là provider chọn tường minh, không tự fallback.
- Env vars: `VIVOO_API_KEY` cho AI33, `GENMAX_API_KEY` cho Genmax, `OPENAI_API_KEY` cho OpenAI fallback.
- Package thực tế:
  - `tts/`: provider adapters, cache manifest, sanitize, timing builder, concat, cost/meta và CLI orchestration.
  - `common/schema.py`: có thêm `BeatTiming`, `TtsMeta`, `TtsManifestEntry`, `validate_beats_timing`.
  - `common/media.py`: có thêm `normalize_audio`, `generate_silence`, `concat_audio`.
- GĐ3 cache theo hash provider/voice/model/speed/narration/normalize để không render lại beat không đổi.
- `provider_mode=auto` chỉ dùng provider có key hợp lệ theo thứ tự AI33 → Genmax → OpenAI; Genmax trong auto còn yêu cầu `genmax_voice_id`. Cache key bao gồm toàn bộ provider chain/model/voice và manifest ghi provider thực tế.
- `beats_timing.json` luôn dựng từ duration đo bằng ffprobe sau khi audio thật đã render/normalize.
- VieNeu pytorch/CUDA tổng hợp theo lô: beat chưa cache được gom `--vieneu-batch-size` (mặc định 16, config `tts.vieneu_batch_size`) beat mỗi lần `infer_batch`; beat đã cache giữ nguyên, manifest lưu sau mỗi lô. Cache key không đổi vì `infer` vốn lấy mẫu ngẫu nhiên và audio theo lô tương đương. Đo trên kịch bản benchmark (62 beat, không cache): 192 s (từng beat) → 129 s (lô 8) → 117 s (lô 16). ONNX/provider khác giữ đường cũ.
- Test tự động dùng mock provider và mock ffprobe/concat; không gọi AI33/Genmax thật.
- `--sentence-timing silence|proportional|off` (config `tts.sentence_timing`, preset production `silence`): `tts/sentence_timing.py` chuẩn hóa từng câu, ước lượng ranh giới theo số chữ cái + quãng ngắt, rồi bắt dính vào quãng lặng thật (`common.media.detect_silences`, ffmpeg silencedetect −35 dB, ≥ 0,15 s; ưu tiên quãng lặng dài trong ±2,5 s). Giọng TTS ngắt 0,5–0,7 s cuối câu và 0,2–0,35 s ở dấu phẩy. Không dùng WhisperX cho audio TTS: căn cả beat 30 s bị nén mọi câu vào vài giây đầu. Cache `work/tts/sentence_timing_cache.json`; `tts_meta.json` ghi `sentence_timing`, `n_sentence_aligned_beats`.
## 13. GĐ4 IMPLEMENTATION HIỆN TẠI

- GĐ4 là CLI local/offline, chạy bằng `python -m shots`.
- GĐ4 không dùng API. Dependencies: `scenedetect`, `opencv-python-headless`, `numpy`, ffmpeg/ffprobe.
- Shot detection dùng PySceneDetect `AdaptiveDetector` mặc định, có option `--detector content`; phim dài có thể dùng `--detector ffmpeg-scene` để detect bằng ffmpeg scene score nhanh hơn.
- GĐ4 có `--max-shot-len` để split scene/shot quá dài thành virtual shot ngắn hơn cho GĐ5; default `0` giữ behavior cũ, preset visual dùng giá trị ngắn hơn để đủ candidate 3-5s.
- Feature pass tính sẵn `motion_score`, `brightness`, `face_count`, `face_area`, `is_usable` cho GĐ5.
- Face detection dùng YuNet (`cv2.FaceDetectorYN`, model `shots/models/face_detection_yunet_2023mar.onnx`, MIT, ngưỡng 0,7); Haar chỉ là dự phòng khi YuNet không nạp được. Có thể tắt bằng `--face-detection off`. `shots.meta.json` ghi `model_versions.face_detector`; orchestrator chạy lại GĐ4 khi detector này khác detector môi trường hiện tại nạp được.
- Package thực tế:
  - `shots/`: detection, thumbnail extraction, feature computation, cache và CLI orchestration.
  - `common/schema.py`: có thêm `Shot`, `ShotsMeta`, `validate_shots`.
- Cache GĐ4 nằm trong `--work-dir`: `detection.json`, `features.json`, `end_credit_marking.json`, `overlay_marking.json`, `profile_marking.json`, `thumbs/`.
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
- Intra-beat splice trims replacement boundaries when necessary so both retained baseline fragments and replacement fragments remain at least `min_visual_clip`; if a valid trim is impossible, that replacement range is skipped with a warning instead of creating a flash cut.
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
- `--sentence-anchors --review-sentences ... --sentence-timing ...` (config `match.sentence_anchors`, preset production bật): `match/sentence_fill.py` chia beat thành cửa sổ câu; cửa sổ nguồn = segment của câu, nới tới ≥ max(4 s, 1,5× audio câu) trong span beat; câu bình luận lấy khoảng giữa hai câu có nguồn kề bên. Mỗi cửa sổ được lấp bằng `fill_beat` hiện có (cùng lọc story/usable/overlay/end-credit, widen, repeat guard) rồi `assign_timeline`. Câu chỉ được lấy phần nguồn chưa chiếu (trừ đi footage các placement trước; nới về sau rồi về trước trong span beat tới khi đủ 1,5× audio câu, hết chỗ mới cho lặp), vì các câu liền nhau hay trỏ cùng segment: bản đầu không có luật này lặp 153 s footage trên 2.041 s. Beat dùng đường này bỏ qua content anchors, opening story-visual start và intra-beat alignment; hook brightness guard vẫn chạy. `edl.qa.json` đo drift theo cửa sổ câu, `candidate_diagnostics.sentence_windows` ghi `ref_hit_ratio`; `edl.meta.json` ghi `n_sentence_anchored_beats`, `sentence_ref_hit_ratio`. `MATCH_ALGORITHM_VERSION=7`.
- `--sentence-visual --sentence-visual-weight 0.6` (config `match.sentence_visual`, cần `--visual-index`): `match.visual.compute_sentence_visual_scores` chấm mọi shot trong span beat với truy vấn `visual_en` của từng câu (keyframe tốt nhất, xác suất SigLIP đã hiệu chuẩn, chuẩn hóa min-max trong span) rồi thay điểm visual theo beat khi lấp cửa sổ câu; `visual_mode` theo beat có thể vẫn `off`. Trong chế độ chronological, điểm này chỉ đổi thứ tự giữa các shot cùng tầng drift. Đo trên phim benchmark (cùng refs, cùng audio): 0,642 → 0,652, nằm trong độ dao động ±0,02 của bộ chấm (36 câu tốt hơn, 28 kém hơn), nên preset production **không bật**; tính năng giữ ở dạng tùy chọn để thử trên phim khác.
- `--sentence-shot-scores PATH` (do stage `shot_pick` ghi, mục 53): điểm VLM thay điểm SigLIP theo câu; khi câu có điểm, `max_source_drift_s` trong cửa sổ câu được nới bằng độ dài cửa sổ để mọi shot trong cửa sổ cùng tầng drift (nếu không, chronological xếp tầng trước điểm nên shot xa hơn 12 s không bao giờ được chọn).
- Đo đồng bộ nội dung độc lập: `python -m scripts.av_sync_audit --run-dir runs/<name>` cho giám khảo đa phương thức (9router) xem từng câu + khung hình giữa câu + dòng FILM_MAP tại nguồn đang chiếu, chấm match/partial/mismatch/commentary; ghi `av_sync_audit.json/.html`. `edl.sync.qa.json` chỉ kiểm cấu trúc timeline, không đo được nội dung.
- Test tu dong dung JSON fixtures/mock; khong dung video/ffmpeg/API.


## 15. GĐ6 IMPLEMENTATION HIỆN TẠI

- GĐ6 là CLI local/offline, chạy bằng `python -m render`.
- GĐ6 chọn encoder bằng `render.encoder: auto|libx264|h264_nvenc` (`auto` dùng NVENC p5/cq24 khi mã hóa thử 1 khung hình thành công, nếu không thì libx264 medium/crf20); temp clip và clip đệm cuối dùng chung tham số để concat `-c copy`. `render.fps: source` render theo fps nguồn (season: chỉ khi mọi tập cùng fps, không thì 30 + cảnh báo); rate NTSC được ghi chính xác dạng `24000/1001`. Sau khi mux thành công, GĐ6 xóa `video_only*.mp4` và temp clip không dùng trong lần render này; `render.meta.json` ghi `encoder`.
- GĐ6 legacy đọc `edl.json`, `voiceover.mp3`, nguồn video và sinh `recap.mp4` + `render.meta.json`; không gọi API, không chọn lại footage, không caption, không nhạc nền, không giữ tiếng gốc. Optional `--edit-plan` + `--audio-assets` chỉ dành cho anime season enhanced ở mục 50.
- Render dùng `ffmpeg/ffprobe`; temp clips luôn video-only, re-encode H.264 `yuv420p`, cùng resolution/fps/codec params rồi concat bằng demuxer `-c copy`.
- Temp clips chỉ được commit vào cache bằng atomic replace sau khi FFmpeg thành công; cache hit phải probe đúng resolution/fps/duration/frame count. Sau concat, GĐ6 đối chiếu frame count và duration với timeline đã quantize trước khi tail-pad để file cache dở dang không biến thành freeze-frame dài.
- Khi video-only concat ngắn hơn voiceover, GĐ6 tail-pad bằng một freeze-frame clip ngắn rồi concat copy; full-video re-encode chỉ là fallback nếu tail padding fail.
- Frame-lock toàn cục: placement chiếm frame `[round(tl_start*fps), round(tl_end*fps))`; cut filter áp dụng time transform trước final FPS sampling, clone-pad nếu nguồn thiếu và trim bằng `end_frame` để luôn phát đúng số frame quantized, không round duration từng clip độc lập.
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
- DAG hiện tại: `shots` chạy song song với chuỗi `ingest → review → tts`; `visual_index` bắt đầu sau `shots` + `ingest` và chạy song song với review; `match` chờ `review + tts + shots`; `render` chạy cuối. `summary.json` ghi `status` và `started_at`/`finished_at` từng stage kể cả khi run lỗi; `run.log` có mốc thời gian UTC. Stage tùy chọn `shot_pick` (mục 53) chạy tuần tự ngay trước `match`, sau `review + tts + shots`.
- Config chính là YAML/JSON một chỗ; mẫu đầy đủ nằm ở `config.example.yaml`. Dependency mới: `PyYAML`.
- Resume/idempotent: output hợp lệ thì skip; `--force` chạy lại selected stages; `--force-stage <stage>` chạy lại stage đó và downstream selected stages.
- Hỗ trợ `--from`, `--to`, `--only`, `--dry-run`; dry-run chỉ in plan, không gọi subprocess.
- Validate output sau mỗi stage bằng schema chung trước khi chuyển stage kế tiếp. GĐ5 output hiện gồm cả `edl.review.html` khi `match.review_html=true`.
- `runs/` là artifact output/cache và không commit vào git.
## 17. GĐ1 ASR/TIMECODE UPDATE HIỆN TẠI

- GĐ1 giữ contract `film_map.json` nhưng meta có thêm `asr_provider`, `aligner_provider`, `timecode_quality`, `approximate_timecodes`, `asr_warnings`.
- GĐ1 hỗ trợ nguồn tiếng Việt bằng `--source-language vi --translate-mode none`; transcript Việt được giữ nguyên vào cả `ko` và `en` để không phá contract cũ, và không gọi KO→EN translation.
- Preset tiếng Việt ổn định nằm ở `config.vi.stable.yaml`; dùng cho video đã là recap/phim tiếng Việt trước khi cân nhắc pipeline Korean drama mặc định.
- Preset tiếng Việt mặc định bật `aligner=whisperx` với `source_language=vi`; WhisperX phải dùng align model `vi`, không được hardcode `ko`.
- ASR provider hiện có: `faster-whisper` default, `openai-gpt4o`, `openai-gpt4o-hybrid`, và `manual` để import transcript Markdown/JSON.
- Cache transcript mới trong `--work-dir`: `transcript_text.json`, `transcript_aligned.json`, `transcript_quality.json`.
- Local `faster-whisper` chunks long audio into `work/ingest/local_asr_chunks` to avoid whole-movie FFT memory spikes; it must pass `source_language` (`ko`/`vi`) into Whisper instead of hardcoding Korean.
- Vietnamese/offline ingest with `translate_mode=none` and `max_vision_frames=0` must not require `OPENAI_API_KEY`; only translation, vision, OpenAI ASR, or OpenAI transcript correction require it.
- Manual transcript dạng `[MM:SS] text` chỉ có timestamp start; end-time được suy luận nên luôn đánh dấu approximate nếu chưa align.
- `--aligner whisperx` chạy WhisperX forced alignment thật khi runtime `torch`/`whisperx` có sẵn; nếu lỗi thì fallback timestamp hiện tại và ghi warning. `qwen3` vẫn là placeholder an toàn.
- Segment quá dài được split theo câu/max duration để GĐ2/GĐ5 có source windows mịn hơn.
- GĐ2 đọc meta GĐ1 và ghi warning khi timecode approximate; không đổi contract `review_script.json`.
## 18. WHISPERX ALIGNMENT RUNTIME

- Máy hiện đã được smoke test với RTX 3060 12GB, Torch CUDA và WhisperX.
- Kết quả tốt nhất cho audio thật là `openai-gpt4o-hybrid` tạo chunk rough windows rồi `whisperx` refine timestamp trong từng chunk.
- Không dùng OpenAI full-span một mình làm timestamp vì API không trả segment timestamp.
- Không dùng WhisperX full-span text không có rough windows vì test thực tế bị nén timeline về đầu audio.
- Transcript QC sau alignment clamp theo duration, merge segment quá ngắn, split/flag segment quá dài và cảnh báo subtitle/credit artifacts.
- Runtime aligner nên xem là optional/heavy; automated tests mock WhisperX, không yêu cầu GPU.


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
- Mặc định `auto`: chỉ mở lại `chat_url` khi core review input hash không đổi; input đổi hoặc chưa có metadata thì bắt đầu conversation mới từ `https://chatgpt.com/`.
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
- GĐ3 phải chạy content QA deterministic trước khi normalize/synthesize `review_script.json`. Narration lặp câu, lặp template, generic fallback, foreign text, hoặc tiếng Việt không dấu là lỗi blocking và phải fail-fast trước khi gọi provider.
- `tts_meta.json` ghi thêm `review_script_hash`; series recap validate hash này trước khi skip/reuse TTS output để tránh synthesize lại từ script stale hoặc bẩn.

- `series_recap.tts_cps` is a planning estimate, not measured provider speed. Final duration audits must use `tts_meta.total_duration_s`; the Solo Leveling S01 AI33 run measured about `18.94` narration characters/second versus the preset estimate of `24.0`.

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

- Khi preset chọn `chatgpt_playwright`, GĐ2 chạy task viết nặng bằng ChatGPT qua Playwright persistent profile; không hỗ trợ chạy thẳng backend `openai_api`. Preset hiện tại dùng 9router (mục 52).
- `PlaywrightChatError` phải có mã lỗi và thuộc tính retry/fallback. Response chưa xuất hiện, streaming timeout, page/context bị ngắt và browser transient error có thể retry/fallback; chưa đăng nhập, profile/session sai, client chưa start, config sai, parse/validate error và lỗi ngoài Playwright không được fallback.
- OpenAI review fallback là opt-in và chỉ dùng sau khi đã hết `playwright_max_attempts`; model lấy từ config, key từ `OPENAI_API_KEY`, client được tạo lazy và không thay thế Playwright làm primary.
- Sau khi submit được xác nhận, `playwright_recovery_timeout_s` chỉ dùng để recover response hiện tại; tuyệt đối không gửi lại cùng prompt vì có thể tạo hai assistant response và tốn ngữ cảnh.
- CLI có `--reply-timeout-s` vì phim lẻ dài có response outline/narration/QA vượt 240s; default runtime hiện là 600s, run phim dài có thể dùng 900s.
- CLI có `--chatgpt-session-file` để restore cookies từ `auto_YT` khi cần; chỉ dùng session file mới capture, không dùng cookie cũ vì có thể bật modal `expired-session`.
- Luôn dùng `--chatgpt-profile-dir D:\VibeCoding\auto_YT\data\chrome_user_data\PROFILE_GPT_1`; không dùng profile local trong repo hoặc profile khác. Đảm bảo không có Chrome/Playwright khác đang giữ lock profile này.
- Runtime browser profile/cookies là artifact local, không commit; `data/`, `.env`, `runs/` phải luôn gitignored.

## 28. PHIM LẺ VS PHIM BỘ RUNTIME MODE

- Phim lẻ dùng mode single-video: một input phim tạo một recap độc lập; GĐ2 phải kể arc trọn vẹn từ mở đầu, twist, cao trào đến kết.
- Phim bộ nhiều tập dùng mode episode-first: mỗi tập vẫn chạy theo contract JSON hiện có, nhưng có `episode_planner` + series memory riêng để giữ glossary/entity bible, episode summaries và mạch truyện xuyên suốt.
- Config phim lẻ nên cân nhắc `target_ratio` khoảng `0.22–0.28` nếu muốn video gọn; test thực tế `DemThanhDoiSanQuy` đạt ratio khoảng `0.253`.

## 29. E2E LESSONS TỪ PHIM LẺ DÀI

- `openai-gpt4o-hybrid` chunked ASR phải skip/cache chunk audio quá nhỏ/invalid để không crash ở chunk cuối phim dài.
- `film_map` approximate timecode vẫn là rủi ro chính cho footage matching; phim dài nên ưu tiên alignment/QC tốt hơn khi cần sát hình.
- GĐ4 dùng YuNet; Recap phụ thuộc `opencv-python>=4.9,<5` vì `scenedetect` tự kéo OpenCV 5 (bỏ Haar `CascadeClassifier`). GĐ5 không được hard-filter theo face.
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
- Storymap cache version `storymap-v2` treats each confident `non_story_range` as a hard timeline boundary. A coarse story bucket must split before and after an OP/ED/preview/end-card gap instead of spanning across the excluded interval.
- G2 `review` accepts `--story-map` and writes optional `review_script.intent.json`; the required `review_script.json` contract is unchanged.
- G5 `match` accepts `--review-intent` and `--story-map`; in `opening_guard_s`, `--opening-ordered-fill` prefers source chronology before score to reduce opening voice/image ordering issues.
- Storymap and review-intent are movie-first defaults in orchestrator; episode behavior remains compatible and can opt out by config.

## G5/G6 Sync QA Report

- G5 writes optional `edl.sync.qa.json` next to `edl.json`; it compares `beats_timing.json` against EDL placements per beat.
- Sync QA flags beat start/end/duration deltas, placements outside the beat timing window, source-order mismatch, high reuse, short clips, long clips, and timeline gaps/overlaps.
- Use this report before changing global audio offset; if only a few beats are flagged, fix matching/timing locally instead of delaying the whole voiceover.
- `summary.json` includes `timecode_qa` from `film_map.meta.json`; if `approximate_timecodes=true`, treat footage/narration mismatch as an ASR/alignment risk first, not a render audio-delay problem.


## 31. GĐ3 TTS TEXT NORMALIZATION / PRONUNCIATION QA

- GĐ3 không sửa `review_script.json`; text gốc vẫn dùng cho QA/GĐ5, còn text gửi provider nằm trong `tts_script.json`.
- CLI hỗ trợ `--tts-text-normalization off|basic|vi`, `--tts-pronunciation-lexicon`, `--tts-normalized-script-output`, và `--tts-normalization-report`.
- Default `vi` xử lý acronym/ký hiệu/số nhỏ cho tiếng Việt: `AI`, `A.I.`, `ChatGPT`, `TTS`, `%`, `/`, URL/email/emoji; không được đổi chữ thường `ai` thành acronym.
- `tts_meta.json` ghi `text_normalization`, `pronunciation_lexicon_path`, `n_text_normalized`, và `normalization_warnings`.
- Cache GĐ3 phải dùng `tts_text` đã normalize để đổi lexicon/rule thì render lại đúng beat.
- Lexicon mẫu không chứa secret nằm ở `examples/tts/vi_pronunciation_lexicon.example.yaml`; key API vẫn chỉ đọc env/.env gitignored.


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

- Orchestrator có `quality_mode: low_cost|balanced|max_quality`, `text_llm_backend` và `api_budget_guard` để chọn backend theo chi phí/chất lượng.
- `text_llm_backend` chấp nhận `ninerouter` hoặc `chatgpt_playwright`; `openai_api` và `off` là config lỗi. OpenAI review chỉ được cấu hình qua `review.openai_fallback_model` và chỉ áp dụng cho đường Playwright; chi phí 9router ghi là `gateway`.
- Mọi quality mode dùng backend text đã chọn (preset: 9router) cho text-heavy QA/review. `api_budget_guard=block` chặn paid API fallback ở mọi mode, không chỉ `low_cost`.
- `low_cost` ưu tiên local-first ASR và tắt OpenAI vision mặc định. `balanced`/`max_quality` vẫn không được bỏ qua Playwright cho review.
- `cost_policy.json` và `cost_summary.json` là artifact bắt buộc của run orchestrator để review stage nào có thể tốn API/TTS trước khi chạy thật; báo cáo phải phân biệt fallback configured, allowed, blocked và triggered.
- GĐ3 pronunciation QA deterministic chạy trước synthesize và ghi `tts_pronunciation_qa.json`; backend suggestion chỉ sinh candidate lexicon, không tự sửa `review_script.json` và không tự gọi TTS lại.
- Playwright chỉ dùng cho text/QA phù hợp với browser; không dùng thay thế ASR timecode, vision frame, TTS audio hoặc media artifact cần local/provider runtime ổn định.


## 33. AUTO LOW-OPENAI VIETNAMESE PIPELINE

- `config.vi.low_openai.yaml` là preset test rẻ cho video tiếng Việt: local-first ASR (`faster-whisper` + `whisperx`), `translate_mode=none`, `max_vision_frames=0`, và `api_budget_guard=block`.
- `config.vi.balanced.auto.yaml` vẫn auto 100%: chạy local-first trước và chỉ rerun GĐ1 bằng `openai-gpt4o-hybrid` khi timecode/alignment có lỗi nghiêm trọng đã phân loại, sau đó force rerun downstream selected stages.
- `timecode_quality != strict` hoặc `approximate_timecodes=true` đơn lẻ không đủ để kích hoạt paid ASR. Trigger hiện tại phải đến từ `asr_warnings` có alignment fail/unavailable, hoặc warning tương lai được đánh dấu rõ bằng `severe:`/`[severe]`; ingest fail trước khi có `film_map.meta.json` vẫn fail-fast và không tự gọi paid API.
- `fallback_plan.json` ghi trigger/block reason; `fallback_summary.json` ghi kết quả sau fallback; `cost_summary.json` có `openai_fallback_possible` và `openai_fallback_triggered`.
- Nếu `api_budget_guard=block`, fallback OpenAI phải bị chặn rõ ràng thay vì âm thầm tốn API.

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

- Setuptools package discovery dùng allowlist cho các runtime package: `common`, `episode_planner`, `ingest`, `match`, `orchestrator`, `preflight`, `render`, `review`, `series_composer`, `series_match`, `series_recap`, `shots`, `storymap`, `tts`, `visual_index`; `run.py` được đóng gói như py-module.
- Không package `tests`, `runs`, `work`, `data`, `broll`, `tts_align`, `build`, `dist`, egg-info hoặc cache directories. Runtime/build artifacts phải nằm trong các thư mục gitignored hiện có.
- Extra `movie-visual` là bộ dependency local đầy đủ cho WhisperX + BGE-M3 + SigLIP2. OpenCLIP/video profile vẫn cài riêng bằng extra `video-profile`.
- Packaging v1 chỉ hỗ trợ editable install và wheel/import smoke trong repo; không cung cấp global `recap` console command và không package config/style assets để chạy từ thư mục bất kỳ.

## 36. PRODUCTION KOREAN MOVIE PRESET / TTS RUNTIME

- `config.movie.production.yaml` là preset production cho phim Hàn trên máy CUDA: Faster Whisper + WhisperX, 9router (`ag/gemini-3.8-flash-high`) cho dịch/vision/review, VieNeu TTS (clone giọng Ngọc Huyền, CUDA), ffmpeg-scene batch shots, End-Credit Guard, BGE-M3 matching và GĐ6 1080p; Visual Index tắt từ đợt review 2026-11 (R5-5: đóng góp trung vị 2% điểm chọn shot cho ~4 phút GPU), `config.movie.visual.yaml` vẫn bật để thử nghiệm.
- Production preset bật `orchestrator.runtime_preflight=true`; thiếu optional module trong `movie-visual` hoặc CUDA phải fail-fast trước khi chạy stage nặng.
- TTS production dùng VieNeu 3.8 CUDA clone từ `data/voices/ngochuyen_ref.mp3` (đo được 17,9 (mẫu) và 18,15 (cả phim) ký tự/giây ở speed 1.0, `review.tts_cps=18.5`); chuyển về CapCut bằng `provider_mode: capcut` + `voice_id: BV074_streaming`. Cấu hình CapCut cũ: concurrency `4`, đo được 20,6–21,7 ký tự/giây; `review.tts_cps=20.0` để recap đạt 95–100% mục tiêu sau khi trừ beat ngắn). Cấu hình AI33/Genmax/OpenAI vẫn giữ trong preset cho `provider_mode=auto`. `TtsMeta` có diagnostics optional/backward-compatible cho provider usage và fallback count.
- Stable/visual presets cũ vẫn tương thích và không bị tự động chuyển sang production behavior.

## 37. GĐ0 → GĐ1 / REVIEW CACHE INTEGRITY

- `video_profile.json` có optional `input_hash`, `config_hash`, `cache_version`; film identity dùng resolved path + size + `mtime_ns`. Legacy profile thiếu integrity metadata vẫn parse nhưng orchestrator xem là stale và rebuild một lần.
- Orchestrator truyền `--video-profile` trực tiếp vào GĐ1 khi preflight bật. GĐ1 chỉ bỏ visual-only gaps overlap `non_story_ranges`; speech thật luôn được giữ.
- GĐ1 dùng `work/ingest/cache_manifest.json` với key riêng cho audio, transcript, correction, translation và vision. Key chỉ được commit sau khi artifact tương ứng ghi thành công.
- `transcript_aligned.json` là output ASR/alignment/QC chưa correction. `transcript_corrected.json` là lớp correction riêng; đổi glossary/model không được chạy lại ASR.
- Selective invalidation: đổi film xóa toàn bộ; đổi ASR/alignment giữ audio; đổi correction giữ aligned transcript; đổi translation giữ transcript; đổi profile/vision config chỉ xóa vision/frames. `--force` mới xóa toàn bộ.
- `film_map.meta.json`, `story_map.meta.json` và `review_script.meta.json` có optional integrity hashes/cache version. Orchestrator chỉ skip khi source/config/content hashes hiện tại khớp; artifact legacy hoặc corrupt phải rerun stage và downstream selected.
- GĐ2 dùng `work/review/cache_manifest.json` hash nội dung film map/meta, story map, video profile, style sample và generation settings. Operational browser settings không làm đổi cache.
- `chat_session_policy=auto` mở conversation mới khi core review input đổi. Explicit `resume` tiếp tục session cũ nhưng phải ghi warning input mismatch.
- Upstream stale buộc downstream chạy lại nhưng không tự truyền `--force`; selective cache của stage con vẫn được giữ. Chỉ user `--force`/`--force-stage` mới yêu cầu xóa cache stage.

## 38. RELEASE CANDIDATE GATE

- Release gate chuẩn chạy bằng `scripts/release_check.ps1`; mặc định yêu cầu worktree sạch và `-MediaPath`. CI Windows dùng `-SkipMediaSmoke`; `-AllowDirty` chỉ dành cho phát triển gate, không hợp lệ để quyết định release.
- Gate offline bắt buộc secret scan tracked tree + full Git history, `git diff --check`, full pytest, compileall, editable-install dry-run, wheel build/content/install/import smoke, CLI help và production preset dry-run.
- GitHub Actions dùng `windows-latest`, Python 3.11, full-history checkout, `permissions: contents: read`; không được đọc repository secrets, cài browser hoặc GPU extras.
- Local media gate cắt 30 giây vào `work/release-gate`, xóa `OPENAI_API_KEY`, `VIVOO_API_KEY`, `GENMAX_API_KEY` khỏi subprocess và chỉ chạy GĐ0/GĐ1 bằng manual transcript, translation/vision offline.
- Cache smoke phải chứng minh unchanged reuse, profile-only vision invalidation, glossary giữ aligned transcript, và film identity rebuild toàn bộ artifacts GĐ1.
- Khi gate chạy từ một venv (ví dụ `.venv` tạo bằng `uv`), `wheel-venv` nhận thêm `gate-parent-venv.pth` trỏ tới site-packages của venv đó để có thư viện phụ thuộc; module `recap` vẫn được nạp từ wheel vừa cài. Trên CI (Python hệ thống) bước này không đổi.
- `work/release-gate/report.json` là audit artifact gitignored. Không ghi key/token; secret findings luôn redact.
- Release hiện tại là `1.0.3`, tag `v1.0.3`; các tag `v1.0.0`, `v1.0.1` và `v1.0.2` phải giữ nguyên. Chỉ tạo release/tag tiếp theo khi CI xanh, local media gate xanh không `-AllowDirty`, report pass, secret scan sạch và main đồng bộ remote.

## 39. CODE QUALITY TOOLING WAVE 1

- Dev extra now includes `ruff` and `tach`; install with `python -m pip install -e ".[dev]"`.
- `ruff` runs as check-only quality gate via `python -m ruff check .`; Wave 1 selects only critical syntax/undefined-name class rules to avoid an unrelated repo-wide cleanup diff.
- `tach.toml` records desired runtime boundaries: `common` is shared, each stage is isolated, `match` may depend on `visual_index`, and `orchestrator` may import stage validation/cache helpers.
- Release gate runs Tach as a blocking check and writes `work/release-gate/tach-report.txt`; boundary findings fail the gate.
- Tach's pytest plugin is disabled in `pyproject.toml`; pytest must keep running normal/full selections unless a task explicitly opts into Tach impacted-test behavior.
- `pytest-subprocess` and codebase graph tools are not workflow dependencies; add them only for a concrete subprocess-heavy test need or one-off architecture exploration.

## 40. ANIME RECAP CORE V1

- Anime support is additive and keeps the stage JSON contracts unchanged: `film_map.json`, `review_script.json`, `beats_timing.json`, `shots.json`, and `edl.json` stay the same.
- `SourceLanguage` includes `ja` and `TranslateMode` includes `ja-en`; ingest, WhisperX alignment, and OpenAI translation must pass the selected source language through.
- Anime presets live in `config.anime.series.yaml` and `config.anime.movie.yaml`. Series uses `content_type=anime_series`, Japanese ingest, strict non-story exclusion, `shots.face_detection=off`, and `match.w_face=0.0`/`match.w_visual=0.0`. Movie uses `content_type=anime_movie` with the same Japanese ingest defaults and setup-style opening behavior.
- `config.anime.series.yaml` defaults Faster Whisper to `ingest.device=cuda` on this RTX 3060 runtime; CPU `large-v3` ASR is too slow for practical multi-episode smoke iteration. Use CPU only as an explicit fallback.
- Manual anime metadata is local only. `preflight.manual_ranges` and `preflight.anime_context` can be YAML or JSON and must merge into `video_profile.non_story_ranges`; `review.context_file` loads the same context for cache identity and prompt grounding.
- Anime manual ranges support `end_card` in addition to opening, ending, preview, eyecatch, recap, sponsor, title-card, and studio-logo labels. Episode planning and series composition treat it as non-story content.
- Anime context must drive canonical Vietnamese names, aliases, special terms, pronunciation hints, continuity notes, and strict OP/ED/preview/recap guards. Do not hardcode OP/ED durations in presets.
- `review/llm_flow.py` must keep anime prompts explicit about no verbatim dialogue, no theme-song lyrics, no OP/ED/preview-only beats, and series-vs-movie continuity. QA should flag glossary drift, unsupported spoilers, non-story source usage, and unclear episode continuity.
- `review.non_story` may use the manual `non_story_ranges` from either `video_profile` or `anime_context`; G5 still keeps `exclude_non_story=true` by default and should not select anime opening/ending/preview footage when the preset says to exclude it.
- `shots.face_detection` is not a strong signal for anime recap V1; chronology and semantic/story context matter more than Haar face counts.

## 41. ANIME SERIES EPISODE V1

- Anime series stays episode-first for analysis in V1, but season-level output is now supported by `python -m series_recap`. Single-episode `run.py` remains compatible and keeps the existing stage JSON contracts unchanged.
- `series_manifest.yaml` is the source of truth for `series_id`, `episode_key`, `episode_number`, `title`, `source_path`, `arc`, and `spoiler_limit_episode`. Filename/OCR episode hints are sanity checks only, not authoritative identity.
- The internal `episode_planner` stage runs after GĐ1/GĐ1.5 when `orchestrator.recap_mode` is enabled. It writes `episode_meta.json`, `episode_memory.json`, and appends `series_memory_index.jsonl` under `orchestrator.series_memory_dir`.
- Recap mode thresholds are locked by config defaults: `importance_score >= 0.70` => `full`, `0.35-0.69` => `quick`, `0.15-0.34` => `merge`, `<0.15` => `skip`. Inputs include reveal, state change, fight/action, new entity, continuity dependency, and OP/ED/recap ratio signals.
- `quick` is the default useful path for low-event episodes: the orchestrator lowers review `target_ratio` toward `quick_target_ratio`, caps coverage/QA work, and prompts GĐ2 to focus on what viewers must remember for the next episode.
- In single-episode `run.py`, `merge` and `skip` still ingest/build storymap/write memory, then short-circuit review, TTS, shots, visual index, match, and render. In `series_recap`, every selected episode is still brought to `episode_planner` plus `shots` so the final season EDL can use story-safe bridge footage when needed.
- GĐ2 may load `episode_memory.json` through `review.context_file`. Memory retrieval must respect `spoiler_limit_episode`, include only prior eligible entries, and keep continuity/name guidance without leaking future spoilers.

## 42. MULTI-EPISODE / SEASON RECAP V1

- New season-level CLI: `python -m series_recap --manifest series_manifest.yaml --config config.anime.series.yaml --episodes 1-3`. If `--run-dir` is omitted, output defaults to `runs/<series_id>/series_recap/`.
- Do not concatenate raw episode videos before analysis. The pipeline remains episode-first for understanding and copyright guards: each selected episode runs `run.py --to episode_planner`, then `run.py --only shots`. With `series_recap.episode_concurrency > 1`, up to that many episodes run `run.py --to storymap` at once (one log per episode), and `run.py --only episode_planner` still runs in episode order because the planner reads earlier episodes' memory.
- Final stages are season-level: legacy uses `series_composer -> tts -> series_match -> render`; enhanced uses `series_composer -> tts -> series_match -> postprocess -> render`. The final video is `series_recap.mp4`; per-episode standalone recap render is not required.
- `series_recap.format` supports `compact`, `episode_chaptered`, and `episode_arc_chaptered`. `compact` keeps the older compressed season-summary behavior. `episode_chaptered` keeps the 1-4 episode chapter flow. Anime series now defaults to `episode_arc_chaptered` with `detail_level=detailed` for 12+ episode seasons.
- Anime detailed season defaults target 12 standard 24-minute episodes at 35-45 minutes (`target_total_min_s=2100`, `target_total_max_s=2700`) with a hard cap of 50 minutes (`target_total_hard_cap_s=3000`). Per-episode targets use `episode_min_s=90`, `episode_normal_s=180`, `episode_high_s=300`, and `arc_size=3`; every canon non-skip episode keeps a nonzero budget. Code scales the detailed episode plan to `target_total_max_s`, enforces the hard cap, and fails if the sum of episode minimums makes the configured bound impossible.
- New season artifacts are additive: `series_event_bank.json`, `series_review_script.json`, `series_review_script.meta.json`, `series_tts_script.json`, `series_chapters.json`, `youtube_chapters.txt`, and `edl.source_map.json`. Existing stage contracts remain unchanged; `series_tts_script.json` is compatible with GĐ3 TTS.
- Detailed season recap also writes `series_arc_plan.json` and `series_composer.qa.json` for arc/episode budget audit, prompt count, revision count, hard-cap diagnostics, and QA warnings.
- `series_composer` builds events from `episode_memory.json`, `story_map.json`, `film_map.json`, and `episode_meta.json`. ChatGPT Playwright may only select `event_id` values and write transformed Vietnamese narration; code derives `source_refs` and timecodes.
- `series_composer` builds `episode_targets` plus `season_target_plan` inside `series_event_bank.json`, and writes `series_arc_plan.json` with arc id/title, episode keys, per-episode seconds/chars/beats, and total target. ChatGPT still returns only selected `event_id` values; code derives source refs/timecodes and deterministic `series_chapters.json`.
- In `episode_arc_chaptered`, `series_composer` does not ask ChatGPT to write all 12 episodes in one initial response. It drafts per arc (default 3 episodes), then runs a final stitch pass that smooths transitions/deduplicates without accepting model-written timecodes. With 9router, arc drafts and arc revisions run concurrently up to `series_recap.composer_concurrency` (default 2) and are gathered in arc order; Playwright stays sequential. When the full stitch prompt exceeds 20,000 characters, each arc seam is smoothed separately (last beat of one arc + first beat of the next, one read-only neighbour each side, same event ids, length within 70-130%), reported as `boundary_stitch` / `invalid_boundary_stitch`.
- `series_composer` has deterministic QA plus one configurable Playwright revision pass (`series_recap.qa_max_revisions`, default `1`) for under-target length, repeated events, missing hook, missing episode chapters, per-episode/arc budget violations, hard-cap duration overflow, blocking narration content, non-story event selection, or non-monotonic post-hook story/episode order. Arc prompts receive a proportional hard character maximum, and beat-level QA maps `beat_ids` back to the owning episode/arc before revision. Invalid revision JSON keeps the prior valid draft and records a warning in `series_composer.qa.json`.
- `series_match` reads `series_review_script.json`, `beats_timing.json`, and per-episode `shots.json`; it writes standard `edl.json` where placement `src` may point to different episode source keys. It hard-excludes `is_story=false`, unusable shots, and end-credit shots.
- Sentence anchors for seasons (`series_recap.sentence_anchors`, default false, not yet enabled in any preset because no season was available to run): a cached step `series_sentence_refs` (`python -m series_composer.sentence_refs`, model `series_recap.sentence_refs_model`, default `ag/gemini-3.8-flash-low`, composer `llm_model` as fallback) writes `series_review_script.sentences.json`; season TTS adds `--sentence-timing silence`; `series_match --review-sentences --sentence-timing` plans sentence units (`plan_sentence_units`: commentary sentences inherit the nearest grounded ref, sentences shorter than the ref's minimum clip merge with a same-ref neighbour, the last unit absorbs the pause before the next beat, source windows from `common.sentence_windows` inside the ref span) and fills each unit with the existing `_ref_plan`. Match stage identity is `series-match-v5-sentence-units`. Beats without usable sentence data keep ref-quota matching.
- `series_match` skips a candidate that would leave an unfillable tail shorter than `min_visual_clip`, allowing the next chronological usable shot to absorb the remainder without creating a flash fragment.
- GĐ6 `render` remains legacy-compatible with `--film` for one source. For season output, pass `--source-map edl.source_map.json`; render cuts each EDL placement from the real source path mapped by `placement.src`.
- `config.anime.series.yaml` owns `series_recap` defaults: end-to-end scope, `format=episode_arc_chaptered`, `detail_level=detailed`, 12-episode target bounds, Playwright composer runtime, and final matching clip limits. The old mode contribution ratios remain available for `episode_chaptered` compatibility.
- If `preflight.manual_ranges` is unset, `series_recap` auto-discovers per-episode sidecars beside the manifest using `manual_ranges.<episode_key>.yaml|yml|json` or `<episode_key>.manual_ranges.yaml|yml|json`.
- `series_manifest.yaml` is still the source of truth for episode identity. OCR/title-card/filename hints are sanity checks only; link-series import or AniList metadata is out of scope for V1.

## 43. PRACTICAL ANIME SEASON PIPELINE V1

- `config.anime.series.practical.yaml` is the recommended 12-episode anime season smoke/production preset when cost control matters: OpenAI API is used only for JA->EN transcript translation, while ASR/WhisperX, shots, match, render, and TTS provider orchestration stay local/provider-specific.
- GĐ1 translation supports `translation_provider=openai_api|chatgpt_playwright|ninerouter`. 9router reuses the batch translator with concurrent batches (`translation_concurrency`, preset 2) and its own cache identity; prompt version `segment-v2`. The Playwright backend uses the locked `PROFILE_GPT_1`, keeps one fresh ChatGPT conversation per ingest process, translates validated batches sequentially, and never falls back to OpenAI API.
- Playwright translation defaults to 80 segments per batch. Each completed batch is atomically cached under `work/ingest/translation_batches` by transcript slice, provider, `chatgpt-web` identity, prompt/parser version, source language, and batch grouping; reruns resume only missing batches.
- Every browser response must cover exactly the requested segment IDs with non-empty English text. Japanese echo/script-dominant output, malformed JSON, missing IDs, and extra IDs are rejected and retry only the current batch. Source IDs, source text, and timecodes are always copied from the transcript rather than accepted from ChatGPT.
- Practical ingest sets `translation_required=true`, `translation_min_success_ratio=0.95`, `vision_provider=off`, and `max_vision_frames=0`. G1 must validate `OPENAI_API_KEY` before making a request: it must start with `sk-` and contain no hidden/control characters.
- If required translation falls below the configured success ratio, G1 fails before writing/committing `translated.json` or `film_map.json`; placeholder-only translation artifacts must not be treated as successful cache.
- `film_map.meta.json` records translation audit fields (`translation_total_count`, `translation_success_count`, `translation_success_ratio`, `translation_required`, `translation_min_success_ratio`) plus `vision_provider`.
- `config.anime.series.localvision.yaml` is opt-in for local Qwen frame descriptions: `vision_provider=local_qwen2_5_vl`, `vision_model=Qwen/Qwen2.5-VL-7B-Instruct`, `max_vision_frames=30`, resize long edge 768, batch size 1. Install optional deps with `python -m pip install -e ".[anime-vision]"`.
- Local vision is best-effort. If the local model/deps/runtime are unavailable, ingest logs a warning, writes an empty `vision.json`, and continues without visual gap descriptions; it must not fallback to OpenAI vision silently.
- Do not automate batch vision through Playwright. Playwright remains the primary backend only for text/review/composer/QA tasks where browser contracts are stable.

## 44. LOCAL WEB UI V1

- The local UI runs with `python -m recap_ui` or `scripts/start_recap_ui.ps1` and binds to `127.0.0.1` only. React/Vite production assets are served by the same FastAPI origin.
- The UI is a supervisor over the existing public CLIs. It must execute `run.py` or `python -m series_recap` through `subprocess.Popen`; it must not call pipeline stage functions in the FastAPI process or change the stage JSON contracts.
- Operational state lives in `data/recap_ui/recap_ui.db` using SQLite WAL. Pipeline artifacts under `runs/` remain the source of truth for validation, cache and delivery QA.
- V1 uses one active pipeline job with a FIFO queue. Run-dir, pipeline and `PROFILE_GPT_1` locks prevent duplicate work; resume always reuses the same run-dir without force.
- Browser clients receive opaque path/artifact tokens. Filesystem and media routes canonicalize paths against configured roots, and mutation routes require the per-process startup token.
- The browser never receives secret values or raw ChatGPT prompts. Provider health reports only configured/available booleans, and worker logs redact key-like values.
- Delivery QA is additive and UI-only: it distinguishes process success from final delivery pass/warn/block, probes rendered media, compares estimated versus actual duration, validates chapters/source coverage, and cross-checks EDL placements against story-safe shots.
- Season delivery duration is minimum-only: the actual rendered duration blocks delivery when it is below `target_total_min_s`; exceeding `target_total_max_s` or `target_total_hard_cap_s` remains acceptable. Maximum and hard-cap values stay as Composer planning references, not final-delivery blockers.
- Frontend source lives in `web/`; generated production assets live in `recap_ui/static/` and are built before wheel packaging. Node is a development/release dependency, not a daily runtime dependency after assets are built.

## 45. LOCAL WEB UI UX V1.1

- The operational SQLite schema is version `2` (`PRAGMA user_version=2`). Migration from v1 is additive and preserves existing rows while adding nullable `display_title` columns to `jobs` and `registered_runs`.
- `display_title` is user-facing, may contain Unicode, and is independent from `run_name`. `run_name` remains the validated 1-80 character safe directory slug used to construct the run path. Display-title fallback order is user input, manifest `series_title`, then the source/run directory name.
- UI locale defaults to Vietnamese. The English toggle changes only UI chrome, validation and status text, persists in browser storage, and must not translate pipeline logs or artifact content.
- Run discovery distinguishes `managed` jobs from `artifact_only` runs. Managed jobs expose only actions authorized by their state; artifact-only runs are read-only and may expose QA, artifacts, EDL, audio and video but must not resume or rerun because the UI does not reconstruct argv/config from artifacts.
- Read-only inspection uses opaque path tokens through `POST /api/inspect/single` and `POST /api/inspect/series`. Filesystem navigation uses `GET /api/fs/listing`, including breadcrumbs and current-directory selection, while retaining configured-root canonicalization and symlink/traversal protection.
- The guided new-run flow is `Source and output -> Preset -> Preflight -> Confirm`. It separates display title from run slug, inspects source/manifest before planning, keeps Basic mode override-free, submits only dirty allowlisted Advanced overrides, and groups season dry-run output by episode plus final chain.
- The UI derives the active loopback host and port from the browser/server metadata instead of hardcoding `127.0.0.1:8765`. Production remains loopback-only and same-origin; this does not authorize LAN binding.
- Duration policy is consistent across planning and delivery: below `target_total_min_s` is blocking, while values above preferred maximum or hard-cap references are amber planning warnings and remain deliverable. Composer may still use maximum/hard-cap values to constrain generation before render.
- UX V1.1 is an additive supervisor change only. Public CLI arguments, stage order, JSON contracts, pipeline cache identity, FIFO execution and Playwright-first/no-paid-fallback policy remain unchanged.

## 46. OPTIONAL VIENEU LOCAL TTS PROVIDER

- GĐ3 supports `provider_mode=vieneu` as an explicit local provider through the Apache-2.0 PyPI SDK `vieneu==3.8.3`; existing `auto` behavior remains AI33 -> Genmax -> OpenAI and must not silently select VieNeu.
- VieNeu v3 Turbo runs with `backend=onnx` on CPU (`precision=int8` default, `fp32` explicit) or `backend=pytorch` on CUDA (SDK 3.8, precision ignored). `style` is kept in config/cache identity but SDK >= 3.7 ignores it and the adapter no longer sends it. Built-in voices are exact IDs such as `Ngọc Linh` or `Trúc Ly`; a `voice_id` that is a path to a 3-8 s audio clip is cloned once per process (`add_voice`, denoise on) and its SHA-256 enters the cache identity. On pytorch/CUDA, uncached beats are synthesized in groups with `infer_batch` (`tts.vieneu_batch_size`, default 16).
- Install with `python -m pip install -e ".[tts-vieneu]"`. The optional extra pins `gradio>=5.49.1,<6` and `huggingface-hub>=0.34,<1` to remain compatible with the existing Transformers stack. VieNeu is lazy-imported and model weights are not downloaded during normal preflight.
- VieNeu emits 48 kHz WAV. The adapter serializes inference per process, writes a temporary WAV, converts it to a real MP3 with ffmpeg, applies `atempo` when `tts.speed != 1`, and leaves the existing normalization, timing, manifest and resume contracts unchanged. The SDK watermark remains enabled.
- Cache identity records provider package version, model, backend, precision, voice (clip name plus `reference_sha256` for clones), style, thread count, watermark policy and speed method. Missing SDK/ONNX/SoundFile runtime, invalid style/voice and model-download failures fail clearly without falling back to a paid provider.
- UI may select VieNeu only through the allowlisted `tts.provider_mode`/`tts.vieneu_style` overrides; backend, precision, model path and reference audio remain locked. Local preflight reports runtime readiness without exposing model paths, secrets or prompts.
- UI preflight also validates the locked VieNeu backend/precision/style/thread settings and checks a configured pronunciation lexicon before enqueue; it exposes only safe runtime details such as the lexicon filename and first-run model-download notice.
- `config.anime.series.vieneu.yaml` is the dedicated season preset for the selected production voice: exact voice ID `Ngọc Linh`, `style=doc_truyen`, pytorch/CUDA (SDK 3.8; switch to `vieneu_backend: onnx` on machines without an NVIDIA GPU), and `speed=0.9`. It uses 9router for required JA->EN transcript translation with `api_budget_guard=block`; the OpenAI-translation practical preset remains unchanged.
- Its `series_recap.tts_cps=16.0` follows the SDK 3.8 measurement (16.1 characters/second at speed 0.9 on the 39,880-character benchmark script); the earlier 17.0 came from the SDK 3.2.3 Tensei S01 run (63,107 normalized narration characters over 3,674.842 seconds, or 17.17 characters/second). The slightly conservative planning value keeps the 35-45 minute season target aligned with measured VieNeu delivery; `review.tts_cps=15` and generic anime presets remain unchanged.
- The VieNeu Solo Leveling preset loads `examples/anime/solo_leveling_vi_pronunciation.yaml`; current validated aliases normalize `Sung Jinwoo`/`Jinwoo` spellings to `Sung Chin U`/`Chin U` before cache identity and synthesis. VieNeu/sea-g2p maps `Chin U` closer to Korean `진우` than `Jin U`, which introduces a "you" glide. Add other character names only after a short pronunciation smoke test.

## 47. SERIES FINAL-STAGE CACHE IDENTITY

- `series_recap` stores a versioned Pydantic manifest at `series_recap/work/stage_manifest.json`. Composer, TTS, YouTube chapters, series match, and render skip only when their direct-input fingerprint, output signatures, and schema validation all match.
- Composer identity includes the manifest, ordered selected episode keys, `episode_meta.json`, `episode_memory.json`, `film_map.json`, `film_map.meta.json`, `story_map.json`, optional `video_profile.json`, and content-affecting Composer settings. It intentionally excludes `shots.json`.
- TTS identity includes `series_tts_script.json`, provider/voice/model/speed/pause/normalization settings, and pronunciation-lexicon content. Chapter identity includes `series_chapters.json` and `beats_timing.json`. Match identity includes review/timing artifacts, ordered `shots.json`, clip settings, and matcher version. Render identity includes EDL/source map/voiceover, source-media identities, and render settings.
- Automatic invalidation evaluates final stages independently and does not add `--force`; unchanged downstream content can remain cached and TTS beat/render temp-clip caches stay reusable. Explicit `--force` and `--force-final` retain their existing hard-force behavior.
- Episode planner invalidation also does not force GĐ4. Shots revalidate their own source/profile identity so profile-only OP/ED changes can reuse detection/features and only rebuild story-safe marking.
- `series_composer/work/cache_manifest.json` uses the same content identity rather than structure-only reuse. Legacy artifacts without the relevant manifest rebuild once; cache records are committed atomically only after successful execution and output validation.

## 48. SERIES MATCHER V2 AND EPISODE IDENTITY

- Series matcher algorithm `series-v2` keeps a candidate pool per `source_ref`, allocates capacity-aware fair quotas in event order, tries strict source windows before same-episode fallback, and fails with `beat_id`/`event_id` when every referenced event cannot be represented.
- Clip settings must satisfy `0 < min_visual_clip <= min_clip <= max_clip`. `min_clip` is the preferred normal clip length; candidates below it are used only when normal capacity is insufficient and must still meet the `min_visual_clip` hard floor. Multi-event beats shorter than `N * min_visual_clip` fail instead of dropping events.
- `edl.json` and `edl.source_map.json` are unchanged. `edl.qa.json` additively records requested, covered, fallback, and missing event IDs plus quotas and short-fallback diagnostics.
- `common.episodes.numeric_episode` is the shared parser for the series orchestrator and episode planner. It recognizes `s01e12`, `S1E12`, `1x12`, `e12`, `ep12`, `episode 12`, and numeric values; legacy fallback is used only when the text contains one unambiguous numeric group.

## 49. LAZY LOCAL VISION AND OPAQUE UI RESPONSES

- Local Qwen vision is created through a factory only after the vision cache misses, eligible visual gaps exist, and the provider is not `off`. The factory is called at most once per pass. Missing local runtime remains best-effort: warn, write empty `vision.json`, and never switch to OpenAI implicitly.
- `ExecutionPlan` and `JobRecord` remain internal persistence/execution models. Browser DTOs expose safe IDs/status/title, basename-only `run_name`, placeholder `command_preview`, logical DAG/output names, sanitized errors/checks/dry-run summaries, and timestamps.
- Browser plan/job/stage/health responses must not expose raw argv, command hashes, config paths/snapshots, absolute run paths, validator payloads, PID/worker/process metadata, raw dry-run stderr, or executable paths. Health exposes only `active_job_id`; resume/rerun accept job IDs and recover internal execution data from the repository.
- `tach.toml` declares `tts` as a direct dependency of `recap_ui` and `series_recap`. `series_composer` must remain independent from `tts`.

## 50. ANIME SEASON ENHANCED POST-PRODUCTION V1

- Enhanced post-production is opt-in only through `config.anime.series.vieneu.enhanced.yaml`. Existing presets and legacy runs retain voiceover-only render behavior and require no artifact migration. Global `run.py` does not add this stage in V1.
- The season final chain is `series_match -> postprocess -> render`. `edl.json` and `edl.source_map.json` remain unchanged. The offline `postprocess` package writes `edit_plan.json`, `edit_plan.meta.json`, `edit_plan.qa.json`, and `audio_attribution.txt`; it never calls an LLM/API.
- Shared Pydantic contracts live in `common/schema.py`: `AudioAssetManifest`, `AudioAsset`, `EditSpeedSegment`, `PlacementEdit`, `MusicCue`, `SfxCue`, `EditAudioMix`, `EditPlan`, `EditPlanMeta`, `EditPlanQa`, and edit override models. `RenderMeta` additively records enhanced visual/audio counters, loudness settings, true audio-stream count, and `original_audio_included=false`.
- `series_match --clip-profile dynamic_anime --event-bank ...` uses event-aware clip ranges: hook/conflict/action `1.5-2.4s`, setup/investigation/neutral `1.8-3.0s`, reveal/climax/ending `2.5-4.0s`, with hard floor `0.6s`. Candidate residuals rotate round-robin and avoid adjacent same-shot or contiguous-source reuse while alternatives exist. Dynamic candidates inset real shot boundaries by `0.75s` only when the remaining span still meets that event profile's minimum clip, reducing fade/black transition frames without degrading short valid shots; series stage cache identity is `series-match-v5-sentence-units` (was `series-match-v4-edge-inset`) while the public QA algorithm/profile remains `series-v3-dynamic-anime`. Forced adjacency fallbacks are written to `edl.qa.json`. Legacy profile remains algorithm `series-v2`.
- Visual planning is deterministic by seed: regular zoom `1.02-1.05`, horizontal pan within `+/-0.04`, one key push-in `1.02->1.08` per key beat, and 2.35:1 masks on at most 8% of placements and once per beat. Grades follow default, suspense/mystery, action/reveal, and emotional/ending mood presets.
- Speed ramp is transition-only and uses frame-locked `1.0/1.15/1.35` slices in the final 40% or less. Eligible beats are bridge/transition events, beats spanning more than one episode, and the beat that opens each episode chapter. Chaptered season scripts keep every beat inside one episode, so without the chapter-opening rule the ramp and every whoosh cue would never fire. It may extend source only inside the same indexed shot; insufficient headroom skips the effect and records QA. Reveal/climax never receive auto ramp, the hook is never a chapter opening, and a placement that already carries a freeze skips the ramp with a `freeze_conflict` QA entry.
- Freeze is `0.45s`, at most once per beat and at least `15s` apart. It is limited to hook/reveal/climax or importance `>=0.85`, skips placements under `2s`, reused placements, missing shots, and effectively static shots, and pairs with push-in plus impact SFX when cue throttling permits.
- Audio is local-only through `data/audio_library/audio_assets.yaml`; V1 never downloads assets. Manifest assets require unique IDs, safe relative paths, kind/mood or SFX kind, loopability, license source, and attribution metadata. Enhanced preflight ffprobes every referenced asset and fails before episode processing unless each file has exactly one decodable audio stream with positive finite duration, a loopable default music track, whoosh, impact, valid license fields, and all required FFmpeg filters.
- Optional `edit_overrides.yaml` beside the series manifest overrides beat/placement mood, track, visual values, ramp, freeze, or disables effects. Precedence is placement override -> beat override -> auto planner -> defaults.
- Music covers the full visual timeline, switches at beat boundaries, keeps automatic track groups at least `20s`, crossfades `1.5s`, and falls back to default with warnings. Track selection uses seeded round-robin. Whoosh spacing is at least `6s` and at most `6/min`; impact spacing is at least `15s`.
- Enhanced audio defaults are music `-24 LUFS`, voiceover duck `9dB`, whoosh `-14dB`, impact `-10dB`, and master `loudnorm I=-14:TP=-1:LRA=11` plus limiter. `audio_delay_s` delays voiceover only; music/SFX remain picture-locked.
- `render --edit-plan ... --audio-assets ...` requires both flags together. Video temp cache remains in `work/render/temp_clips/`; music/SFX/master cache is separate under `work/render/audio_cache/`. Final mux maps video-only plus master audio only, probes exactly one audio stream, and never maps source audio.
- Series-stage cache identity includes event bank/profile for match, all edit inputs/overrides/audio asset identities for postprocess, and edit plan/audio manifest/assets for enhanced render. Changing music reruns audio composition but reuses unchanged footage temp clips.
- The local UI conditionally shows `postprocess` in the season DAG, reports locked-preset audio readiness without exposing arbitrary paths, discovers enhanced artifacts, and blocks enhanced delivery when edit artifacts are invalid, output audio stream count is not one, or source-audio exclusion is not confirmed.
- Synthetic acceptance runs with `python -m scripts.enhanced_render_smoke --work-dir work/enhanced-render-smoke`. It verifies 1920x1080/30fps frame lock, exact duration/frame count, one audio stream, source-tone suppression, nonblack/no-flash frames, measurable zoom, and freeze motion reduction. The non-skipped local release media gate runs this smoke and stores its JSON report.
- Crop, grade, speed changes, reframing, muted source audio, and shorter cuts are editorial styling only. They do not guarantee avoidance of Content ID, copyright claims, or platform enforcement.

## 51. AI33 TEST AUDIO ASSET GENERATION AND REAL ACCEPTANCE

- `python -m scripts.generate_ai33_audio_assets` is a one-time online utility, not a pipeline stage. It reads only `VIVOO_API_KEY`, never accepts or logs a key, and uses `examples/audio/ai33_anime_cinematic_pack.yaml` to request up to seven instrumental anime-cinematic music tracks plus three whoosh and three impact effects.
- Live submission requires `--confirm-live`. The 10,000-credit guard blocks later forecastable submissions above budget; the first default-music task is an unknown-cost canary and can itself exceed the configured guard. Paid create requests retry only explicit `429` and `503 server_busy` responses, never an ambiguous network failure that might duplicate a charged task. Lifetime spend survives force/spec changes, and incremental task/report state plus `--resume` reuse every asset whose hash and ffprobe checks remain valid.
- Generated media is normalized to stereo 48 kHz, music receives a 1.5-second tail-to-head loop derivative, and every file must pass duration/audio-stream and loop-seam checks before entering `work/ai33-audio-library/audio_assets.test.yaml`. Companion artifacts are `generation_report.json` and `NOT_FOR_PRODUCTION.txt`.
- Generator output is restricted to a child of repository `work/`. Downloads follow only guarded AI33/Suno HTTPS redirects, strip `xi-api-key` before public Suno hops, and redact the key if a provider error echoes it.
- The generated manifest must identify the assets as `AI33/Suno generated test asset; commercial rights not verified`. It is technical-test material, is not assumed royalty-free, and must never be published to `data/audio_library/audio_assets.yaml` without separately verified commercial-use terms and license URL.
- `python -m scripts.enhanced_real_media_smoke --audio-assets work/ai33-audio-library/audio_assets.test.yaml` audits the existing Tensei season with `series-v3-dynamic-anime` plus postprocess, then renders an isolated 90-second real-footage excerpt. It SHA-256 guards production artifacts, enforces rolling cue density and the 0.6-second clip floor, measures required zoom/freeze capability, verifies non-silent music plus every scheduled SFX cue, proves temp footage is video-only, and checks final AAC packets match the cached master. If the full plan has freeze/impact capability but the first 90 seconds has no automatic freeze, the smoke may create a deterministic acceptance-only override inside `work/`; this must be recorded in the report and never copied to production.
- `series_recap` reconfigures stdout/stderr to UTF-8 at CLI startup when supported. This keeps dry-run command previews containing Unicode values such as VieNeu voice `Ngọc Linh` printable on Windows terminals whose inherited encoding is `cp1252`.

## 52. PIPELINE REVIEW 2026-11: 9ROUTER, RUNTIME VÀ ĐỘ SẠCH FOOTAGE

Báo cáo chi tiết và số đo trước/sau nằm ở `docs/pipeline-review/` (R1–R8). Phim benchmark: `Quý Ông Số Đỏ` (7.406 s, 1920x804, 25 fps, có phụ đề cứng).

- **9router gateway** (`common/llm_gateway.py`, `GatewayChatClient`): endpoint OpenAI-compatible cục bộ, mặc định `http://localhost:20128/v1` (ghi đè bằng `NINEROUTER_URL`), key chỉ đọc từ biến môi trường `NINEROUTER_API_KEY`, không bao giờ ghi vào config/log/git. Client phân loại lỗi: quarantine/503/429/upstream bị từ chối là lỗi tạm, chờ theo `Retry-After`, `reset after Ns` hoặc `retryAfter` trong body (tối đa 6 lần); `response_too_large` và 401/403/404 thì dừng ngay. Preflight của orchestrator/series (`require_gateway_ready`) kiểm key, `/models` và probe thật model đã chọn trước khi chạy stage nặng; probe bận tạm thời vẫn được coi là sẵn sàng.
- Dùng 9router cho: GĐ1 translation (batch song song), GĐ1 vision (`image_url` data URL, gặp lỗi vĩnh viễn thì dừng và không ghi cache `vision.json`; prompt bỏ qua phụ đề/logo), GĐ2 review và `series_composer`. Model mặc định `ag/gemini-3.8-flash-high` (chọn qua A/B ở R3); model được đưa vào cache identity của từng stage.
- **Runtime cô lập**: chạy bằng `.venv` tạo bằng `uv` (torch 2.8.0+cu128, `transformers` 4.57.x, OpenCV 4.x). `orchestrator.runtime_preflight` báo lỗi khi gói đã cài lệch specifier của `recap` (`common/runtime.py`). Trên Windows, GĐ1 nạp trước cuBLAS/cudart của torch cho CTranslate2 và giải phóng VRAM sau ASR/WhisperX. `scripts/start_recap_ui.ps1` ưu tiên `.venv`.
- `ingest.asr_batch_size` (faster-whisper `BatchedInferencePipeline`, có timestamp) là tùy chọn, mặc định 0 (tắt): nhanh gấp 1,8 lần nhưng độ giống chữ chỉ 91,7% so với chạy tuần tự.
- **Overlay guard GĐ4** (`shots/overlays.py`, `shots.overlay_guard`, mặc định bật trong orchestrator): lấy khung giữa mỗi shot ở độ rộng 640 bằng ffmpeg seek. Bộ ba shot liên tiếp được xét khi trải ≥ 6 s và khác hình nhau (lệch xám trung bình ≥ 12). Nếu ≥ 0,2% điểm ảnh là cạnh vừa đứng yên vừa giữ nguyên màu (sai lệch < 18) thì đó là chữ chèn cứng, ví dụ quảng cáo của site phim. Shot chứa nó, cộng 1 shot mỗi bên, bị đánh `is_usable=false`, `unusable_reasons=["overlay_text"]`; GĐ5 vốn đã loại cứng lý do này. Nếu hơn 30% bộ ba bị gắn cờ thì coi là watermark cố định: không loại shot, chỉ cảnh báo. Cache `overlay_marking.json`; `shots.meta.json` ghi `n_overlay`, `overlay_intervals`, `hardsub_ratio` và `model_versions.overlay_detector`.
- Overlay guard cũng đo tỷ lệ thời lượng shot có phụ đề cứng (chữ trắng viền tối ở dải 86–99% chiều cao). Từ 20% trở lên thì cảnh báo nên dùng bản raw; pipeline không tự che phụ đề, vì làm mờ một dải cố định không che được phụ đề 2 dòng mà vẫn làm xấu hình.
- Thumbnail GĐ4 và keyframe GĐ4.5 được ghi bằng `common.media.write_image` (`cv2.imencode` + `Path.write_bytes`), vì `cv2.imwrite` làm hỏng đường dẫn tiếng Việt trên Windows.
- **Concurrency thích ứng** (`AdaptiveLimiter` trong `common/llm_gateway.py`): mọi `GatewayChatClient` giới hạn số request đang bay. Cửa sổ bắt đầu ở 2, tăng 1 sau mỗi 8 request liên tiếp thành công cho tới trần, giảm một nửa (tối thiểu 1) khi gặp `rate_limited`/`quarantined`/`upstream_rejected` và giữ mức đó 60 s. Các cờ `translation_concurrency`, `vision_concurrency`, `review.llm_concurrency` (rewrite QA/style/length song song; narration batch vẫn tuần tự), `series_recap.composer_concurrency` là **trần**, không phải mức cố định. Playwright luôn chạy 1. `gateway_usage.json`/usage summary ghi `max_concurrency`, `peak_concurrency`, `throttle_events`.
- **Fallback model trong gateway**: `review.llm_fallback_model`, `ingest.translate_fallback_model`, `ingest.vision_fallback_model`, `series_recap.llm_fallback_model` (preset: `ag/claude-sonnet-4-6`, đã probe). Sau 2 lần lỗi thuộc {`quarantined`, `rate_limited`, `upstream_rejected`, `upstream_error`} trên model hiện tại, client chuyển model cho các lần thử còn lại trong cùng `max_attempts`; lỗi không retryable không đổi model. Đây là fallback *trong* 9router; không có fallback tự động sang Playwright hay OpenAI. Cache identity vẫn theo model chính; `fallback_switches` và `served_models` ghi model thật sự trả lời. Preflight probe cả fallback nhưng chỉ cảnh báo khi fallback không sẵn sàng.
- Ghi chú vận hành: phim tải từ site lậu có thể chứa quảng cáo cờ bạc chèn cứng; overlay guard loại các khoảng này khỏi footage, nhưng vẫn nên ưu tiên nguồn sạch.
- **VieNeu SDK 3.8 (2026-11)**: `tts-vieneu` pins `vieneu==3.8.3`. `tts.vieneu_backend` accepts `onnx` (CPU) or `pytorch` (CUDA engine, ~0.7 GB VRAM); `config.anime.series.vieneu*.yaml` use `pytorch`. `style` is ignored by the SDK and no longer sent. A `voice_id` that is a path to a 3-8 s audio file is enrolled once per process via `add_voice` (zero-shot clone) and its SHA-256 enters the TTS cache identity; only use clips you have the right to clone. Measured on the benchmark script (59 beats, 39,880 chars, speed 0.9): 170 s wall for 2,476 s of audio (RTF ~0.07, about 5x faster than ONNX int8 on CPU, per-beat path); batched `infer_batch` (default 16 beats) took the uncached benchmark TTS from 192 s to 117 s). `series_recap.tts_cps` is now 16.0 to match the measured 16.1 chars/s at speed 0.9 (it was 17.0 from SDK 3.2.3); confirm on the next season run.

## 53. STAGE TÙY CHỌN `shot_pick` (VLM CHỌN SHOT THEO CÂU)

- `python -m shot_pick` (package `shot_pick/`, config `shot_pick.enabled`, mặc định tắt): với mỗi câu có nguồn, lấy tối đa 6 shot story/usable (không end-credit) trong cửa sổ nguồn của câu (`common.sentence_windows`, cùng cửa sổ GĐ5 dùng), gửi thumbnail GĐ4 (~360 px) cho model đa phương thức qua 9router (`shot_pick.model`, mặc định `ag/gemini-3.8-flash-low`, fallback `review.llm_model`), ~6 câu mỗi request, chấm 0–3 theo "khung hình có cho thấy người/hành động câu kể không" (ưu tiên người được nhắc tới hơn người nghe). Ghi `sentence_shot_scores.json` + `.meta.json` (`ShotPickMeta`, có `input_hash`); cache theo câu + ứng viên + hash thumbnail + model trong `work/shot_pick/cache.json`; batch lỗi thử lại 1 lần rồi bỏ trống (GĐ5 giữ cách chọn hiện tại cho câu đó). GĐ5 vẫn offline.
- Orchestrator: `STAGES` giữa `visual_index` và `match`, phụ thuộc `review`, `tts`, `shots`; `require_gateway_ready` trước khi chạy; `cost_policy` ghi `gateway`; khi bật, `match` nhận `--sentence-shot-scores`.
- Kết quả trên phim benchmark (cùng kịch bản, refs, giọng đọc; `final6b` so với `final10`): giám khảo Gemini 0,638 → 0,668 (lệch 34 → 25, từng câu 50 tốt hơn / 37 kém hơn); giám khảo Claude (`ag/claude-sonnet-4-6`) 0,610 → 0,615 (31 / 34). Mức tăng chỉ xuất hiện ở giám khảo cùng họ với model chọn shot, nên preset production **không bật**. Thời gian: 126 s cho 45 request.
- `scripts/av_sync_audit.py` có `--tag` để lưu kết quả từng giám khảo riêng; với Claude dùng `--batch-size 6 --concurrency 2` (batch 12 làm câu trả lời bị cắt, mất JSON).
