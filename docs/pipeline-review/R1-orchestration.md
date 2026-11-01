# R1 — Orchestration, DAG và runtime

Phạm vi: `run.py`, `orchestrator/`, `series_recap/__main__.py`, môi trường chạy. Số liệu lấy từ [00-baseline.md](00-baseline.md).

| ID | Tác động | Loại | Bằng chứng | Đề xuất | Lợi ích ước tính | Rủi ro |
|---|---|---|---|---|---|---|
| R1-1 | **Cao** | Độ tin cậy | Python 3.11 toàn cục dùng chung với project khác và đã lệch pin: `transformers` 5.16.1, OpenCV 5.0, Pillow 12.3, `huggingface-hub` 1.29. Hệ quả: `visual_index` crash, nhận diện mặt bị tắt âm thầm (0/1.527 shot). | Tạo venv riêng cho Recap bằng `uv` (`uv venv` + `uv pip install -e ".[movie-visual,dev,ui,tts-vieneu]"`), khóa phiên bản bằng `uv.lock`. Bổ sung cho `runtime_preflight`: kiểm phiên bản gói đã cài so với pin, sai thì fail ngay. | Không còn hỏng ngầm khi project khác nâng cấp gói; tính năng nhận diện mặt hoạt động lại | Cài lại bộ torch/CUDA tốn khoảng 5 GB đĩa; launcher/UI phải trỏ đúng Python của venv |
| R1-2 | TB | Tốc độ | `visual_index` (3,2 phút) chạy sau review+tts ([run.py:303-316](../../run.py:303)), dù chỉ phụ thuộc `shots` (xong ở phút 8,9). | Khởi chạy `visual_index` ngay khi `shots` xong **và** ASR đã trả VRAM (phụ thuộc R2-3), song song với dịch/vision/GĐ2. | −3,2 phút cho mỗi phim (khoảng −4% tổng thời gian) | Tranh VRAM với ASR nếu R2-3 chưa làm; phải chặn theo trạng thái GPU |
| R1-3 | TB | Độ tin cậy / vận hành | Run lỗi thì không ghi `summary.json`, nên mất thời gian của mọi stage (baseline phải dựng lại từ mtime). Log các stage ghi nối tiếp vào cùng `run.log`, không có timestamp hay dấu phân cách giữa các lần resume. | Luôn ghi `summary.json` (kể cả khi lỗi) kèm `started_at`/`finished_at` từng stage. Tách log theo stage (`logs/<stage>.log`), mỗi dòng có timestamp. | Debug và đo hiệu năng dễ hơn nhiều | Thấp |
| R1-4 | TB (ước tính) | Tốc độ | Season xử lý 12 tập tuần tự hoàn toàn ([series_recap/__main__.py:1220](../../series_recap/__main__.py:1220)): `ingest` (GPU) rồi `shots` (CPU) của từng tập. | Pipeline hai làn: chạy `shots` của tập N song song với `ingest` của tập N+1, tối đa 1 job GPU tại một thời điểm. | Tiết kiệm khoảng thời gian shots × (N−1), ước tính 15–25 phút mỗi season 12 tập | Chưa đo được vì chưa có bộ season mẫu; log song song khó đọc hơn |

**Thứ tự đề xuất:** R1-1 trước (nó gây ra các lỗi đã thấy ở R2/R5), rồi R1-3; R1-2 làm sau R2-3; R1-4 chờ có dữ liệu season.
