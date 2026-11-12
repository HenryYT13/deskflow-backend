# DeskFlow Backend - Nhật ký thay đổi (Changelog)

Toàn bộ lịch sử các bản phát hành và cập nhật quan trọng của máy chủ dịch vụ DeskFlow Backend.

---

## [v1.0.6] - 2026-11-12

### 🐛 Sửa lỗi (Fixes)
- **`POST /api/focus/enable` từ chối `{"enabled": false}` bằng 409 `challenge_required`.**
  - Endpoint này chỉ dùng để BẮT ĐẦU phiên, nhưng trước đây bỏ qua cờ `enabled` nên mọi yêu cầu "dừng" của app desktop lại tạo phiên mới. Hệ quả: giải đúng bài toán xong cửa sổ vẫn bị khoá lại sau 10 giây.
  - Giờ trả lỗi rõ ràng và **không thay đổi state** (version giữ nguyên), buộc client đi qua `/api/focus/challenge` + `/api/focus/verify`.
- Thêm regression test `test_enable_refuses_stop_request_and_keeps_state`.

---

## [v1.0.5] - 2026-11-12

### ✨ Thay đổi (Changes)
- **`/api/app/control/lamp/dim` chỉ còn điều khiển vòng LED.** Bỏ hẳn tham số `screen` (đèn nền ST7789 nối cứng 3.3V, không PWM được). Bản cũ vẫn gửi `screen` thì backend bỏ qua an toàn, không trả lỗi.
- **`/api/app/control/led` nhận thêm `on` và `off`** (trước chỉ `toggle` / `blink`) để app desktop bật lại đèn sau khi tắt.
- **Morse mặc định 60 ms / 1000 Hz** ở cả `alarm_engine.play_morse`, `station_control.play_morse` và route `/api/app/control/morse/play` (giá trị sai lệch vẫn được kẹp về mặc định mới).
- **Đồng bộ âm lượng Spotify:** `set_spotify_volume()` phát `{"action":"spotify_volume","volume":N}` xuống `deskflow/display`; route `/api/app/spotify/volume` và menu phần cứng ESP32 (`volume_up` / `volume_down`) đều gọi hàm này để OLED và mọi app luôn khớp.
- **`spotify_service.get_current_volume()`** đọc `device.volume_percent`; payload `spotify_update` gửi kèm `volume` để trạm hiển thị đúng.
- **Discord:** `/lamp_dim` chỉ còn tham số `led`, thêm `/lamp_on`; `/lamp_off` dùng action `off`.

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
