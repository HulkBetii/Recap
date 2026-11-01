# R7 — GĐ6 render và độ sạch footage

Phạm vi: `render/`, các dữ liệu nguồn đi vào render (`shots/` thumbnail, overlay trên nguồn), `recap_ui/qa.py` (kiểm tra định dạng). Số liệu: phim đầy đủ sau R1–R5 (`runs/bench-qods-r4`): 524 placement, 1.832 s output, render mất **11,4 phút** (11:15:03 → 11:26:30), file 741 MB (3,2 Mbps).

Nguồn: H.264 High, 1920x804 (2,39:1), **25 fps**, 1,38 Mbps, GOP trung bình 2,7 s. Máy: RTX 3060, 12 luồng CPU, ffmpeg có `h264_nvenc`.

| ID | Tác động | Loại | Bằng chứng | Đề xuất | Lợi ích ước tính | Rủi ro |
|---|---|---|---|---|---|---|
| R7-1 | Cao | Tốc độ | Bảng A/B bên dưới: 24 placement thật (82,8 s output), 4 job song song. NVENC p5 cq24 cho SSIM **0,9957**, nhỉnh hơn libx264 medium crf20 (0,9955), bitrate thấp hơn 6% và **nhanh hơn 3,1 lần**. Giải mã bằng CUDA không giúp gì (libx264: 52,3 s so với 46,6 s), nên vẫn giải mã bằng CPU. | Thêm `render.encoder: auto\|libx264\|h264_nvenc` và `render.nvenc_cq` (mặc định 24). `auto` chọn NVENC khi ffmpeg có `h264_nvenc` và mã hóa thử 1 khung hình thành công, nếu không thì dùng libx264. Clip cắt và clip đệm cuối dùng chung một bộ tham số mã hóa, vì concat `-c copy` cần stream đồng nhất. Khóa cache temp clip có thêm encoder. | Render 11,4 → khoảng 4 phút mỗi phim | Thấp; máy không có GPU tự dùng libx264 |
| R7-2 | TB | Chất lượng | Nguồn 25 fps bị bộ lọc `fps=30` nhân đôi 1/5 khung hình: **16,7% khung hình output là bản lặp**, lặp 5 lần mỗi giây, gây giật nhịp khi lia máy hoặc khi có chuyển động. Phim Hàn và anime thường 23,976 fps nên cũng bị lặp khung hình theo nhịp không đều. | Cho phép `render.fps: source`: đo `avg_frame_rate` của nguồn. Với season, chỉ dùng khi mọi tập cùng fps; nếu khác nhau thì về 30 và ghi cảnh báo. Đặt `source` cho preset phim lẻ và anime thường. Preset anime enhanced giữ 30 vì smoke test của nó khóa khung hình ở 30. QA giao file ([recap_ui/qa.py:465](../../recap_ui/qa.py:465)) đang chặn mọi fps khác 30, sẽ chuyển sang so với fps ghi trong `render.meta.json`. | Hết giật nhịp; ít hơn 17% khung hình phải mã hóa | Thấp; khóa cache render và QA giao file phải đổi theo |
| R7-3 | TB | Chất lượng / chính sách | Quét toàn phim ở 1 fps: **quảng cáo cờ bạc "i999.ai… Nổ Hũ, Game Bài…" chèn cứng ở 176–201 s** (26 s, chỉ 1 lần; 3 điểm phát hiện lẻ khác đều là đèn vàng, tức báo nhầm). **3 placement (8,1 s output) đang lộ quảng cáo này trong recap.** Video quảng bá cờ bạc có rủi ro bị YouTube hạn chế kiếm tiền. Bản thử nghiệm: lấy các cạnh vừa đứng yên vừa giữ nguyên màu qua 3 shot khác hình, cách nhau ≥ 6 s. Hai bộ ba trong khoảng quảng cáo đạt 2.928 và 2.911 điểm ảnh; cao nhất ngoài khoảng đó là 201; p99 là 41. Hai nhóm tách nhau hơn 10 lần. | Thêm bước `overlay_marking` ở GĐ4 (có cache riêng, giống end-credit), chạy trên frame thumbnail đã có trong bộ nhớ. Shot nằm trong khoảng có overlay được đánh `is_usable=false`, `unusable_reasons=["overlay_text"]`; GĐ5 vốn đã loại cứng mọi lý do khác `too_dark`. Chống nhầm logo cố định: nếu hơn 30% bộ ba bị gắn cờ thì coi là watermark, không loại shot nào, chỉ ghi cảnh báo. | Không còn quảng cáo của site nguồn trong recap | Thấp–TB; cần thêm phim có/không có overlay để chỉnh ngưỡng |
| R7-4 | TB | Chất lượng | Phụ đề Việt in cứng xuất hiện trong **61,8%** thời lượng output (đo trên khung giữa của mọi placement). Phụ đề 2 dòng chiếm tới khoảng 26% chiều cao hình. Ảnh so sánh: làm mờ một dải cố định chỉ che được 1 dòng; muốn che đủ 2 dòng thì phải làm mờ một phần tư khung hình; zoom cắt bỏ thì vẫn lộ dòng trên và mất thêm nhiều chiều ngang. | Mặc định không che. Phát hiện tỷ lệ phụ đề cứng ở GĐ4 và ghi vào `shots.meta.json` cùng `summary.json`: nếu trên 20% thì cảnh báo "nên dùng bản raw không phụ đề". Tùy chọn (chỉ khi bạn muốn): `render.subtitle_mask: off\|blur` kèm chiều cao dải. | Biết sớm nguồn có phụ đề cứng để đổi nguồn | Thấp |
| R7-5 | Thấp–TB | Chất lượng | `fit=cover` trên nguồn 2,39:1 cắt **25,6%** chiều ngang. YuNet trên khung giữa của 524 placement: 505 khuôn mặt (rộng ≥ 40 px), trong đó 43 mặt (8,5%) bị cắt, 28 mặt mất quá nửa. **26 placement (5%)** có ít nhất một mặt bị cắt quá nửa. | (a) Giữ cover; AGENTS.md đang khóa "full-screen". (b) Cover theo mặt: GĐ4 ghi thêm `face_center_x` (trung bình có trọng số diện tích, trường tùy chọn); GĐ6 nhận `--shots` và dịch khung crop ngang theo mặt cho từng placement. (c) Letterbox: đổi quyết định đã khóa, không khuyến nghị. | (b): ít mặt bị cắt hơn ở khoảng 5% số placement | (b): thêm trường tùy chọn ở `Shot`, thêm tham số vào khóa cache render |
| R7-6 | TB | Lỗi | Tên file thumbnail trên đĩa bị lỗi font: `shots/QuÃ½ Ã”ng…-000.jpg`, trong khi `shots.json` trỏ tới `shots/Quý Ông…-000.jpg`. Nguyên nhân: `cv2.imwrite` trên Windows nhận đường dẫn theo ANSI ([shots/thumbs.py:24](../../shots/thumbs.py:24), [shots/thumbs.py:34](../../shots/thumbs.py:34)). Hậu quả: ảnh trong `edl.review.html` bị vỡ với mọi phim có tên tiếng Việt. Ghi keyframe ở [visual_index/frames.py:60](../../visual_index/frames.py:60) cũng gặp lỗi tương tự khi đường dẫn run-dir có dấu. | Ghi ảnh bằng `cv2.imencode` + `Path.write_bytes` ở cả 3 chỗ. | Thumbnail và trang review hoạt động đúng với tên có dấu | Không đáng kể |
| R7-7 | Thấp | Vận hành | Sau khi render thành công, `work/render` vẫn giữ `video_only.mp4` (723 MB, trùng với hình trong `recap.mp4`). Ổ D: hiện chỉ còn 4,4 GB trống. | Xóa `video_only.mp4` và `video_only_padded.mp4` sau khi mux và validate xong. Temp clip vẫn giữ lại để làm cache. | Tiết kiệm khoảng 0,7 GB mỗi lần chạy | Không đáng kể |

