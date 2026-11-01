# R3 — GĐ1.5 storymap và GĐ2 review

Phạm vi: `storymap/`, `review/`. Số liệu lấy từ baseline cả phim và từ A/B model trên đoạn 900 giây (`work/bench/r3_ab`, mỗi model chạy một lần trên cùng `film_map`).

## A/B model qua 9router (đoạn 900 giây)

| Model | Thời gian | Beat | Đạt ngân sách ký tự | QA | Request / token vào |
|---|---:|---:|---:|---|---|
| `heavy` (combo, `claude-sonnet-4-6` trả lời) | **117 s** | 9 | 99% | 2 issue → pass | 3 / 24k |
| `ag/gemini-3.8-flash-high` | 310 s | 7 | 101% | 3 → 0, pass | 7 / 82k |
| `ag/gemini-3.1-pro-high` | lỗi | | | | 404 theo tài khoản, rồi hết ngân sách; giống Opus |

**Đọc mẫu** (beat 0–1): `heavy` viết có không khí nhưng chung chung ("một thám tử", "một tên tội phạm", "một nhân vật bỗng giơ dao"), không có tên nhân vật và có thể nhầm vai. Gemini Flash dùng tên (Ji Hyun Soo), mạch nhân quả rõ ("yêu cầu được gọi cho luật sư", "hành xử như một quý ông"), khớp chủ đề phim hơn. Mỗi model chỉ chạy một lần, nên đây là tín hiệu định tính, chưa phải kết luận thống kê.

## Phát hiện

| ID | Tác động | Loại | Bằng chứng | Đề xuất | Lợi ích ước tính | Rủi ro |
|---|---|---|---|---|---|---|
| R3-1 | **Cao** | Chất lượng (độ dài) | Phim 2 giờ chỉ có 18 beat, mục tiêu trung bình **1.586 ký tự/beat**; bản nháp chỉ đạt **60%** (16/18 beat dưới 80%). Model gần như luôn viết 700–1.350 ký tự/beat. Đoạn 900 giây có mục tiêu 375 ký tự/beat thì đạt 99%. | Outline prompt nêu số beat cần có, N ≈ ngân sách ký tự / 650 (khoảng 30 s audio mỗi beat; phim 2 giờ được khoảng 45 beat). Sau narration, thêm một vòng viết lại có giới hạn cho các beat dưới 75% mục tiêu. | Độ dài narration gần mục tiêu; beat ngắn hơn cũng giúp GĐ5 khớp hình sát hơn | Nhiều beat hơn thì nhiều request hơn (khoảng 5–6 batch narration); ngân sách tài khoản Antigravity cạn nhanh hơn |
| R3-2 | **Cao** | Chất lượng (độ dài) | `review.tts_cps=15`, trong khi CapCut đọc khoảng 20,5 ký tự/giây, nên ngay cả khi viết đủ, recap vẫn ngắn hơn khoảng 27%. Cộng với R3-1: baseline chỉ đạt **49%** độ dài mục tiêu (0,67 × 0,73). | Đặt `tts_cps` theo giọng đã chọn ở R4. | Độ dài thực tế khớp kế hoạch | Ngân sách ký tự tăng thêm khoảng 37%, nên phải làm cùng R3-1 |
| R3-3 | TB | Chất lượng | QA cả phim không hội tụ: 19/19/29/18 issue, vòng cuối chưa pass. Mỗi vòng chỉ viết lại tối đa 6 beat ([review/__main__.py:397](../../review/__main__.py:397)). Phần lớn là `accuracy` (12–25 issue mỗi vòng), do thiếu mô tả hình nên model tự điền bối cảnh. Đoạn 900 giây sau khi vision chạy được: 2 issue → pass. | Sau R2-1 và R3-1, nâng giới hạn viết lại theo số beat: `max(6, beats // 3)`. Đo lại cả phim trước khi làm gì thêm. | QA pass hoặc ít issue tồn đọng hơn | Thêm thời gian GĐ2 (mỗi lần viết lại 10–20 s) |
| R3-4 | TB | Độ bền | Hai lần GĐ2 bị dừng hẳn chỉ vì một lần viết lại beat (bước tùy chọn) trả JSON sai schema. | Ở các bước viết lại theo QA/style/opening, lỗi parse hoặc validate thì bỏ qua lần viết lại đó, giữ beat cũ và ghi cảnh báo, giống cách đang xử lý lỗi transport. Outline và narration vẫn fail rõ ràng. | GĐ2 không chết vì một lần viết lại hỏng | Một beat có thể giữ nguyên lỗi QA, nhưng có cảnh báo ghi lại |
| R3-5 | Quyết định của bạn | Chất lượng / tốc độ | Xem bảng A/B ở trên. | **Đề xuất dùng `ag/gemini-3.8-flash-high` cho GĐ2** vì dùng đúng tên và đúng mạch truyện, chấp nhận chậm hơn khoảng 2,7 lần. Giữ `heavy` làm phương án dự phòng (đổi qua `review.llm_model`). | Narration cụ thể và đúng nhân vật hơn | GĐ2 cả phim có thể mất khoảng 25–35 phút thay vì 15 phút |

**Không đề xuất sửa:** coverage dưới `min_coverage` hiện chỉ sinh cảnh báo. Đoạn 900 giây đạt 84%, mức hợp lý với một đoạn cắt.

## Kết quả sau khi sửa (phim đầy đủ, `runs/bench-qods-r4`, Gemini 3.8 Flash, `tts_cps=20.5`)

| Chỉ số | Baseline | Sau sửa |
|---|---|---|
| Số beat | 18 | 62 |
| Narration so với ngân sách ký tự | 67% (bản nháp 60%) | **100,3%** (0 beat cần mở rộng) |
| QA | 4 vòng 19/19/29/18 issue, chưa pass | **2 → 0, pass** |
| Coverage | 97,8% | 97,6% |
| GĐ2 | 14,6 phút, 28 request, 380k/42k token | 10,8 phút, 16 request, 418k/25k token |

Trạng thái: R3-1, R3-3, R3-4 đã sửa; R3-5 chuyển sang `ag/gemini-3.8-flash-high`.
