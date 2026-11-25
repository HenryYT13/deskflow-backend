import os
import sys
import json
import time
import psutil
import logging
import asyncio
import random
import unicodedata
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Any, Dict, List
import discord
from discord import app_commands
import paho.mqtt.client as mqtt
import httpx
from groq import Groq
from pocketbase import PocketBase

import config
from timetable import get_processed_daily_tkb, get_dual_day_tkb
import scheduler
import spotify_service
import focus_mode
from alarm_engine import alarm_engine
import station_control as station_control_module
from station_control import station_control

# Múi giờ Việt Nam (UTC+7 / ICT)
VN_TZ = timezone(timedelta(hours=7))

# ================= CẤU HÌNH HỆ THỐNG GHI LOG TỰ ĐỘNG =================
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

class DailyNamedFileHandler(logging.FileHandler):
    def __init__(self, log_dir, encoding="utf-8"):
        self.log_dir = log_dir
        self.current_date_str = datetime.now(VN_TZ).strftime("%d-%m-%y")
        filename = os.path.join(self.log_dir, f"deskflow-log-{self.current_date_str}.log")
        super().__init__(filename, encoding=encoding)

    def emit(self, record):
        now_date_str = datetime.now(VN_TZ).strftime("%d-%m-%y")
        if now_date_str != self.current_date_str:
            self.current_date_str = now_date_str
            self.close()
            self.baseFilename = os.path.join(self.log_dir, f"deskflow-log-{self.current_date_str}.log")
            self.stream = self._open()
        super().emit(record)

logger = logging.getLogger("DeskFlow")
logger.setLevel(logging.INFO)

log_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

file_handler = DailyNamedFileHandler(LOG_DIR, encoding="utf-8")
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

logger.info("=== HỆ THỐNG DESKFLOW SERVER KHỞI ĐỘNG ===")

# ================= KHỞI TẠO CLIENT NGOÀI =================
pb_url = getattr(config, "POCKETBASE_URL", "http://127.0.0.1:8090")
pb = PocketBase(pb_url)
logger.info(f"[PocketBase] Đã liên kết client tại: {pb_url}")

groq_key = getattr(config, "GROQ_API_KEY", None)
groq_client = Groq(api_key=groq_key) if groq_key else None
if groq_client:
    logger.info("[Groq AI] Khởi tạo Groq Client thành công.")
else:
    logger.warning("[Groq AI] Không tìm thấy GROQ_API_KEY trong cấu hình.")

# ================= QUẢN LÝ TRẠNG THÁI HỆ THỐNG =================
device_status = {
    "battery_pct": 100,
    "voltage": 4.12,
    "wifi_rssi": -55,
    "spotify_track": "Chưa phát nhạc",
    "spotify_artist": "N/A",
    "weather": "28°C Nắng nhẹ",
    "math_active": False,
    "current_math_answer": None
}

device_settings = {
    "darkmode_mode": "off",
    "darkmode_start": "22:00",
    "darkmode_end": "06:00",
    "led_brightness": 100,
    "buzzer_enabled": True,
    "buzzer_duration": 40,
    "weather_alert_enabled": True,
    "weather_alert_time": "06:15",
    "weather_city": "Ha Long",
    "battery_low_thresh": 30,
    "battery_high_thresh": 80,
    "quote_schedules": ["06:30", "12:00", "20:00"],
    "clock_style": "digital_neon",
    "hardware_config_version": 1,
    "applied_by_station": False,
    "last_ack_error": None
}

laptop_status = {
    "cpu": 0.0,
    "ram": 0.0,
    "temp": 0.0,
    "battery": 0,
    "last_seen": 0
}

current_screen_mode = "main"

def clean_ascii(text: str) -> str:
    if not text:
        return ""
    text = text.replace("đ", "d").replace("Đ", "D")
    nfd = unicodedata.normalize('NFD', text)
    return "".join(c for c in nfd if unicodedata.category(c) != 'Mn')

def get_server_metrics():
    # Shared cached sampler: psutil.cpu_percent(interval=None) returns 0.0 and
    # even a 0.1s window is too short on the production KVM guest.
    cpu_pct = station_control_module.get_cpu_percent()
    ram = psutil.virtual_memory()
    uptime_seconds = int(time.time() - psutil.boot_time())
    uptime_hours = uptime_seconds // 3600
    uptime_mins = (uptime_seconds % 3600) // 60

    return {
        # Clamp so a bad psutil reading (e.g. 441%) can never reach the clients.
        "cpu": max(0.0, min(100.0, round(cpu_pct, 1))),
        "ram_pct": max(0.0, min(100.0, round(ram.percent, 1))),
        "ram_used_mb": int(ram.used / (1024 * 1024)),
        "ram_total_mb": int(ram.total / (1024 * 1024)),
        "uptime": f"{uptime_hours}h {uptime_mins}m"
    }

async def build_tkb_payload():
    try:
        dual_tkb = await get_dual_day_tkb()
        if not dual_tkb or not dual_tkb.get("today"):
            return None

        today = dual_tkb["today"]
        tom = dual_tkb.get("tomorrow", {})

        today_name = today.get("day", "Thứ 4")

        clean_morning = [s.split(": ")[-1].strip() for s in today.get("morning", []) if s.strip()]
        clean_afternoon = [s.split(": ")[-1].strip() for s in today.get("afternoon", []) if s.strip()]
        has_afternoon = bool(today.get("has_afternoon", False)) and len(clean_afternoon) > 0

        dp_sang = today.get("dp_sang", "Tự do")
        dp_chieu = today.get("dp_chieu", "")

        if has_afternoon:
            if dp_chieu and dp_chieu != dp_sang:
                uniform_str = f"Sáng: {dp_sang}, Chiều: {dp_chieu}"
            else:
                uniform_str = f"{dp_sang} (chung cả ngày)"
        else:
            uniform_str = dp_sang

        clean_tom_m = [s.split(": ")[-1].strip() for s in tom.get("morning", []) if s.strip()]
        clean_tom_a = [s.split(": ")[-1].strip() for s in tom.get("afternoon", []) if s.strip()]
        tom_has_afternoon = bool(tom.get("has_afternoon", False)) and len(clean_tom_a) > 0
        tom_dp_sang = tom.get("dp_sang", "Tự do")
        tom_dp_chieu = tom.get("dp_chieu", "")

        if tom_has_afternoon:
            if tom_dp_chieu and tom_dp_chieu != tom_dp_sang:
                tom_uniform_str = f"Sáng: {tom_dp_sang}, Chiều: {tom_dp_chieu}"
            else:
                tom_uniform_str = f"{tom_dp_sang} (chung cả ngày)"
        else:
            tom_uniform_str = tom_dp_sang

        tom_day_label = tom.get("display_day", tom.get("day", "Ngày mai"))

        return {
            "action": "sync_data",
            "view": "tkb",
            "day_name": today_name,
            "morning": ", ".join(clean_morning) if clean_morning else "Nghỉ học",
            "has_afternoon": has_afternoon,
            "afternoon": ", ".join(clean_afternoon) if has_afternoon else "",
            "uniform": uniform_str,
            "tom_day": tom_day_label,
            "tom_morning": ", ".join(clean_tom_m) if clean_tom_m else "Nghi hoc",
            "tom_has_afternoon": tom_has_afternoon,
            "tom_afternoon": ", ".join(clean_tom_a) if tom_has_afternoon else "",
            "tom_uniform": tom_uniform_str
        }
    except Exception as e:
        logger.error(f"[TKB Build Error] {e}")
        return None

def check_title_with_groq(page_title: str) -> bool:
    if not groq_client:
        return True
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Bạn là hệ thống kiểm soát Pomodoro học tập. "
                        "Nhiệm vụ: Nhận tiêu đề trang web và xác định nó có phục vụ cho học tập, nghiên cứu, lập trình, làm bài tập hay không. "
                        "Chỉ trả lời đúng 1 từ: 'ALLOW' (nếu là học tập/làm việc) hoặc 'BLOCK' (nếu là mạng xã hội, giải trí, game, video nhảm, mua sắm...)."
                    )
                },
                {"role": "user", "content": f"Tiêu đề web: '{page_title}'"}
            ],
            temperature=0.0,
            max_completion_tokens=1024
        )
        ans = completion.choices[0].message.content.strip().upper()
        logger.info(f"[Pomodoro Audit] Title: '{page_title}' -> Verdict: {ans}")
        return "ALLOW" in ans
    except Exception as e:
        logger.error(f"[Pomodoro Audit Error]: {e}")
        return True