## A/B bộ mã hóa (R7-1)

Tham chiếu là bản lossless libx264 qp0 dùng đúng chuỗi filter. SSIM tính trên toàn bộ khung hình.

| Cấu hình | Thời gian (s) | Mbps | SSIM TB | SSIM min | × realtime |
|---|---|---|---|---|---|
| libx264 medium crf20 (hiện tại) | 46,6 | 3,23 | 0,99553 | 0,99122 | 1,78 |
| libx264 medium crf20 + giải mã CUDA | 52,3 | 3,23 | 0,99553 | 0,99122 | 1,59 |
| libx264 faster crf20 | 37,7 | 3,36 | 0,99550 | 0,99086 | 2,20 |
| libx264 veryfast crf19 | 23,1 | 3,18 | 0,99535 | 0,99012 | 3,59 |
| **NVENC p5 cq24** | **15,2** | **3,03** | **0,99570** | **0,99199** | **5,45** |
| NVENC p5 cq25 (giải mã CPU) | 12,7 | 2,62 | 0,99538 | 0,99123 | 6,52 |
| NVENC p5 cq21 | 14,5 | 4,71 | 0,99647 | 0,99347 | 5,72 |
| NVENC p7 cq19 | 18,1 | 5,90 | 0,99689 | 0,99440 | 4,58 |

