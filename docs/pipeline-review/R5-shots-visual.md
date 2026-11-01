# R5 — GĐ4 shots và GĐ4.5 visual index

Phạm vi: `shots/`, `visual_index/`. Số liệu: phim đầy đủ sau R1–R4 (`runs/bench-qods-r4`).

| ID | Tác động | Loại | Bằng chứng | Đề xuất | Lợi ích ước tính | Rủi ro |
|---|---|---|---|---|---|---|
| R5-1 | TB | Độ bền | `feature_cache_key` ([shots/__main__.py:120](../../shots/__main__.py:120)) chỉ hash cấu hình `face_detection: on`, không hash việc detector có thật sự chạy hay không (`face_detector_version` có được tính nhưng không đưa vào khóa). Lần chạy bị OpenCV 5 tắt nhận diện mặt sẽ để lại cache `face_count=0` cả sau khi môi trường đã được sửa. | Đưa `face_detector_version` (tên detector thực tế, `off` nếu bị tắt) vào khóa cache feature và vào `shots.meta.json`. | Môi trường sửa xong thì feature tự tính lại | Thấp; cache feature cũ bị tính lại một lần (khoảng 8 phút, chạy song song với ingest) |
| R5-2 | TB | Chất lượng | Haar chỉ nhận mặt nhìn thẳng. Trong 12 shot sáng mà Haar báo không có mặt, **ít nhất 6 shot có mặt rõ** (cận cảnh, góc 3/4). `w_face=0.18` là trọng số lớn thứ hai trong GĐ5, nên điểm thưởng này đang gán sai cho khoảng một nửa số shot có người. | Dùng `cv2.FaceDetectorYN` (YuNet, có sẵn trong OpenCV 4.14) làm detector mặc định, Haar làm dự phòng. Đo lại trên cùng mẫu để so recall. Cần tải `face_detection_yunet_2023mar.onnx` (~230 KB, MIT, từ opencv_zoo) và đặt vào repo. | Chọn đúng shot có người hơn | Phải tải model (cần bạn đồng ý); YuNet chậm hơn Haar một chút trên CPU |
| R5-3 | — | Kiểm tra | 32 shot end-credit đều nằm trong 6 phút cuối phim (7.029–7.406 s), đúng là credit. Đoạn 900 giây từng đánh dấu nhầm 2 shot, nhưng đó là vì guard xét 600 giây cuối của chính đoạn cắt. | Không cần sửa. | | |
| R5-4 | — | Tốc độ | shots mất 8,5 phút cho 1.527 shot, chạy song song với ingest (14,2 phút), nên không nằm trên đường găng. | Không cần sửa. | | |
| R5-5 | Thấp | Tốc độ / giá trị | Visual rerank (SigLIP2) chỉ chiếm trung vị **2%** điểm tổng hợp (p90 17%); shot được chọn có trung vị xếp hạng visual **435**. Thứ tự thời gian quyết định gần như toàn bộ. Visual index tốn khoảng 4 phút GPU cho mỗi phim, nhưng nhờ R1-2 đã nằm ngoài đường găng. | Giữ nguyên trong production (không tốn thời gian trên đường găng). Hiệu chỉnh lại `w_visual` cần bộ beat có gán nhãn, nên chỉ nên làm khi có nhu cầu. | | |

**Phát hiện cho R7:** một thumbnail có **dòng quảng cáo cờ bạc do site PhimMoi chèn thẳng vào video** ("Sản phẩm: Nổ Hũ, Game Bài… 24/7 nhận tiền…"). Xem thêm ở R7.

## Kết quả sau khi sửa (phim đầy đủ, `runs/bench-qods-r4`)

| ID | Trạng thái | Kết quả đo được |
|---|---|---|
| R5-1 | Đã sửa | Khóa cache feature và `shots.meta.json` (`model_versions.face_detector`) ghi detector thực sự nạp được. Orchestrator coi shots là cũ khi detector đó khác detector môi trường hiện tại nạp được: `run.py --only shots` không cần `--force` đã tự chạy lại sau khi đổi Haar sang YuNet (340 s). Tach cho phép `orchestrator → shots`. |
| R5-2 | Đã sửa | YuNet (`opencv-yunet-2023mar`, ngưỡng 0,7) là detector mặc định, Haar làm dự phòng; model đóng gói ở `shots/models/`. Shot có mặt người: **1.108/1.527** (Haar: 541). Xem 12 shot ngẫu nhiên chỉ YuNet nhận ra: cả 12 đều có mặt người thật. |