def generate_groq_quote(topic: str = "học tập và kiên trì", max_chars: int = 75) -> str:
    """Sinh danh ngôn từ Groq AI dùng openai/gpt-oss-20b"""
    fallback_quote = "Kien tri hom nay, thanh cong ngay mai!"
    if not groq_client:
        logger.warning("[Groq AI] groq_client chưa được khởi tạo, dùng fallback.")
        return fallback_quote[:max_chars]

    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system", 
                    "content": (
                        "Bạn là trợ lý tạo động lực DeskFlow. Hãy viết đúng 1 câu danh ngôn ngắn gọn tạo cảm hứng bằng tiếng Việt "
                        f"(tối đa {max_chars} ký tự). Chỉ trả về nội dung câu nói, không dùng ngoặc kép, không thêm lời dẫn."
                    )
                },
                {"role": "user", "content": f"Chủ đề: {topic}"}
            ],
            temperature=0.7,
            max_completion_tokens=1024,  # Đủ chỗ cho cả reasoning tokens và text content
            reasoning_effort="low"       # Giảm bớt token suy nghĩ để trả về kết quả nhanh & không bị nuốt chữ
        )

        quote = completion.choices[0].message.content
        if not quote:
            return fallback_quote[:max_chars]

        quote = quote.strip().strip('"').strip("'")
        if len(quote) > max_chars:
            quote = quote[:max_chars - 3].rstrip() + "..."

        return quote if quote else fallback_quote[:max_chars]

    except Exception as e:
        logger.error(f"[Groq AI Error] Lỗi gọi API Groq: {e}", exc_info=True)
        return fallback_quote[:max_chars]

async def send_morning_briefing(user_id: int):
    user = await bot.fetch_user(user_id)
    if not user:
        return
    tkb_data = await get_processed_daily_tkb()
    quote = generate_groq_quote("khởi đầu ngày mới tràn đầy năng lượng")

    embed = discord.Embed(
        title="☀️ DeskFlow: Morning Briefing Chào Ngày Mới!",
        description=f"Chúc bạn một ngày học tập thật hiệu quả!\n\n💡 *\"{quote}\"*",
        color=0xF1C40F
    )
    if tkb_data:
        m_subs = "\n".join(tkb_data['morning']) if tkb_data.get('morning') else "Nghỉ"
        a_subs = "\n".join(tkb_data['afternoon']) if tkb_data.get('afternoon') else "Nghỉ"
        embed.add_field(name="🌅 Tiết Sáng", value=m_subs, inline=True)
        embed.add_field(name="🌆 Tiết Chiều", value=a_subs, inline=True)
        embed.add_field(name="👕 Đồng Phục", value=f"Sáng: **{tkb_data.get('dp_sang', 'Tự do')}** | Chiều: **{tkb_data.get('dp_chieu', 'Tự do')}**", inline=False)

    embed.add_field(name="⛅ Thời Tiết Dự Báo", value=device_status.get("weather", "N/A"), inline=False)
    embed.set_footer(text="DeskFlow IoT Companion System")
    await user.send(embed=embed)

# ================= MQTT CLIENT =================
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
alarm_engine.set_mqtt_client(mqtt_client)
station_control.set_mqtt_client(mqtt_client)
station_control.bind_shared_state(
    device_status=device_status,
    device_settings=device_settings,
    laptop_status=laptop_status,
    groq_client=groq_client
)

def mqtt_publish_logged(topic: str, payload: str, qos: int = 1, retain: bool = False):
    logger.info(f"[MQTT OUT] Topic: {topic} | Payload: {payload} | Retain: {retain}")
    mqtt_client.publish(topic, payload, qos=qos, retain=retain)

async def trigger_alarm_event(math_q: str, answer: int):
    """Kích hoạt chuông reo dồn dập và chuyển màn hình ST7789 sang Math Alarm"""
    device_status["math_active"] = True
    device_status["current_math_answer"] = str(answer)
    try:
        await alarm_engine.trigger_alarm(question=math_q, solution=answer)
    except Exception as e:
        logger.error(f"[AlarmEngine] Lỗi kích hoạt chuông: {e}")
    logger.info(f"[MATH ALARM HOẠT ĐỘNG] Phép tính: {math_q} = {answer}")

async def handle_pogo_action(action: str):
    global current_screen_mode

    if action == "hold_play":
        modes_cycle = ["main", "stats", "tkb", "calendar", "weather", "lyrics", "notes", "countdown", "clock"]
        try:
            next_idx = (modes_cycle.index(current_screen_mode) + 1) % len(modes_cycle)
        except ValueError:
            next_idx = 0
        current_screen_mode = modes_cycle[next_idx]

        logger.info(f"[Screen] Chuyển chế độ hiển thị sang: [{current_screen_mode.upper()}]")

        if current_screen_mode == "main":
            mqtt_publish_logged("deskflow/display", json.dumps({"action": "switch_view", "view": "main"}))
        elif current_screen_mode == "stats":
            srv = get_server_metrics()
            comp = station_control.get_status().get("computer", {})
            payload = {
                "action": "switch_view",
                "view": "stats",
                "server": srv,
                "laptop": {
                    "cpu": comp.get("cpu", 0),
                    "ram_pct": comp.get("ram", 0),
                    "ram": comp.get("ram", 0),
                    "temp": comp.get("temp", 0),
                    "battery": comp.get("battery", 0),
                    "online": comp.get("online", False)
                }
            }
            mqtt_publish_logged("deskflow/display", json.dumps(payload))
        elif current_screen_mode == "tkb":
            tkb_payload = await build_tkb_payload()
            if tkb_payload:
                tkb_payload["action"] = "switch_view"
                mqtt_publish_logged("deskflow/display", json.dumps(tkb_payload, ensure_ascii=False))
        elif current_screen_mode == "weather":
            payload = {
                "action": "switch_view",
                "view": "weather",
                "city": clean_ascii(device_settings.get("weather_city", "Ha Long")),
                "weather": clean_ascii(device_status.get("weather", "28*C"))
            }
            mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))
        elif current_screen_mode == "lyrics":
            payload = {"action": "switch_view", "view": "lyrics", "curr": "Dang dong bo...", "next": ""}
            mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))
        elif current_screen_mode == "notes":
            payload = {"action": "switch_view", "view": "notes"}
            mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))
            station_control.sync_notes_to_station()
        elif current_screen_mode == "countdown":
            payload = {"action": "switch_view", "view": "countdown"}
            mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))
            station_control.sync_countdowns_to_station()
        elif current_screen_mode == "clock":
            payload = {"action": "switch_view", "view": "clock"}
            mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))
        elif current_screen_mode == "calendar":
            station_control.sync_calendar_to_station()
            payload = {"action": "switch_view", "view": "calendar"}
            mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))

    elif action == "refresh_tkb":
        tkb_payload = await build_tkb_payload()
        if tkb_payload:
            mqtt_publish_logged("deskflow/schedule", json.dumps(tkb_payload, ensure_ascii=False))

    elif action == "press_play":
        spotify_service.play_pause_toggle()
    elif action == "press_next":
        spotify_service.next_track()
    elif action == "press_prev":
        spotify_service.previous_track()
    elif action in ("volume_up", "volume_down"):
        # Station hardware menu volume stepper (±5%), clamped 0-100.
        delta = 5 if action == "volume_up" else -5
        # Blocking Spotify calls run off the event loop thread.
        current = await asyncio.to_thread(spotify_service.get_current_volume)
        base = current if current is not None else 50
        new_volume = max(0, min(100, base + delta))
        await asyncio.to_thread(spotify_service.set_playback_volume, new_volume)
        # Echo the authoritative value back so the OLED menu and every app agree.
        station_control.set_spotify_volume(new_volume)
        logger.info(f"[Station Menu] Spotify volume -> {new_volume}% ({action})")

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("[MQTT] Kết nối Broker thành công! Tiến hành Subscribe topics...")
        client.subscribe(config.TOPIC_STATUS_UPDATE)
        client.subscribe(config.TOPIC_MATH_TRIGGER)
        client.subscribe(config.TOPIC_BATTERY_ALERT)
        client.subscribe("deskflow/laptop_stats")
        client.subscribe("deskflow/button_action")
        client.subscribe("deskflow/device_watchdog")
        client.subscribe("deskflow/browser_audit")
        client.subscribe("deskflow/clock_style_ack")
        client.subscribe("deskflow/clock_style_request")
        client.subscribe("deskflow/hardware_settings_ack")
        client.subscribe("deskflow/hardware_settings_request")
        client.subscribe("deskflow/pair_request")
        client.subscribe("deskflow/screenshot")
        client.subscribe("deskflow/system/restart")
        client.subscribe("deskflow/serial/log")
    else:
        logger.error(f"[MQTT Error] Lỗi kết nối Broker: rc={rc}")

