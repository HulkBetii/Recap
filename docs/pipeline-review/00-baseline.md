# 00 — Baseline: "Quý Ông Số Đỏ" (2026-11-01)

## Đầu vào và cấu hình

- Phim: `Quý Ông Số Đỏ Vietsub - PhimMoi.mp4`, 7.406 s (2 giờ 03 phút), 1920x804 (2,39:1), 25 fps, audio tiếng Hàn (nhận diện `ko` ≥0,989), có hardsub tiếng Việt in thẳng lên hình.
- Preset: `config.movie.production.yaml` sau Phase A: 9router (`heavy` cho GĐ2, `ag/gemini-3.8-flash-high` cho dịch), CapCut `BV074_streaming`, CUDA trên RTX 3060 12 GB.
- Run: `runs/bench-qods-baseline` (gitignored). Có thêm một run thử trên 900 giây đầu: `runs/bench-qods-900s`.

## Thời gian từng stage (phim đầy đủ)

| Stage | Thời gian | Ghi chú |
|---|---:|---|
| preflight | 0,4 phút | intro không được nhận diện (confidence 0,4) |
| ingest — tách audio | 0,4 phút | |
| ingest — ASR + WhisperX | 12,3 phút | GPU trung bình 55% |
| ingest — dịch KO→EN | 6,2 phút | 1.160 câu, 15 batch, thành công 100% |
| ingest — vision | **29,7 phút** | 193/193 lần gọi OpenAI lỗi `credit_balance_exhausted`; ra 0 mô tả |
| shots (chạy song song) | ~8,1 phút | 1.527 shot |
| review (GĐ2) | 14,6 phút | 28 request, 380 nghìn token vào / 42 nghìn token ra, cả 28 request đều do `claude-sonnet-4-6` trả lời |
| tts (CapCut) | 1,5 phút | 19.229 ký tự → 925,95 s |
| visual_index | 3,2 phút | chạy sau tts dù chỉ phụ thuộc shots |
| match | 1,1 phút | 286 placement |
| render | 5,4 phút | 286 temp clip, libx264 `medium` |
| **Tổng thời gian có hiệu quả** | **~75 phút** | **~45 phút nếu bỏ phần vision vô ích** |

Thời gian chờ do các lỗi gateway đã sửa trong lúc chạy baseline không được tính vào bảng.

## Chất lượng đầu ra

| Chỉ số | Giá trị | Đánh giá |
|---|---|---|
| Độ dài recap | 925,9 s = **12,5%** độ dài phim | GĐ2 nhắm 1.903 s (25,7%); thực tế chỉ đạt **49%** mục tiêu |
| Narration so với ngân sách ký tự | 19.212 / 28.550 (67%) | model viết thiếu ký tự |
| Tốc độ đọc TTS thực tế | 20,8 ký tự/giây | preset lập kế hoạch theo `tts_cps=15` |
| QA GĐ2 | 3 vòng, vòng cuối **chưa pass, còn 18 issue** | mỗi vòng chỉ viết lại tối đa 6 beat, trong khi QA tìm ra 19–29 issue |
| Coverage | 97,8% | đạt |
| Mô tả hình cho đoạn không thoại | **0/193** | vision hỏng |
| Mở đầu | **6 giây logo hãng phim / PLUS M** | lỗi hook |
| Hardsub trong recap | ~50% frame lấy mẫu | sub thoại gốc không khớp narration |
| Nhận diện mặt | **0/1.527 shot** | OpenCV 5 không còn `CascadeClassifier` |
| Sync QA | 0 khoảng hở, 0 chồng lấn, 2 `source_order_mismatch` | tốt |
| Render | 1080p, 30 fps (nguồn 25 fps), AAC 48 kHz, khớp thời lượng | tốt; còn vấn đề fps và fit (R7) |

## Phát hiện về runtime

- **VRAM:** tiến trình ingest giữ 11,9/12 GB VRAM suốt 36 phút sau khi ASR đã xong, kéo dài qua cả lúc dịch và vision.
- **Môi trường Python dùng chung đã lệch pin:** `transformers` 5.16.1 (pin `<5`), `opencv-python` 5.0 (pin `opencv-python-headless <5`), `Pillow` 12.3 (pin `<12`), `huggingface-hub` 1.29 (pin `<1`). Hệ quả: `visual_index` bị crash (đã thêm shim), và nhận diện mặt bị tắt âm thầm.

## Lỗi của Phase A phát hiện và đã sửa trong lúc chạy baseline

1. 9router liệt kê model nhưng upstream không phục vụ (`gemini-4-argon-high`): thêm bước gọi thử thật một request nhỏ ở preflight.
2. "Account retry budget exhausted": client chờ đúng theo `retryAfter`; số lần thử mặc định nâng lên 6.
3. Lỗi 404 của một tài khoản, bị 9router bọc trong 503: retry theo gợi ý "reset after Ns".
4. `502 response_too_large` khi viết narration cho cả phim trong một response: chia narration thành batch khoảng 8.000 ký tự, có cache từng batch.
5. Claude trả `hook` dạng văn bản; trả lại cả span/timecode khi viết lại beat: prompt ghi rõ kiểu dữ liệu, parse chỉ lấy `beat_id` và `narration`.
6. Opus qua Antigravity không ổn định: GĐ2 dùng combo `heavy` và ghi lại model thực sự trả lời trong `served_models`.
7. `transformers` 5 đổi kiểu dữ liệu trả về: thêm shim trong `visual_index/encoder.py`.
