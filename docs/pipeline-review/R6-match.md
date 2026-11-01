# R6 — GĐ5 match

Phạm vi: `match/`, `series_match/`. Số liệu: phim đầy đủ sau R1–R4 (`runs/bench-qods-r4`), 62 beat, 524 placement.

| Chỉ số | Giá trị | Đánh giá |
|---|---|---|
| Sync QA | 0 khoảng hở, 0 chồng lấn, 0 cảnh báo (baseline có 2 `source_order_mismatch`) | Tốt |
| Lệch nguồn (source drift) | trung vị 2,8 s, p90 9,5 s, tối đa 101 s; 4 beat có cảnh báo drift cao | Tốt |
| Dùng lại footage | 1 lần, cộng 1 lần lặp có kiểm soát (beat 34) | Tốt |
| Clip | trung bình 3,5 s; 0 beat phải dùng shot tối | Đúng quyết định đã khóa (3–5 s) |
| Mở đầu | 6 cảnh báo `opening_ordered_fill` (chỉ là thông tin); không còn logo | Tốt |
| Thời gian | 72 s, BGE-M3 trên CUDA | Không phải điểm nghẽn |

## Phát hiện

| ID | Tác động | Loại | Bằng chứng | Đề xuất |
|---|---|---|---|---|
| R6-1 | Thấp | Chất lượng | 4 beat có drift cao (tối đa 101 s); với narration dày đặc, đây là mức chấp nhận được. | Không sửa. Nếu khi xem video thấy hình lệch lời ở các beat này thì xử lý cục bộ, không đổi audio delay toàn cục. |
| R6-2 | — | Phụ thuộc R5-2 | `w_face=0.18` đang dựa trên Haar bỏ sót khoảng một nửa số mặt. | Giải quyết ở R5-2. |

**Kết luận R6:** GĐ5 đang tốt sau khi R2 (vision) và R3 (beat ngắn hơn) cải thiện dữ liệu đầu vào. Không đề xuất thay đổi code riêng cho GĐ5.