def on_message(client, userdata, msg):
    payload_str = msg.payload.decode("utf-8")
    topic = msg.topic
    logger.info(f"[MQTT IN] Topic: {topic} | Payload: {payload_str}")

    try:
        data = json.loads(payload_str)
    except Exception as e:
        logger.warning(f"[MQTT Error] Parse JSON fail: {e}")
        return

    if topic == config.TOPIC_STATUS_UPDATE:
        device_status.update(data)
        device_status["last_seen"] = time.time()
        reported_view = data.get("active_view")
        if isinstance(reported_view, str) and reported_view.strip():
            station_control.sync_station_view(reported_view)
        action = data.get("action")
        if action == "hardware_settings_update":
            station_control.on_hardware_settings_update(data)
        elif action == "hardware_settings_ack":
            station_control.on_hardware_settings_ack(data)
        elif action == "hardware_settings_request":
            station_control.on_hardware_settings_request(data)
        elif action == "station_language_update":
            station_control.on_station_language_update(data)
        elif action == "confirm_awake":
            asyncio.run_coroutine_threadsafe(alarm_engine.confirm_awake(bypass_captcha=True), bot.loop)
        elif action == "ota_progress":
            station_control.record_ota_progress(str(data.get("status") or "updating"))
    elif topic == "deskflow/screenshot":
        try:
            import base64
            w = int(data.get("width") or 240)
            h = int(data.get("height") or 320)
            b64 = str(data.get("rle_b64") or "")
            if b64:
                raw_rle = base64.b64decode(b64)
                station_control.save_screenshot_rgb565_rle(w, h, raw_rle)
                logger.info(f"[Screenshot] Captured {w}x{h} frame ({len(raw_rle)} bytes RLE)")
        except Exception as se:
            logger.warning(f"[Screenshot] Decode error: {se}")
    elif topic == "deskflow/serial/log":
        from src import v24_features
        v24_features.append_serial_log(str(data.get("line") or payload_str))
    elif topic == "deskflow/system/restart":
        logger.warning("[System] Emergency restart requested from ESP32 via MQTT!")
        os._exit(1)
    elif topic == config.TOPIC_MATH_TRIGGER:
        device_status["math_active"] = True
        device_status["current_math_answer"] = str(data.get("answer"))
    elif topic == config.TOPIC_BATTERY_ALERT:
        alert_type = data.get("type")
        pct = data.get("percent", 0)
        asyncio.run_coroutine_threadsafe(handle_battery_event(alert_type, pct), bot.loop)
    elif topic == "deskflow/laptop_stats":
        laptop_status.update(data)
        laptop_status["last_seen"] = time.time()
    elif topic == "deskflow/button_action":
        action = data.get("action")
        asyncio.run_coroutine_threadsafe(handle_pogo_action(action), bot.loop)
    elif topic == "deskflow/device_watchdog":
        is_net = data.get("internet", False)
        logger.info(f"[ESP32 Local Watchdog] Internet: {'ONLINE' if is_net else 'OFFLINE'}")
    elif topic == "deskflow/browser_audit":
        audit_id = data.get("id")
        title = data.get("title", "")
        allowed = check_title_with_groq(title)
        res = {"id": audit_id, "title": title, "status": "ALLOW" if allowed else "BLOCK"}
        mqtt_publish_logged("deskflow/browser_audit_result", json.dumps(res, ensure_ascii=False), qos=1)
    elif topic == "deskflow/clock_style_ack":
        station_control.on_clock_style_ack(data)
    elif topic == "deskflow/clock_style_request":
        station_control.on_clock_style_request(data)
    elif topic == "deskflow/hardware_settings_ack":
        station_control.on_hardware_settings_ack(data)
    elif topic == "deskflow/hardware_settings_request":
        station_control.on_hardware_settings_request(data)
    elif topic == "deskflow/pair_request":
        asyncio.run_coroutine_threadsafe(handle_esp32_pair_request(), bot.loop)


