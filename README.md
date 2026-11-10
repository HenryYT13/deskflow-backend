# DeskFlow Unified Backend

Hệ thống Backend đồng bộ trung tâm của hệ sinh thái **DeskFlow**, phục vụ điều khiển tập trung (**Focus Mode**), thiết bị bàn học thông minh (**ESP32 Station**), tiện ích mở rộng trình duyệt (**Browser Extension**), và các ứng dụng đa nền tảng (**Android, Windows, Linux, macOS**).

Hệ thống kết hợp ba thành phần chính:
- **REST API (aiohttp)**: Cung cấp API quản lý phiên tập trung, đồng bộ thiết bị, giải toán mở khoá, ghi chú, báo thức và Spotify.
- **Discord Bot (discord.py)**: Điều khiển từ xa qua nhóm lệnh `/app` và các tiện ích học tập.
- **MQTT Bridge (paho-mqtt)**: Giao tiếp thời gian thực 2 chiều với trạm phần cứng ESP32.

---

## 🏗️ Kiến trúc & Tính năng

```text
               +---------------------------+
               |     Discord Slash Bot     |
               |         (/app ...)        |
               +-------------+-------------+
                             |
+--------------------+       v       +----------------------+
|  Native Clients    | <===========> |     DeskFlow Core    | <===========> MQTT Broker (Mosquitto)
|  (Android, Win,    |   REST API    |   - Focus Engine     |                     |
|   Linux, macOS,    |  (Port 8080)  |   - Auth & Pairing   |                     v
|   Browser Ext)     |               |   - Math Challenge   |            ESP32 Station
+--------------------+               +----------------------+           (OLED, LED, Buzzer)
```

### 1. Quản lý chế độ tập trung (Focus Mode & Anti-Tamper)
- Kích hoạt chế độ tập trung chặn web gây xao nhãng đồng bộ trên tất cả thiết bị.
- Hệ thống **Math Challenge** chống gian lận: Yêu cầu giải bài toán ngẫu nhiên trước khi cho phép tắt Focus Mode.

### 2. Ghép nối thiết bị (Device Pairing)
- Ghép nối an toàn qua mã OTP 8 ký tự hoặc mã QR do lệnh Discord `/app pair` sinh ra.
- Cấp Bearer Token để client gọi API xác thực.
- Hỗ trợ đổi tên thiết bị qua API `PATCH /api/devices/<id>`.

### 3. Nhạc chuông Melody & Trạm ESP32
- Điều khiển phát nhạc melody đơn âm định dạng MIDI trên còi buzzer trạm ESP32 (`melody_play`, `melody_stop`).
- Đồng bộ thông tin Spotify (bài hát, nghệ sĩ, thanh thời gian, lời bài hát).
- Hẹn giờ Pomodoro đếm ngược và báo thức thông minh.

---

## 🚀 Cài đặt & Khởi chạy

### Yêu cầu hệ thống
- Python 3.10 trở lên
- MQTT Broker (Mosquitto hoặc EMQX)
- Discord Bot Application Token (nếu sử dụng bot)

### Cài đặt thư viện
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Cấu hình biến môi trường (`.env`)
Tạo file `.env` tại thư mục gốc với các thông số cấu hình:
```ini
# Server Configuration
PORT=8080
HOST=0.0.0.0
DATA_DIR=data

# Discord Bot
DISCORD_TOKEN=your_discord_bot_token_here
OWNER_USER_ID=your_discord_user_id

# MQTT Broker (ESP32 Station)
MQTT_BROKER=localhost
MQTT_PORT=1883
MQTT_USER=
MQTT_PASSWORD=

# Topics
TOPIC_TIMER=deskflow/timer
TOPIC_DISPLAY=deskflow/display
TOPIC_ALARM=deskflow/alarm
TOPIC_STATION=deskflow/station

# Spotify (Tuỳ chọn)
SPOTIPY_CLIENT_ID=
SPOTIPY_CLIENT_SECRET=
SPOTIPY_REDIRECT_URI=http://localhost:8888/callback
```

### Chạy Backend
```bash
python3 src/main.py
```

### Chạy kiểm thử tự động
```bash
for f in tests/focus_mode/test_*.py tests/test_*.py; do
  PYTHONPATH=.:src python3 "$f"
done
```

---

## 🤖 Danh sách lệnh Discord Slash Commands

Tất cả lệnh điều khiển Focus Mode đã được gom về nhóm lệnh chuẩn `/app`:

| Lệnh | Mô tả |
|---|---|
| `/app on [duration] [note]` | Bật chế độ Focus Mode trên toàn bộ thiết bị liên kết |
| `/app off` | Yêu cầu bài toán thử thách để tắt Focus Mode |
| `/app answer <solution>` | Nộp đáp án bài toán để xác nhận tắt Focus Mode |
| `/app status` | Kiểm tra trạng thái tập trung và thời gian còn lại |
| `/app pair` | Tạo mã OTP 8 ký tự dùng ghép nối app/extension mới |
| `/app devices` | Liệt kê danh sách tất cả các thiết bị đã liên kết |
| `/app revoke <device_id>` | Huỷ liên kết và thu hồi token của thiết bị |
| `/app language <vi\|en>` | Đổi ngôn ngữ hiển thị thông báo |
| `/timer <duration> [note]` | Hẹn giờ đếm ngược Pomodoro |
| `/ki_thi <tên> <DD/MM>` | Đặt bộ đếm ngược kỳ thi hiển thị lên trạm ESP32 |

---

## 📡 API Endpoints Tham chiếu

### Thiết bị & Ghép nối
- `POST /api/focus/pair` — Đổi mã OTP lấy Device Token.
- `GET /api/devices` — Danh sách thiết bị đã đăng ký (yêu cầu Bearer Token).
- `PATCH /api/devices/<id>` — Đổi tên thiết bị (Body: `{"name": "Laptop Học Tập"}`).
- `DELETE /api/devices/<id>` — Huỷ ghép nối thiết bị.

### Trạng thái & Thử thách
- `GET /api/focus/status` — Lấy trạng thái Focus Mode hiện tại.
- `POST /api/focus/start` — Kích hoạt phiên Focus.
- `POST /api/focus/challenge` — Lấy bài toán giải đố để tắt Focus.
- `POST /api/focus/deactivate` — Nộp đáp án mở khoá.

### Điều khiển Trạm & Ghi chú
- `GET /api/app/control/notes` / `POST` / `DELETE /<id>` — Quản lý giấy nhớ đồng bộ.
- `GET /api/app/control/alarms` / `POST` / `DELETE /<id>` — Quản lý báo thức.
- `POST /api/app/control/melody/play` — Phát bản nhạc melody trên ESP32.
