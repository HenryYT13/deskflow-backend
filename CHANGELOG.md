# DeskFlow Backend - Nhật ký thay đổi (Changelog)

Toàn bộ lịch sử các bản phát hành và cập nhật quan trọng của máy chủ dịch vụ DeskFlow Backend.

---

## [v1.0.4] - 2026-11-11

### ✨ Tính năng mới (New Features)
- **API Xóa thiết bị vĩnh viễn:**
  - Bổ sung `DELETE /api/focus/devices/{device_id}` và `POST /api/focus/devices/{device_id}/delete` cho phép xóa triệt để thiết bị khỏi cơ sở dữ liệu `focus_mode.json`.
  - Cập nhật `handle_unpair` cho phép cả phạm vi `focus` và `control` thực hiện hủy liên kết và xóa thiết bị.
  - Tự động dọn dẹp các thiết bị đã bị thu hồi (revoked) trong cơ sở dữ liệu.
- **Hỗ trợ phát Preset Melodies linh hoạt:**
  - Endpoint `POST /api/app/control/melody/play` chấp nhận linh hoạt các khóa `preset_id`, `song` hoặc `preset`.
  - Phục vụ danh sách bài mẫu 8-bit (*Mario*, *Rickroll*, *Tetris*, *Coffin Dance*, *Star Wars*, *Nokia Tune*) qua `GET /api/app/control/melody/presets`.

### 🐛 Vá lỗi & Cải thiện (Bug Fixes & Improvements)
- **Đồng bộ điều chỉnh độ sáng đèn (Dimming Control):**
  - Cập nhật `set_lamp_dim` trong `station_control.py` để lưu trữ và duy trì `led_brightness` trong cấu hình trạm `station_settings.json`.
  - Bổ sung cả `led_bright` và `led_brightness` trong gói tin phát sóng MQTT `deskflow/display`.
- **Cấp quyền mặc định khi ghép nối bằng mã code:**
  - Hàm `generate_pairing_code` gán mặc định phạm vi `["focus", "control", "spotify"]` để các thiết bị ghép nối bằng mã có đầy đủ thẩm quyền điều khiển trạm và quản lý thiết bị.
