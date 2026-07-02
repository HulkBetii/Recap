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
- `work/review/revisions/`

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

Cache GĐ4:

- `work/shots/detection.json`
- `work/shots/features.json`
- `work/shots/thumbs/`

Thêm `--force` để detect/tính feature lại.
## Chạy GĐ5

GĐ5 nhận `review_script.json`, `beats_timing.json`, `shots.json` và tạo `edl.json` + `edl.meta.json`. Stage này chỉ xử lý JSON, không decode video và không dùng API.

```powershell
python -m match `
  --review-script out\review_script.json `
  --beats-timing out\beats_timing.json `
  --shots out\shots.json `
  --output out\edl.json `
  --min-clip 3.0 `
  --max-clip 5.0 `
  --widen-margin 15 `
  --max-widen 3 `
  --allow-repeat `
  --seed 1234 `
  --work-dir work\match
```

Nguyên tắc GĐ5:

- Face là điểm cộng mềm, không lọc cứng.
- Placement mặc định 1:1 speed `1.0`.
- Thiếu footage thì nới cửa sổ nguồn trước, sau đó mới repeat có kiểm soát.
- Cache nằm ở `work/match/plan.json`; thêm `--force` để recompute.

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
- `render.meta.json` ghi duration video/audio, số temp clips, cache hits và warnings.

