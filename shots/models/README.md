# Bundled models

| File | Source | License | SHA-256 |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | [opencv/opencv_zoo `models/face_detection_yunet`](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | MIT | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |

YuNet is the default GĐ4 face detector (`cv2.FaceDetectorYN`, OpenCV ≥ 4.5.4). On a 24-shot sample from
"Quý Ông Số Đỏ" it found faces in 6/12 bright shots where Haar found none (all six visually contained faces,
no false positives on face-free shots), and in 10/12 shots Haar had flagged. Haar remains the fallback.
