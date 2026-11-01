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