async def handle_esp32_pair_request():
    """Generates a Full Access 8-char pairing code + packed QR bitmap matrix for the ESP32 TFT."""
    try:
        svc = focus_mode.get_service()
        if not svc:
            return
        raw_code, expires_at = await svc.auth.generate_pairing_code(scopes=["focus", "control", "spotify"])
        qr_payload = f"deskflow://pair?code={raw_code}"
        qr_size = 0
        qr_bits_hex = ""
        try:
            import qrcode
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=1,
                border=1,
            )
            qr.add_data(qr_payload)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            qr_size = len(matrix)
            buf = bytearray((qr_size * qr_size + 7) // 8)
            bit_idx = 0
            for row in matrix:
                for val in row:
                    if val:
                        buf[bit_idx >> 3] |= (1 << (7 - (bit_idx & 7)))
                    bit_idx += 1
            qr_bits_hex = buf.hex().upper()
        except Exception as qe:
            logger.warning(f"[ESP32 Pair] QR generation error: {qe}")

        out = {
            "action": "pair_code_result",
            "code": raw_code,
            "qr_payload": qr_payload,
            "qr_size": qr_size,
            "qr_bits": qr_bits_hex,
            "expires_at": expires_at,
            "expires_in": 600,
            "full_access": True,
        }
        mqtt_publish_logged("deskflow/display", json.dumps(out), qos=1)
        logger.info(f"[ESP32 Pair] Issued Full Access pairing code + {qr_size}x{qr_size} QR for station.")
    except Exception as e:
        logger.error(f"[ESP32 Pair] Failed: {e}")

async def handle_battery_event(alert_type: str, pct: int):
    user = await bot.fetch_user(config.OWNER_USER_ID)
    if not user:
        return
    if alert_type == "low":
        embed = discord.Embed(title=f"🪫 Cảnh Báo Pin Yếu (< {device_settings['battery_low_thresh']}%)", description=f"Pin DeskFlow còn **{pct}%**.\nVui lòng cắm sạc!", color=0xE74C3C)
        await user.send(embed=embed)
    elif alert_type == "high":
        embed = discord.Embed(title=f"🔋 Bảo Vệ Pin: Đã Đạt {device_settings['battery_high_thresh']}%", description=f"Pin DeskFlow đạt **{pct}%**.\nHãy ngắt sạc để bảo vệ cell!", color=0x2ECC71)
        await user.send(embed=embed)

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(config.MQTT_BROKER, config.MQTT_PORT, 60)
mqtt_client.loop_start()

# ================= DISCORD BOT =================
class DeskFlowBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        intents.guilds = True
        super().__init__(intents=intents, chunk_guilds_at_startup=True)
        self.tree = app_commands.CommandTree(self)
        self.cached_spotify = None

    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if after.id == config.OWNER_USER_ID:
            sp = None
            for act in after.activities:
                if isinstance(act, discord.Spotify):
                    sp = act
                    logger.info(f"[Spotify Discord Presence] {act.title} - {act.artist}")
                    break
            self.cached_spotify = sp

    async def setup_hook(self):
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            logger.error(f"[Slash Command Error] {error}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Có lỗi: `{error}`", ephemeral=True)

        try:
            focus_svc = await focus_mode.init_focus_mode(
                bot=self,
                groq_client=groq_client,
                logger=logger,
                owner_user_id=config.OWNER_USER_ID
            )
            if focus_svc:
                def _mqtt_focus_publisher(topic: str, payload: Any, retain: bool = True, qos: int = 1):
                    msg = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload)
                    mqtt_publish_logged(topic, msg, qos=qos, retain=retain)
                focus_svc.set_mqtt_publisher(_mqtt_focus_publisher)
        except Exception as e:
            logger.error(f"[Focus Mode Init Error] {e}", exc_info=True)

        await self.tree.sync()
        scheduler.setup_scheduler(self, mqtt_client)
        if focus_mode.get_service():
            scheduler.setup_focus_scheduler(focus_mode.get_service(), loop=self.loop)
        asyncio.create_task(self.spotify_sync_task())
        asyncio.create_task(self.telemetry_broadcast_task())

    async def close(self):
        try:
            await focus_mode.shutdown_focus_mode()
        except Exception as e:
            logger.warning(f"[Focus Mode Shutdown Error] {e}")
        await super().close()

    async def spotify_sync_task(self):
        last_sent_lyric = None
        while True:
            try:
                sp_data = await asyncio.to_thread(spotify_service.get_current_playing)
                if sp_data and sp_data.get("is_playing"):
                    curr_lyric = sp_data.get("lyric_curr", "") or ""
                    if curr_lyric != last_sent_lyric or current_screen_mode == "lyrics":
                        last_sent_lyric = curr_lyric
                        payload = {
                            "action": "spotify_update",
                            "track": sp_data.get("track", "") or "",
                            "artist": sp_data.get("artist", "") or "",
                            "progress_pct": sp_data.get("progress_pct", 0),
                            "lyric_curr": curr_lyric,
                            "lyric_next": sp_data.get("lyric_next", "") or "",
                            "volume": sp_data.get("volume"),
                            "is_playing": True
                        }
                        if mqtt_client and mqtt_client.is_connected():
                            mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False), qos=0)
            except Exception as e:
                logger.debug(f"[Spotify Sync Guard] {e}")
            await asyncio.sleep(1.0)

    async def telemetry_broadcast_task(self):
        tkb_ticks = 0
        psutil.cpu_percent(interval=None)
        while True:
            try:
                srv = get_server_metrics()
                active_tel = station_control.get_active_telemetry()
                selected_metrics = active_tel.get("metrics")
                is_selected_online = bool(active_tel.get("is_online", False))

                if is_selected_online and selected_metrics:
                    lp_cpu = selected_metrics.get("cpu", 0.0)
                    lp_ram = selected_metrics.get("ram_pct", 0.0)
                    lp_bat = selected_metrics.get("battery", 0)
                else:
                    is_lp_online = (time.time() - laptop_status.get("last_seen", 0)) < 3.0
                    if is_lp_online:
                        lp_cpu = laptop_status.get("cpu", 0.0)
                        lp_ram = laptop_status.get("ram", 0.0)
                        lp_bat = laptop_status.get("battery", 0)
                    else:
                        # No live source: never rebroadcast the last known values
                        # (that is how stale readings such as RAM 441 survived).
                        lp_cpu = 0.0
                        lp_ram = 0.0
                        lp_bat = 0

                payload = {
                    "action": "telemetry",
                    "server": {
                        "cpu": srv["cpu"],
                        "ram_pct": srv["ram_pct"],
                        "ram_used_mb": srv["ram_used_mb"],
                        "uptime": srv["uptime"]
                    },
                    "laptop": {
                        "cpu": lp_cpu,
                        "ram_pct": lp_ram,
                        "battery": lp_bat
                    },
                    "selected_device_id": active_tel.get("selected_device_id"),
                    "source_online": is_selected_online
                }
                if mqtt_client and mqtt_client.is_connected():
                    mqtt_publish_logged("deskflow/telemetry", json.dumps(payload, ensure_ascii=False), qos=1)

                # (#117) Laptop Battery Threshold Protection Alert (>=80% unplug, 1..24% plug in)
                if lp_bat and 0 < int(lp_bat) <= 100 and (is_selected_online or (time.time() - laptop_status.get("last_seen", 0)) < 15.0):
                    now_bat_ts = time.time()
                    last_bat_alert = getattr(self, "_last_laptop_bat_alert_ts", 0.0)
                    last_bat_kind = getattr(self, "_last_laptop_bat_alert_kind", "")
                    bat_int = int(lp_bat)
                    alert_kind = ""
                    if bat_int >= 80:
                        alert_kind = "high"
                    elif bat_int < 25:
                        alert_kind = "low"
                    if alert_kind and (alert_kind != last_bat_kind or (now_bat_ts - last_bat_alert) >= 900.0):
                        self._last_laptop_bat_alert_ts = now_bat_ts
                        self._last_laptop_bat_alert_kind = alert_kind
                        msg_t = (
                            f"Pin Laptop đã đạt {bat_int}% (>=80%) — hãy rút sạc để bảo vệ pin!"
                            if alert_kind == "high"
                            else f"Pin Laptop còn {bat_int}% (<25%) — hãy cắm sạc ngay!"
                        )
                        station_control.push_notification("laptop_battery", "Bảo vệ Pin Laptop", msg_t, "high")
                        if mqtt_client and mqtt_client.is_connected():
                            mqtt_publish_logged("deskflow/alarm", json.dumps({"action": "buzz", "duration": 180}), qos=1)
                            mqtt_publish_logged("deskflow/display", json.dumps({
                                "action": "webhook_flash",
                                "color": "#22C55E" if alert_kind == "high" else "#EF4444",
                                "summary": f"Laptop Bat {bat_int}%!",
                            }), qos=0)
                    elif 30 <= bat_int <= 75:
                        self._last_laptop_bat_alert_kind = ""

                tkb_ticks += 1
                if tkb_ticks >= 5:
                    tkb_ticks = 0
                    tkb_payload = await build_tkb_payload()
                    if tkb_payload and mqtt_client and mqtt_client.is_connected():
                        mqtt_publish_logged("deskflow/schedule", json.dumps(tkb_payload, ensure_ascii=False), qos=1)
            except Exception as e:
                logger.error(f"[Telemetry Broadcast Error] {e}", exc_info=True)
            await asyncio.sleep(1.0)

bot = DeskFlowBot()

# ================= CÁC LỆNH SLASH COMMANDS =================

# --- 1. NHÓM BÁO THỨC, THỜI KHÓA BIỂU & HỌC TẬP ---