Tham số NVENC: `-preset p5 -tune hq -rc vbr -cq 24 -b:v 0 -bf 3`.

## Kết quả sau khi sửa (phim đầy đủ, `runs/bench-qods-r4`)

Chạy lại `shots → visual_index → match → render` với `config.movie.production.yaml`; ingest, review và TTS dùng cache.

| Chỉ số | Trước | Sau |
|---|---|---|
| Thời gian render | 686,5 s (11,4 phút) | **225,9 s (3,8 phút), −67%** |
| Encoder / fps | libx264 medium crf20, 30 fps (16,7% khung lặp) | `h264_nvenc` p5 cq24, **25 fps theo nguồn** (45.811 khung) |
| File output | 741 MB, 3,2 Mbps | 685 MB, 2,9 Mbps |
| Placement lộ quảng cáo i999.ai | 3 (8,1 s) | **0** |
| Đồng bộ | `duration_match` đúng; 0 khoảng hở, 0 chồng lấn | Như trước: 1.832,5 s video = audio |
| `work/render` sau khi chạy | 692 MB temp clip + 723 MB `video_only.mp4` | 640 MB temp clip (đã dọn 524 clip cũ); không còn `video_only.mp4` |

| ID | Trạng thái | Ghi chú |
|---|---|---|
| R7-1 | Đã sửa | `render.encoder: auto` (preset), CLI mặc định `libx264` để giữ tương thích. Khóa cache temp clip của libx264 không đổi. |
| R7-2 | Đã sửa | `render.fps: source` trong 12 preset; `config.anime.series.vieneu.enhanced.yaml` giữ 30. Rate NTSC ghi dạng `24000/1001`. QA giao file so với fps trong `render.meta.json`. |
| R7-3 | Đã sửa | GĐ4 `overlay_guard` (mặc định bật): 12 shot ở 174,4–205,3 s bị loại (gồm 1 shot đệm mỗi bên); không báo nhầm ở 1.001 bộ ba shot còn lại; mất 109 s, chạy song song với ingest. |
| R7-4 | Đã sửa (chỉ cảnh báo) | `shots.meta.json`: `hardsub_ratio=0.455` cùng cảnh báo "burned-in subtitles in 45% of shot time; prefer a clean (raw) source". |
| R7-5 | Giữ nguyên | Giữ `fit=cover` theo quyết định đã khóa. |
| R7-6 | Đã sửa | Thumbnail và keyframe ghi bằng `common.media.write_image`. Run cũ có tên lỗi font sẽ tự sinh lại thumbnail khi GĐ4 dựng lại `shots.json`. |
| R7-7 | Đã sửa | Ngoài `video_only*.mp4`, GĐ6 còn xóa temp clip không dùng trong lần render vừa xong (trước đây clip của các lần render cũ tích lũy mãi). |

## Không cần sửa

- Concat `-c copy` và mux chỉ mất vài giây. Gần như toàn bộ 11,4 phút là thời gian cắt và mã hóa temp clip.
- GOP nguồn ngắn (tối đa 4 s), nên seek bằng `-ss` trước `-i` không giải mã thừa nhiều.
