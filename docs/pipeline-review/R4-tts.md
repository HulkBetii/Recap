# R4 — GĐ3 TTS (CapCut)

Phạm vi: `tts/`, `common/media.py`. Số liệu: TTS của baseline và lượt đo lại 900 giây; mẫu giọng ở `work/bench/r4_voices/` (3 beat mở đầu, 1.286 ký tự).

## Giọng CapCut dùng được

| Giọng | Tên hiển thị | Ký tự/giây | Mẫu nghe |
|---|---|---:|---|
| `BV074_streaming` (đang dùng) | Cô Gái Hoạt Ngôn | 20,56 | `work/bench/r4_voices/BV074_streaming.mp3` |
| `BV421_vivn_streaming` | Nhỏ Ngọt Ngào | 20,31 | `work/bench/r4_voices/BV421_vivn_streaming.mp3` |
| `BV562_streaming` | Mai | 19,72 | `work/bench/r4_voices/BV562_streaming.mp3` |
| `vi_female_huong` | Giọng Nữ Phổ Thông | 15,85 | `work/bench/r4_voices/vi_female_huong.mp3` |

Các giọng "Review Phim" (`multi_female_*_uranus_bigtts`) và `vi-VN-HoaiMyNeural` đều bị CapCut trả `status: failed` với client hiện tại. Dòng giọng bigtts nhiều khả năng cần payload khác.

## Phát hiện

| ID | Tác động | Loại | Bằng chứng | Đề xuất | Lợi ích ước tính | Rủi ro |
|---|---|---|---|---|---|---|
| R4-1 | **Cao** | Chất lượng (độ dài) | `review.tts_cps=15` trong preset, nhưng CapCut đo được 19,7–20,6 ký tự/giây (trừ `vi_female_huong` 15,9). Đây là một nửa nguyên nhân khiến recap chỉ đạt 49% mục tiêu (xem R3-2). | Đặt `review.tts_cps` (và `series_recap.tts_cps` nếu dùng) bằng tốc độ đo được của giọng đã chọn; ghi chú tốc độ từng giọng ngay trong preset. | Độ dài thực tế khớp kế hoạch | Thấp |
| R4-2 | Quyết định của bạn | Chất lượng | 4 giọng dùng được (bảng trên). | Nghe mẫu và chọn giọng. | | |
| R4-3 | Thấp | Chất lượng / sync | Khoảng lặng đầu/cuối clip chỉ khoảng 0,11 s + 0,15 s mỗi beat (trans-zh đo 0,55 s trên AI33). | **Không sửa:** cả phim chỉ lợi khoảng 5 giây. | | |
| R4-4 | — | Kiểm tra | Loudness mỗi beat −14,7 đến −15,2 LUFS, 48 kHz; không có lỗi sample rate như bug 96 kHz trước đây. | Không cần sửa. | | |
| R4-5 | — | Tốc độ / chi phí | CapCut tổng hợp 19.229 ký tự trong 87,8 s, miễn phí (AI33 mất vài phút và tốn credit). | Giữ CapCut. | | |
| R4-6 | Thấp | Độ bền | CapCut là API riêng phải ký request; có thể hỏng bất cứ lúc nào. | Giữ AI33/VieNeu làm lựa chọn tường minh trong config (đang có). | | |

## Kết quả sau khi sửa (phim đầy đủ, `runs/bench-qods-r4`)

Giữ giọng `BV074_streaming`; `review.tts_cps` của các preset dùng CapCut đổi từ 15 thành 20,5. Recap dài **1.832 s so với mục tiêu 1.926 s (95%)**, baseline chỉ 49%. Tốc độ đọc thực tế trên cả phim là 21,6 ký tự/giây (đoạn mẫu đo 20,6), nên recap ngắn hơn mục tiêu khoảng 5%. TTS 39.620 ký tự mất 178 s.
