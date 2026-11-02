# Tổng kết review pipeline R1–R8

Phim benchmark: `Quý Ông Số Đỏ` (7.406 s, 1920x804, 25 fps, phụ đề cứng). Cả hai lần đo dưới đây đều chạy **từ đầu, không cache**, preset `config.movie.production.yaml`, cùng máy (RTX 3060 12 GB, 12 luồng CPU).

| Chỉ số | Baseline (`runs/bench-qods-baseline`) | Sau R1–R8 (`runs/bench-qods-final`) |
|---|---|---|
| Tổng thời gian | ~75 phút | **54,0 phút** (13:33:19 → 14:27:21) |
| Độ dài recap / mục tiêu | 926 s / 1.926 s (49%) | **1.767 s / 1.926 s (92%)** |
| Số beat | 18 | 67 |
| QA kịch bản | 4 vòng, chưa pass (18 lỗi còn lại) | pass sau 3 vòng |
| Vision mô tả khe im lặng | 0/193 | 200/200 |
| Shot có mặt người | 0/1.527 | 1.108/1.527 |
| Đồng bộ hình–tiếng | 2 cảnh báo thứ tự nguồn | 0 khoảng hở, 0 chồng lấn, 0 cảnh báo |
| Logo/quảng cáo trong recap | logo mở đầu, 3 placement quảng cáo cờ bạc | 0 |
| Render | libx264, 30 fps (17% khung lặp) | NVENC, 25 fps theo nguồn, 221 s |
| VRAM đỉnh | 11,9 GB (giữ sau ASR) | 9,5 GB, giải phóng sau ASR |

## Thời gian từng stage (lần cuối)

| Stage | Thời gian | Ghi chú |
|---|---|---|
| preflight | 12 s | |
| ingest | 30,1 phút | ASR + WhisperX 14 phút, dịch 8 phút, vision 8 phút (9router) |
| shots | 8,5 phút | song song với ingest, gồm quét overlay |
| storymap | <1 s | |
| review | 15,1 phút | 9router; lần chạy có cache trước đó mất 10,8 phút, chênh lệch do tốc độ gateway |
| visual_index | 4,2 phút | song song với review |
| tts | 3,3 phút | CapCut |
| match | 73 s | |
| render | 221 s | NVENC, 493 placement |

Đường găng: ingest → review → tts → match → render ≈ 50 phút; shots và visual index không còn trên đường găng.

## Còn lại

- Ingest và review chiếm 45 trong 54 phút, phần lớn là chờ 9router (GPU trung bình chỉ 16%). Tăng concurrency là cách nhanh nhất nhưng đổi lấy rủi ro quarantine của pool tài khoản.
- Recap đạt 92% mục tiêu (lần có cache đạt 95%); CapCut đọc nhanh hơn `tts_cps=20.5` một chút.
- R8-1/R8-2 (season) mới kiểm bằng test giả.
- Phụ đề cứng chiếm 45% khung hình của nguồn này; là lỗi nguồn, không phải pipeline.

## Đợt bổ sung 2026-11-01: concurrency thích ứng, fallback model, cắt visual index

Chạy lại từ đầu, không cache (`runs/bench-qods-final2`), cùng máy và preset production đã sửa.

| Chỉ số | Sau R1–R8 | Sau đợt bổ sung |
|---|---|---|
| Tổng thời gian | 54,0 phút | **48,8 phút** |
| ingest | 30,1 phút (ASR 14, dịch 8, vision 8) | 25,5 phút (ASR 14, dịch 7, vision 5) |
| review | 15,1 phút | 14,1 phút (QA pass ngay vòng 1, không có rewrite để chạy song song) |
| visual_index | 4,2 phút GPU (ngoài đường găng) | tắt |
| Độ dài recap / mục tiêu | 92% | **96,4%** |
| Throttle / đổi model | — | 0 / 0 (trần chưa chạm giới hạn của pool) |
| VRAM đỉnh | 9,5 GB | 8,3 GB |

Mục tiêu ≤ 45 phút chưa đạt. Phần còn lại trên đường găng là ASR + WhisperX (14 phút, local) và chuỗi outline → narration batch tuần tự trong review (~10 phút); muốn nhanh hơn nữa phải đụng tới hai chỗ này, với đánh đổi về độ chính xác timestamp (ASR batch) hoặc mạch kể (narration song song).

## Đợt bổ sung 2026-11-02: QA kịch bản bám nguồn

Nguyên nhân QA không pass: outline gán span lệch so với nội dung tóm tắt, viết lại beat không thấy FILM_MAP, và mỗi vòng QA chấm lại toàn bộ kịch bản nên luôn phát sinh lỗi mới. Sau khi sửa (`review-prompts-v5`), đo trên bản sao input của `final4`:

| Chỉ số | final2 / final3 / final4 | qa5a / qa5b |
|---|---|---|
| QA GĐ2 cuối | 0 / 1 / 4 lỗi | 0 / 0 lỗi |
| Chấm độc lập toàn kịch bản (2 lần) | 0+4 / 3+2 / 3+2 | 0+1 / 0+0 |
| Request 9router của GĐ2 | — / — / 35 | 17 / 13 |
| Input token GĐ2 | — / — / 738k | 383k / 334k |

Chạy lại từ GĐ2 tới render trên cache ingest/shots của `final4` (`runs/bench-qods-final5`):

| Chỉ số | final4 | final5 |
|---|---|---|
| Tổng thời gian các stage review → render | 25,8 phút | 19,6 phút |
| review | 19,9 phút | 11,5 phút |
| QA GĐ2 theo vòng | 14 → 5 → 5 → 4 | 1 → 0 |
| Chấm độc lập (2 lần) | 3+2 | 0+0 |
| Độ dài recap / mục tiêu | 103,6% | 106,1% |
| Sync QA | sạch | sạch |
| Beat drift cao | 5 | 7 |
| Drift nguồn trung vị / p90 | 3,0 / 9,8 s | 3,4 / 9,3 s |
| Placement trên shot quảng cáo | 0 | 0 |

Bộ chấm dao động mạnh (final2 lần 0 lỗi, lần 4 lỗi), nên đây là xu hướng chứ chưa phải tỷ lệ chính xác.