@bot.tree.command(name="alarm", description="Cài báo thức thức dậy Math Alarm (VD: 4:45 hoặc 06:30)")
@app_commands.describe(time="Mốc thời gian báo thức (HH:MM)")
async def cmd_alarm(interaction: discord.Interaction, time: str):
    await interaction.response.defer(ephemeral=True)
    t_clean = time.strip()
    try:
        parts = t_clean.split(":")
        target_h = int(parts[0])
        target_m = int(parts[1])

        # Tính toán chính xác theo giờ Việt Nam
        now_vn = datetime.now(VN_TZ)
        target_time = now_vn.replace(hour=target_h, minute=target_m, second=0, microsecond=0)

        # Nếu giờ hẹn đã qua trong ngày -> Hẹn sang ngày hôm sau
        if target_time <= now_vn:
            target_time += timedelta(days=1)

        delay_seconds = (target_time - now_vn).total_seconds()

        # Tạo phép toán ngẫu nhiên
        n1 = random.randint(10, 50)
        n2 = random.randint(10, 50)
        correct_ans = n1 + n2
        device_status["current_math_answer"] = str(correct_ans)
        math_q = f"{n1} + {n2}"

        async def wait_and_ring():
            await asyncio.sleep(delay_seconds)
            await trigger_alarm_event(math_q, correct_ans)

        asyncio.create_task(wait_and_ring())

        mins_left = int(delay_seconds // 60)
        secs_left = int(delay_seconds % 60)
        await interaction.followup.send(
            f"⏰ Đã hẹn giờ báo thức lúc **{target_h:02d}:{target_m:02d}** "
            f"(còn **{mins_left} phút {secs_left} giây** nữa)!\n"
            f"Chuông sẽ kêu liên tục cho đến khi giải đúng bằng lệnh `/solve`.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Định dạng giờ không hợp lệ (hãy dùng HH:MM): `{e}`", ephemeral=True)

@bot.tree.command(name="solve", description="Nhập đáp án số để tắt còi Math Alarm")
@app_commands.describe(answer="Đáp án số của phép tính trên màn hình trạm")
async def cmd_solve(interaction: discord.Interaction, answer: int):
    await interaction.response.defer(ephemeral=True)
    active_session = await alarm_engine.get_active_session_public()
    sid = active_session.get("session_id")
    if not sid or not active_session.get("active"):
        await interaction.followup.send("⚠️ Không có báo thức nào đang kêu!", ephemeral=True)
        return
    try:
        await alarm_engine.solve_alarm(session_id=sid, answer=answer)
        device_status["math_active"] = False
        device_status["current_math_answer"] = None
        
        tkb_payload = await build_tkb_payload()
        if tkb_payload:
            tkb_payload["action"] = "switch_view"
            mqtt_publish_logged("deskflow/display", json.dumps(tkb_payload, ensure_ascii=False))
        
        await send_morning_briefing(config.OWNER_USER_ID)
        await interaction.followup.send("✅ **Chính xác!** Đã tắt còi Math Alarm, chuyển trạm sang TKB và gửi Morning Briefing qua tin nhắn riêng.", ephemeral=True)
    except ValueError:
        await interaction.followup.send(f"❌ Đáp án `{answer}` chưa đúng! Còi vẫn đang kêu, hãy tính lại cẩn thận.", ephemeral=True)
    except TimeoutError:
        await interaction.followup.send("⏳ Đang trong thời gian chờ (Cooldown) do sai quá 5 lần. Vui lòng thử lại sau 1 phút.", ephemeral=True)
    except PermissionError:
        await interaction.followup.send("🔒 Báo thức đã bị khóa do sai quá 8 lần. Hãy liên hệ hủy báo thức.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: `{e}`", ephemeral=True)

@bot.tree.command(name="alarm_cancel", description="Hủy hoặc tắt còi Math Alarm khẩn cấp")
async def cmd_alarm_cancel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await alarm_engine.emergency_stop()
        device_status["math_active"] = False
        device_status["current_math_answer"] = None
        await interaction.followup.send("🛑 Đã tắt còi Math Alarm khẩn cấp thành công!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: `{e}`", ephemeral=True)

@bot.tree.command(name="close_tkb", description="Tắt màn hình Thời khóa biểu từ xa, đưa trạm về Dashboard đồng hồ chính")
async def cmd_close_tkb(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    global current_screen_mode
    current_screen_mode = "main"
    mqtt_publish_logged("deskflow/display", json.dumps({"action": "switch_view", "view": "main"}))
    await interaction.followup.send("🖥️ Đã đóng TKB, đưa màn hình về lại **Dashboard đồng hồ chính**.", ephemeral=True)

@bot.tree.command(name="timer", description="Hẹn giờ tập trung học bài (Timer)")
@app_commands.describe(duration="Thời gian đếm ngược (VD: 25m, 1h)", note="Mục tiêu bài học")
async def cmd_timer(interaction: discord.Interaction, duration: str, note: str = "Tập trung học"):
    d_clean = duration.strip().lower()
    total_seconds = int(d_clean[:-1]) * 60 if d_clean.endswith("m") else (int(d_clean[:-1]) * 3600 if d_clean.endswith("h") else int(d_clean) * 60)
    payload = json.dumps({"action": "start_timer", "seconds": total_seconds, "label": duration, "note": note}, ensure_ascii=False)
    mqtt_publish_logged(config.TOPIC_TIMER, payload)

    embed = discord.Embed(title="⏳ Bắt Đầu Hẹn Giờ Tập Trung", color=0xE67E22)
    embed.add_field(name="Thời lượng", value=duration, inline=True)
    embed.add_field(name="Mục tiêu", value=note, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ki_thi", description="Thiết lập đếm ngược kỳ thi quan trọng lên màn hình DeskFlow")
@app_commands.describe(ten_ky_thi="Tên kỳ thi (VD: HSG Tỉnh, Thi Giữa Kỳ)", ngay_thi="Ngày thi định dạng DD/MM (VD: 14/01)")
async def cmd_ki_thi(interaction: discord.Interaction, ten_ky_thi: str, ngay_thi: str):
    await interaction.response.defer(ephemeral=True)
    try:
        d_p, m_p = [int(x) for x in ngay_thi.strip().split("/")]
        curr_year = datetime.now(VN_TZ).year
        target_date = date(curr_year, m_p, d_p)
        today = datetime.now(VN_TZ).date()
        if target_date < today:
            target_date = date(curr_year + 1, m_p, d_p)
        days_left = (target_date - today).days

        payload = {
            "action": "exam_countdown",
            "name": ten_ky_thi,
            "date": ngay_thi.strip(),
            "days_left": days_left
        }
        mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))
        await interaction.followup.send(f"🎯 Đã ghim đếm ngược kỳ thi **{ten_ky_thi}** ({ngay_thi})! Còn đúng **{days_left} ngày**.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Ngày tháng không đúng định dạng DD/MM: `{e}`", ephemeral=True)

# --- 2. NHÓM ĐÈN LED & MÀN HÌNH ---

@bot.tree.command(name="lamp_color", description="Đổi màu vòng LED WS2812B theo mã HEX bất kỳ (VD: #FF00FF, #38BDF8, #F00, red)")
@app_commands.describe(hex="Mã màu HEX bất kỳ (VD: #FF0000, #38BDF8, #F00) hoặc tên màu (red, cyan, warm)")
async def cmd_lamp_color(interaction: discord.Interaction, hex: str):
    await interaction.response.defer(ephemeral=True)
    try:
        res = station_control.set_lamp_color(hex)
        await interaction.followup.send(f"🎨 Đã chuyển màu đèn sang mã: `{res['hex']}`", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Mã màu không hợp lệ: `{e}`", ephemeral=True)

@bot.tree.command(name="lamp_dim", description="Điều chỉnh độ sáng vòng LED WS2812B (0-100%)")
@app_commands.describe(led="Độ sáng vòng LED (0-100%)")
async def cmd_lamp_dim(interaction: discord.Interaction, led: int):
    await interaction.response.defer(ephemeral=True)
    value = max(0, min(100, led))
    station_control.set_lamp_dim(led=value)
    await interaction.followup.send(f"🔆 Đã đặt độ sáng vòng LED: `{value}%`", ephemeral=True)

@bot.tree.command(name="lamp_on", description="Bật lại vòng LED WS2812B với màu đã lưu")
async def cmd_lamp_on(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    station_control.set_led("on")
    await interaction.followup.send("💡 Đã bật vòng LED trang trí.", ephemeral=True)

@bot.tree.command(name="lamp_off", description="Tắt nhanh vòng LED (màn hình đồng hồ vẫn sáng bình thường)")
async def cmd_lamp_off(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    station_control.set_led("off")
    await interaction.followup.send("🌙 Đã tắt vòng LED trang trí.", ephemeral=True)

@bot.tree.command(name="clock_style", description="Đổi kiểu hiển thị mặt đồng hồ trên DeskFlow")
@app_commands.describe(style="Chọn kiểu mặt đồng hồ")
@app_commands.choices(style=[
    app_commands.Choice(name="Số cổ điển (Digital Classic)", value="digital_classic"),
    app_commands.Choice(name="Tối giản (Digital Minimal)", value="digital_minimal"),
    app_commands.Choice(name="Đèn Neon (Digital Neon)", value="digital_neon"),
    app_commands.Choice(name="Đồng hồ kim (Analog)", value="analog"),
    app_commands.Choice(name="Đồng hồ lật (Flip Clock)", value="flip"),
    app_commands.Choice(name="Đồng hồ nhị phân (Binary)", value="binary"),
    app_commands.Choice(name="Đồng hồ chữ (Word Clock)", value="word")
])
async def cmd_clock_style(interaction: discord.Interaction, style: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    cfg = station_control.set_clock_config({"style": style.value})
    device_settings["clock_style"] = cfg["style"]
    await interaction.followup.send(f"🕰️ Đã chuyển mặt đồng hồ sang kiểu: **{style.name}** (v{cfg['config_version']})", ephemeral=True)

# --- 3. NHÓM TƯƠNG TÁC, AI & GIÁM SÁT ---

@bot.tree.command(name="say", description="Ghim ghi chú/tin nhắn khẩn cấp lên màn hình bàn học dạng Sticky Note")
@app_commands.describe(noi_dung="Nội dung lời nhắn muốn hiển thị")
async def cmd_say(interaction: discord.Interaction, noi_dung: str):
    await interaction.response.defer(ephemeral=True)
    clean_msg = noi_dung.strip()
    payload = {"action": "sticky_note", "note": clean_msg}
    mqtt_publish_logged("deskflow/display", json.dumps(payload, ensure_ascii=False))
    await interaction.followup.send(f"📌 Đã ghim giấy nhớ lên màn hình: *\"{clean_msg}\"*", ephemeral=True)

@bot.tree.command(name="status", description="Xem bảng thông số hệ thống: Pin, Điện áp, Wi-Fi, Spotify, Weather")
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    srv = get_server_metrics()
    is_lp_online = (time.time() - laptop_status.get("last_seen", 0)) < 60

    embed = discord.Embed(title="📊 Trạng Thái Hệ Thống DeskFlow Station", color=0x38BDF8)
    embed.add_field(
        name="⚡ Trạm ESP32",
        value=f"• Pin: `{device_status.get('battery_pct', 0)}%` ({device_status.get('voltage', 0.0)}V)\n• Wi-Fi RSSI: `{device_status.get('wifi_rssi', 0)} dBm`\n• View Hiện Tại: `{current_screen_mode.upper()}`",
        inline=False
    )
    embed.add_field(
        name="⛅ Thời Tiết & Đa Phương Tiện",
        value=f"• Thời tiết: `{device_status.get('weather', 'N/A')}`\n• Spotify: `{clean_ascii(device_status.get('spotify_track', 'Chưa phát'))}`",
        inline=False
    )

    if is_lp_online:
        lp_val = f"• Server CPU/RAM: `{srv['cpu']}%` | `{srv['ram_pct']}%` (Up: `{srv['uptime']}`)\n• Laptop: `{laptop_status.get('cpu', 0)}% CPU` | `{laptop_status.get('ram', 0)}% RAM`"
    else:
        lp_val = f"• Server CPU/RAM: `{srv['cpu']}%` | `{srv['ram_pct']}%` (Up: `{srv['uptime']}`)\n• Laptop: *Offline*"

    embed.add_field(
        name="🖥️ Debian Server & Laptop",
        value=lp_val,
        inline=False
    )
    embed.set_footer(text="DeskFlow IoT Companion System")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="quote", description="Yêu cầu Groq AI sinh danh ngôn động lực đẩy xuống trạm")
@app_commands.describe(
    topic="Chủ đề danh ngôn (mặc định: học tập và kiên trì)",
    limit="Giới hạn số lượng ký tự tối đa (mặc định: 75 ký tự, tối đa: 150)"
)
async def cmd_quote(
    interaction: discord.Interaction,
    topic: Optional[str] = "học tập và kiên trì",
    limit: Optional[int] = 75
):
    await interaction.response.defer(ephemeral=True)
    char_limit = max(20, min(150, limit if limit else 75))
    quote = generate_groq_quote(topic, max_chars=char_limit)
    clean_q = quote

    payload = {"action": "quote_update", "quote": clean_q}
    mqtt_publish_logged("deskflow/quote", json.dumps(payload, ensure_ascii=False))

    await interaction.followup.send(
        f"💡 **Danh ngôn mới** *(Giới hạn: {char_limit} ký tự | Thực tế: {len(quote)} ký tự)*:\n> *\"{quote}\"*",
        ephemeral=True
    )

# --- 4. NHÓM CẤU HÌNH HỆ THỐNG TỪ XA (REMOTE SETTINGS) ---

@bot.tree.command(name="config_darkmode", description="Cài đặt chế độ Dark Mode từ xa")
@app_commands.describe(mode="Chế độ Dark Mode", start="Giờ bắt đầu (VD: 22:00)", end="Giờ kết thúc (VD: 06:00)")
@app_commands.choices(mode=[
    app_commands.Choice(name="Tắt (off)", value="off"),
    app_commands.Choice(name="Luôn bật (always)", value="always"),
    app_commands.Choice(name="Theo khung giờ (custom)", value="custom")
])
async def cmd_config_darkmode(interaction: discord.Interaction, mode: app_commands.Choice[str], start: Optional[str] = "22:00", end: Optional[str] = "06:00"):
    device_settings["darkmode_mode"] = mode.value
    device_settings["darkmode_start"] = start
    device_settings["darkmode_end"] = end
    payload = {"action": "config_darkmode", "mode": mode.value, "start": start, "end": end}
    mqtt_publish_logged("deskflow/display", json.dumps(payload))
    await interaction.response.send_message(f"🌙 Cài đặt Dark Mode: **{mode.name}** ({start} -> {end})", ephemeral=True)

@bot.tree.command(name="config_weather", description="Đặt mốc giờ quét trời mưa trước khi đi học và khu vực")
@app_commands.describe(enable="Bật/Tắt tính năng", time="Mốc giờ quét mưa (HH:MM)", city="Tên thành phố")
async def cmd_config_weather(interaction: discord.Interaction, enable: bool, time: Optional[str] = "06:15", city: Optional[str] = "Ha Long"):
    device_settings["weather_alert_enabled"] = enable
    device_settings["weather_alert_time"] = time
    device_settings["weather_city"] = city
    await interaction.response.send_message(f"🌧️ Đã cập nhật cảnh báo mưa: **{'BẬT' if enable else 'TẮT'}** lúc `{time}` tại `{city}`.", ephemeral=True)

@bot.tree.command(name="quote_schedule", description="Thêm, xóa hoặc xem các mốc giờ Groq AI tự động phát danh ngôn")
@app_commands.describe(action="Hành động", time="Mốc giờ (HH:MM)")
@app_commands.choices(action=[
    app_commands.Choice(name="Xem danh sách (list)", value="list"),
    app_commands.Choice(name="Thêm mốc giờ (add)", value="add"),
    app_commands.Choice(name="Xóa mốc giờ (remove)", value="remove")
])
async def cmd_quote_schedule(interaction: discord.Interaction, action: app_commands.Choice[str], time: Optional[str] = None):
    scheds = device_settings.setdefault("quote_schedules", ["06:30", "12:00", "20:00"])
    if action.value == "list":
        await interaction.response.send_message(f"⏰ Các mốc giờ phát danh ngôn trong ngày: `{', '.join(scheds)}`", ephemeral=True)
    elif action.value == "add":
        if time and time not in scheds:
            scheds.append(time)
            scheds.sort()
            await interaction.response.send_message(f"➕ Đã thêm mốc giờ: `{time}`. Danh sách: `{', '.join(scheds)}`", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Thiếu mốc giờ hoặc mốc giờ đã tồn tại!", ephemeral=True)
    elif action.value == "remove":
        if time and time in scheds:
            scheds.remove(time)
            await interaction.response.send_message(f"➖ Đã xóa mốc giờ: `{time}`. Danh sách: `{', '.join(scheds)}`", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Mốc giờ không tồn tại trong danh sách!", ephemeral=True)

@bot.tree.command(name="config_battery", description="Tùy biến ngưỡng cảnh báo pin yếu và ngắt sạc bảo vệ pin")
@app_commands.describe(low="Ngưỡng pin yếu nhắc cắm sạc (%)", high="Ngưỡng pin đầy nhắc rút sạc (%)")
async def cmd_config_battery(interaction: discord.Interaction, low: int, high: int):
    device_settings["battery_low_thresh"] = low
    device_settings["battery_high_thresh"] = high
    await interaction.response.send_message(f"🔋 Đã cập nhật ngưỡng pin: Pin yếu < **{low}%** | Rút sạc > **{high}%**", ephemeral=True)

@bot.tree.command(name="settings", description="Xem lại toàn bộ thông số cấu hình đang lưu trong thiết bị")
async def cmd_settings(interaction: discord.Interaction):
    embed = discord.Embed(title="⚙️ Toàn Bộ Cấu Hình DeskFlow Station", color=0x9B59B6)
    embed.add_field(
        name="🌙 Dark Mode",
        value=f"• Chế độ: `{device_settings['darkmode_mode']}`\n• Khung giờ: `{device_settings['darkmode_start']}` -> `{device_settings['darkmode_end']}`",
        inline=False
    )
    embed.add_field(
        name="🌧️ Cảnh Báo Thời Tiết",
        value=f"• Trạng thái: `{'Bật' if device_settings['weather_alert_enabled'] else 'Tắt'}`\n• Quét lúc: `{device_settings['weather_alert_time']}`\n• Địa điểm: `{device_settings['weather_city']}`",
        inline=False
    )
    embed.add_field(
        name="🔋 Quản Lý Pin 21700",
        value=f"• Báo cắm sạc: `< {device_settings['battery_low_thresh']}%`\n• Báo rút sạc: `> {device_settings['battery_high_thresh']}%`",
        inline=False
    )
    embed.add_field(
        name="🕰️ Giao Diện & AI",
        value=f"• Mặt đồng hồ: `{device_settings['clock_style']}`\n• Lịch phát Quote: `{', '.join(device_settings.get('quote_schedules', []))}`",
        inline=False
    )
    embed.set_footer(text="DeskFlow Remote Configuration Monitor")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 5. CÁC LỆNH ĐIỀU KHIỂN ĐÃ CÓ ---

@bot.tree.command(name="next", description="Chuyển sang bài hát tiếp theo trên Spotify")
async def cmd_next(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await handle_pogo_action("press_next")
        mqtt_publish_logged("deskflow/button_action", json.dumps({"action": "press_next"}))
        await interaction.followup.send("⏭️ Đã chuyển sang bài hát tiếp theo!", ephemeral=True)
    except Exception as e:
        logger.error(f"[Command Error] /next: {e}")
        await interaction.followup.send(f"❌ Lỗi: `{e}`", ephemeral=True)

@bot.tree.command(name="previous", description="Quay lại bài hát trước đó trên Spotify")
async def cmd_previous(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await handle_pogo_action("press_prev")
        mqtt_publish_logged("deskflow/button_action", json.dumps({"action": "press_prev"}))
        await interaction.followup.send("⏮️ Đã quay lại bài hát trước đó!", ephemeral=True)
    except Exception as e:
        logger.error(f"[Command Error] /previous: {e}")
        await interaction.followup.send(f"❌ Lỗi: `{e}`", ephemeral=True)

@bot.tree.command(name="play_pause", description="Bật/Tạm dừng nhạc hoặc Giữ nút để chuyển màn hình trạm")
@app_commands.describe(mode="Chọn thao tác: Nhấn thường (Play/Pause) hoặc Giữ nút (Đổi View)")
@app_commands.choices(mode=[
    app_commands.Choice(name="Nhấn thường (Play / Pause)", value="press"),
    app_commands.Choice(name="Giữ nút (Chuyển chế độ màn hình)", value="hold")
])
async def cmd_play_pause(interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(ephemeral=True)
    chosen_mode = mode.value if mode else "press"
    try:
        if chosen_mode == "hold":
            await handle_pogo_action("hold_play")
            mqtt_publish_logged("deskflow/button_action", json.dumps({"action": "hold_play"}))
            await interaction.followup.send(f"🔄 Đã giữ nút Play: Màn hình trạm chuyển sang view **{current_screen_mode.upper()}**", ephemeral=True)
        else:
            await handle_pogo_action("press_play")
            mqtt_publish_logged("deskflow/button_action", json.dumps({"action": "press_play"}))
            await interaction.followup.send("⏯️ Đã đảo trạng thái Play / Pause Spotify!", ephemeral=True)
    except Exception as e:
        logger.error(f"[Command Error] /play_pause: {e}")
        await interaction.followup.send(f"❌ Lỗi: `{e}`", ephemeral=True)

@bot.tree.command(name="tkb", description="Tra cứu thời khóa biểu và đồng phục từ PocketBase")
@app_commands.describe(day="Chọn thứ cần xem")
@app_commands.choices(day=[
    app_commands.Choice(name="Thứ 2", value="Thứ 2"),
    app_commands.Choice(name="Thứ 3", value="Thứ 3"),
    app_commands.Choice(name="Thứ 4", value="Thứ 4"),
    app_commands.Choice(name="Thứ 5", value="Thứ 5"),
    app_commands.Choice(name="Thứ 6", value="Thứ 6"),
])
async def cmd_tkb(interaction: discord.Interaction, day: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    try:
        tkb_info = await get_processed_daily_tkb(day.value)
        if not tkb_info:
            await interaction.followup.send(f"📅 Không tìm thấy TKB **{day.name}**!", ephemeral=True)
            return

        morning_list = tkb_info.get("morning", [])
        afternoon_list = tkb_info.get("afternoon", [])
        dp_sang = tkb_info.get("dp_sang", "Không có")
        dp_chieu = tkb_info.get("dp_chieu", "Không có")

        embed = discord.Embed(title=f"📚 Thời Khóa Biểu - {day.value}", color=0x3498DB)
        sang_text = "\n".join(morning_list) if morning_list else "*Nghỉ học*"
        embed.add_field(name="🌅 Buổi Sáng", value=sang_text, inline=False)

        if tkb_info.get("has_afternoon"):
            chieu_text = "\n".join(afternoon_list)
            embed.add_field(name="🌆 Buổi Chiều", value=chieu_text, inline=False)
            embed.add_field(name="👔 Đồng phục", value=f"• **Sáng:** {dp_sang}\n**Chiều:** {dp_chieu}", inline=False)
        else:
            embed.add_field(name="👔 Đồng phục", value=f"• {dp_sang}", inline=False)

        embed.set_footer(text=f"Tuần {tkb_info.get('week_number', 'N/A')} • DeskFlow PocketBase Sync")

        clean_m = [s.split(": ")[-1].strip() for s in morning_list if s.strip()]
        clean_a = [s.split(": ")[-1].strip() for s in afternoon_list if s.strip()]
        has_a = bool(tkb_info.get("has_afternoon", False)) and len(clean_a) > 0

        dp_s_clean = dp_sang
        dp_c_clean = dp_chieu
        if has_a:
            uniform_str = f"Sáng: {dp_s_clean}, Chiều: {dp_c_clean}" if (dp_c_clean and dp_c_clean != dp_s_clean) else f"{dp_s_clean} (chung cả ngày)"
        else:
            uniform_str = dp_s_clean

        mqtt_payload = {
            "action": "switch_view",
            "view": "tkb",
            "day_name": day.value,
            "morning": ", ".join(clean_m) if clean_m else "Nghỉ học",
            "has_afternoon": has_a,
            "afternoon": ", ".join(clean_a) if has_a else "",
            "uniform": uniform_str
        }
        mqtt_publish_logged("deskflow/display", json.dumps(mqtt_payload, ensure_ascii=False))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"[Command Error] /tkb: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Lỗi: `{e}`", ephemeral=True)

@bot.tree.command(name="view", description="Chuyển chế độ hiển thị trên màn hình ST7789")
@app_commands.describe(screen="Chọn màn hình muốn hiển thị")
@app_commands.choices(screen=[
    app_commands.Choice(name="Màn hình chính (Main)", value="main"),
    app_commands.Choice(name="Phần cứng (Stats)", value="stats"),
    app_commands.Choice(name="Thời khóa biểu (TKB)", value="tkb"),
    app_commands.Choice(name="Lịch Google (Calendar)", value="calendar"),
    app_commands.Choice(name="Thời tiết & AQI (Weather)", value="weather"),
    app_commands.Choice(name="Lời bài hát (Lyrics)", value="lyrics"),
    app_commands.Choice(name="Đĩa than Spotify (Vinyl)", value="vinyl"),
    app_commands.Choice(name="Matrix Rain (#51)", value="matrix"),
    app_commands.Choice(name="GitHub Stats (#57)", value="github"),
    app_commands.Choice(name="Công thức & Bảng tuần hoàn (#11)", value="formulas"),
    app_commands.Choice(name="Arcade Game Hub (#23/#58-#63)", value="game"),
])
async def cmd_view(interaction: discord.Interaction, screen: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    global current_screen_mode
    current_screen_mode = screen.value

    if current_screen_mode in ("main", "stats", "tkb", "weather", "lyrics", "calendar", "vinyl", "matrix", "github", "formulas", "game"):
        if current_screen_mode == "github":
            await station_control.sync_github_stats()
        tkb_p = await build_tkb_payload() if current_screen_mode == "tkb" else None
        station_control.switch_view(current_screen_mode, tkb_payload=tkb_p)

    await interaction.followup.send(f"📺 Đã chuyển trạm sang màn hình: **{screen.name}**", ephemeral=True)

@bot.tree.command(name="buzz", description="Kích hoạt còi Buzzer trên trạm ESP32")
@app_commands.describe(duration="Thời gian bíp (mili-giây, mặc định 250ms)")
async def cmd_buzz(interaction: discord.Interaction, duration: Optional[int] = 250):
    await interaction.response.defer(ephemeral=True)
    await alarm_engine.trigger_buzz(duration=duration or 250)
    await interaction.followup.send(f"🔔 Đã kích hoạt còi bíp ({duration}ms)!", ephemeral=True)

@bot.tree.command(name="led", description="Điều khiển đèn LED trên trạm ESP32")
@app_commands.describe(action="Hành động cho đèn LED")
@app_commands.choices(action=[
    app_commands.Choice(name="Bật / Tắt (Toggle)", value="toggle"),
    app_commands.Choice(name="Bật nháy (Blink Mode)", value="blink")
])
async def cmd_led(interaction: discord.Interaction, action: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    payload = {"action": "led", "act": action.value}
    mqtt_publish_logged("deskflow/display", json.dumps(payload))
    await interaction.followup.send(f"💡 Đã gửi lệnh điều khiển LED: **{action.name}**!", ephemeral=True)

@bot.tree.command(name="melody", description="Phát nhạc MIDI / Chiptune 8-bit qua còi Buzzer trên trạm ESP32")
@app_commands.describe(
    song="Chọn bài hát mẫu hoặc Dừng phát",
    midi_file="Đính kèm file .mid từ máy tính / điện thoại để phát qua còi ESP32"
)
@app_commands.choices(song=[
    app_commands.Choice(name="Super Mario Bros Theme", value="mario"),
    app_commands.Choice(name="Never Gonna Give You Up (Rickroll)", value="rickroll"),
    app_commands.Choice(name="Tetris Theme (Korobeiniki)", value="tetris"),
    app_commands.Choice(name="Astronomia (Coffin Dance)", value="coffin"),
    app_commands.Choice(name="Star Wars - Imperial March", value="starwars"),
    app_commands.Choice(name="Nokia Tune", value="nokia"),
    app_commands.Choice(name="⏹ Dừng phát nhạc (Stop)", value="stop")
])
async def cmd_melody(
    interaction: discord.Interaction,
    song: Optional[app_commands.Choice[str]] = None,
    midi_file: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=True)
    if song and song.value == "stop":
        await station_control.stop_melody()
        await interaction.followup.send("⏹ Đã dừng phát nhạc trên còi ESP32!", ephemeral=True)
        return

    from midi_parser import get_preset, parse_midi_bytes
    if midi_file:
        if not (midi_file.filename.endswith(".mid") or midi_file.filename.endswith(".midi")):
            await interaction.followup.send("❌ Vui lòng đính kèm file định dạng `.mid` hoặc `.midi`!", ephemeral=True)
            return
        data = await midi_file.read()
        try:
            notes = parse_midi_bytes(data)
            title = midi_file.filename.rsplit(".", 1)[0]
            await station_control.play_melody(notes=notes, title=title)
            await interaction.followup.send(f"🎵 Đang phát file MIDI: **{title}** ({len(notes)} nốt) trên còi ESP32!", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Không thể giải mã file MIDI: {e}", ephemeral=True)
            return

    chosen = song.value if song else "mario"
    p = get_preset(chosen)
    if not p:
        await interaction.followup.send("❌ Không tìm thấy bài hát!", ephemeral=True)
        return
    await station_control.play_melody(notes=p["notes"], title=p["title"])
    await interaction.followup.send(f"🎵 Đang phát bài: **{p['title']}** ({len(p['notes'])} nốt) trên còi ESP32!", ephemeral=True)


# --- #92 Discord Slash Commands: /rgb, /ota, /game, /morning ---
@bot.tree.command(name="rgb", description="Điều khiển hiệu ứng đèn LED 24 bóng & Gradient trên DeskFlow")
@app_commands.describe(
    effect="Hiệu ứng đèn LED",
    color="Màu chính HEX (vd #00FF88)",
    color2="Màu thứ 2 cho Gradient (vd #FF0088)",
    gradient="Bật/Tắt chế độ dải màu Gradient 2 màu",
    speed="Tốc độ hiệu ứng (5-100)"
)
@app_commands.choices(effect=[
    app_commands.Choice(name="Đứng yên (Solid)", value="solid"),
    app_commands.Choice(name="Cầu vồng RGB", value="rainbow"),
    app_commands.Choice(name="Nhịp thở (Breathing)", value="breathing"),
    app_commands.Choice(name="Đuổi nhau (Chase)", value="chase"),
    app_commands.Choice(name="Cảnh sát (Police)", value="police"),
    app_commands.Choice(name="Lấp lánh (Sparkle)", value="sparkle"),
    app_commands.Choice(name="Lửa cháy (Fire)", value="fire"),
    app_commands.Choice(name="Nhấp nháy (Strobe)", value="strobe"),
    app_commands.Choice(name="Theo bìa Spotify (Ambient)", value="ambient"),
    app_commands.Choice(name="Tắt đèn (Off)", value="off"),
])
async def cmd_rgb(
    interaction: discord.Interaction,
    effect: app_commands.Choice[str],
    color: Optional[str] = None,
    color2: Optional[str] = None,
    gradient: Optional[bool] = None,
    speed: Optional[int] = None,
):
    await interaction.response.defer(ephemeral=True)
    if effect.value == "off":
        station_control.set_lamp_off()
        await interaction.followup.send("💡 Đã tắt dải đèn LED!", ephemeral=True)
        return
    try:
        res = station_control.set_lamp_effect(
            effect.value,
            speed=speed,
            color=color,
            color2=color2,
            gradient=gradient,
        )
        await interaction.followup.send(
            f"✨ LED updated: **{res.get('led_effect')}** | RGB={res.get('led_rgb')} | Gradient={res.get('led_gradient')} | Speed={res.get('led_speed')}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi LED: {e}", ephemeral=True)


@bot.tree.command(name="game", description="Chọn & mở Minigame trên màn hình ESP32 (Dino / Flappy / Snake)")
@app_commands.describe(mode="Chọn tựa game")
@app_commands.choices(mode=[
    app_commands.Choice(name="🦖 Dino Runner (Nhảy xương rồng)", value=0),
    app_commands.Choice(name="🐦 Flappy Bird", value=1),
    app_commands.Choice(name="🐍 Snake (Rắn săn mồi)", value=2),
])
async def cmd_game(interaction: discord.Interaction, mode: app_commands.Choice[int]):
    await interaction.response.defer(ephemeral=True)
    res = station_control.set_game_mode(mode.value)
    station_control.switch_view("game")
    await interaction.followup.send(f"🎮 Đã chuyển màn hình ESP32 sang **{res.get('name')}**!", ephemeral=True)


@bot.tree.command(name="ota", description="Kiểm tra phiên bản OTA, tự pull từ GitHub hoặc nạp firmware xuống ESP32")
@app_commands.describe(action="Xem trạng thái, tự động Check & Pull GitHub, hoặc Nạp ngay")
@app_commands.choices(action=[
    app_commands.Choice(name="Xem trạng thái OTA", value="status"),
    app_commands.Choice(name="Check & Tự Pull bản mới nhất + Flash", value="pull"),
    app_commands.Choice(name="Nạp Firmware OTA có sẵn ngay", value="flash"),
])
async def cmd_ota(interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(ephemeral=True)
    act = action.value if action else "status"
    st = station_control.get_ota_status()
    if act == "pull":
        try:
            res = await station_control.check_and_pull_latest_ota(auto_pull=True, auto_flash=True, force=True)
            await interaction.followup.send(
                f"✅ **OTA GitHub Sync**\n• Bản mới nhất: `{res.get('tag_name')}`\n• ESP32 trước đó: `{res.get('current_esp_version')}`\n• Đã pull: `{res.get('pulled')}` | Đã gửi lệnh Flash: `{res.get('flashed')}`",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Check/Pull OTA thất bại: {e}", ephemeral=True)
        return
    if act == "flash":
        try:
            res = station_control.trigger_ota_flash()
            await interaction.followup.send(
                f"🚀 Đã gửi lệnh OTA v**{res.get('version')}** tới ESP32!\nURL: `{res.get('url')}`",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Không thể flash OTA: {e}", ephemeral=True)
        return
    await interaction.followup.send(
        f"📡 **OTA Status**\n• ESP32 hiện tại: `{st.get('station_fw_version')}` ({'Online' if st.get('station_online') else 'Offline'})\n• Firmware trên Server: `{st.get('version') or 'Chưa upload'}` ({st.get('size_bytes', 0)} bytes)",
        ephemeral=True,
    )


@bot.tree.command(name="screenshot", description="(#75) Chụp màn hình ST7789 trên ESP32 và gửi ảnh về Discord")
async def cmd_screenshot(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    prev_ts = int(station_control.device_settings.get("last_screenshot_at") or 0)
    station_control.request_screenshot()
    for _ in range(25):
        await asyncio.sleep(0.25)
        if int(station_control.device_settings.get("last_screenshot_at") or 0) > prev_ts:
            break
    raw = station_control.get_latest_screenshot_bytes()
    if not raw:
        await interaction.followup.send("⏳ ESP32 chưa phản hồi ảnh chụp màn hình.", ephemeral=True)
        return
    import io
    await interaction.followup.send(
        content="📸 **ESP32 ST7789 Screenshot (240×320)**",
        file=discord.File(fp=io.BytesIO(raw), filename="deskflow_esp32.bmp"),
    )


@bot.tree.command(name="morning", description="(#91) Gửi ngay Bản Tin Sáng DeskFlow (Thời tiết, TKB, Google Calendar)")
async def cmd_morning(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        embed = await scheduler.build_morning_report_embed()
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi tạo bản tin sáng: {e}", ephemeral=True)

if __name__ == "__main__":
    spotify_service.init_spotify()
    token = getattr(config, "DISCORD_TOKEN", None) or getattr(config, "DISCORD_BOT_TOKEN", None)
    logger.info("[Discord] Đang kết nối Gateway...")
    bot.run(token)