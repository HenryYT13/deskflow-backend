"""
DeskFlow Station Control Module
Centralized manager for physical desk station controls:
- Display views & screen switching
- LED lighting, colors, dimming, onboard LED
- Clock style
- Sticky notes & exam countdown
- Groq AI quote generation
- Pomodoro timer
- Alarm scheduling & buzzer
- Remote settings management & system status telemetry
"""

import os
import json
import time
import uuid
import random
import re
import logging
import asyncio
import threading
import unicodedata
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, Optional, List

import config
from alarm_engine import alarm_engine

logger = logging.getLogger("DeskFlow.StationControl")

VN_TZ = timezone(timedelta(hours=7))

SUPPORTED_CLOCK_STYLES = (
    "digital_classic",
    "digital_minimal",
    "digital_neon",
    "analog",
    "flip",
    "binary",
    "word"
)

STYLE_LEGACY_MAP = {
    "modern": "digital_classic",
    "minimal": "digital_minimal"
}

DEFAULT_CLOCK_CONFIG = {
    "style": "digital_classic",
    "time_format": "24h",
    "show_seconds": True,
    "show_date": True,
    "show_ampm": False,
    "language": "vi",
    "animation_enabled": True,
    "animation_speed": 1.0,
    "accent_color": "#38BDF8",
    "config_version": 1,
    "applied_by_station": False,
    "last_applied_style": "digital_classic",
    "last_error": None
}


def parse_strict_bool(val: Any, field_name: str) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        if val == 1:
            return True
        if val == 0:
            return False
        raise ValueError(f"{field_name} must be boolean or 0/1 (got {val})")
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{field_name} must be boolean (got '{val}')")
    raise ValueError(f"{field_name} must be boolean (got {type(val).__name__})")


def parse_strict_brightness(val: Any, clamp: bool = False) -> int:
    if isinstance(val, bool):
        raise ValueError("led_brightness cannot be boolean")
    try:
        b = int(val)
    except (TypeError, ValueError):
        raise ValueError(f"led_brightness must be an integer between 0 and 100 (got {val})")
    if clamp:
        return max(0, min(100, b))
    if b < 0 or b > 100:
        raise ValueError(f"led_brightness must be between 0 and 100 (got {b})")
    return b


def parse_strict_buzzer_duration(val: Any) -> int:
    if isinstance(val, bool):
        raise ValueError("buzzer_duration cannot be boolean")
    if val is None or val == "":
        raise ValueError("buzzer_duration cannot be empty")
    try:
        d = int(val)
    except (TypeError, ValueError):
        raise ValueError(f"buzzer_duration must be an integer between 10 and 1000 ms (got {val})")
    if d < 10 or d > 1000:
        raise ValueError(f"buzzer_duration must be between 10 and 1000 ms (got {d})")
    return d


def parse_strict_darkmode_mode(val: Any) -> str:
    if not isinstance(val, str):
        raise ValueError(f"darkmode_mode must be string (got {type(val).__name__})")
    v = val.strip().lower()
    if v in ("always", "always_on", "on", "dark"):
        return "always_on"
    if v in ("off", "light"):
        return "off"
    if v == "custom":
        return "custom"
    if v in ("scheduled", "sync", "auto"):
        return "scheduled"
    raise ValueError(f"darkmode_mode must be one of 'off', 'always_on', 'custom', 'scheduled', 'dark', 'light', 'sync' (got '{val}')")


def validate_time_str(val: Any, field_name: str) -> str:
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string in HH:mm format")
    s = val.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        raise ValueError(f"Invalid time format for {field_name}: '{val}'. Must be HH:mm (00:00 - 23:59)")
    h, mn = int(m.group(1)), int(m.group(2))
    if h < 0 or h > 23 or mn < 0 or mn > 59:
        raise ValueError(f"Invalid hour/minute for {field_name}: '{val}'. Hour must be 0-23, minute 0-59")
    return f"{h:02d}:{mn:02d}"


def clean_ascii(text: str) -> str:
    if not text:
        return ""
    text = text.replace("đ", "d").replace("Đ", "D")
    nfd = unicodedata.normalize('NFD', text)
    return "".join(c for c in nfd if unicodedata.category(c) != 'Mn')


class StationControlManager:
    def __init__(self):
        self._config_lock = threading.RLock()
        self._mqtt_client = None
        self._groq_client = None
        self.device_status: Dict[str, Any] = {
            "battery_pct": 100,
            "voltage": 4.12,
            "wifi_rssi": -55,
            "spotify_track": "Chưa phát nhạc",
            "spotify_artist": "N/A",
            "weather": "28°C Nắng nhẹ",
            "math_active": False,
            "current_math_answer": None
        }
        self.device_settings: Dict[str, Any] = {
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
            "clock_style": "digital_classic",
            "hardware_config_version": 1,
            "applied_by_station": False,
            "last_ack_error": None
        }
        self.clock_config: Dict[str, Any] = dict(DEFAULT_CLOCK_CONFIG)
        self.notes: List[Dict[str, Any]] = []
        self.active_note_id: Optional[str] = None
        self.countdowns: List[Dict[str, Any]] = []
        self.active_countdown_id: Optional[str] = None
        self.alarms: List[Dict[str, Any]] = []
        self.selected_display_device_id: Optional[str] = None
        self._device_telemetry: Dict[str, Dict[str, Any]] = {}
        self._data_dir = os.path.join(os.path.dirname(__file__), "data")
        self._settings_file = os.path.join(self._data_dir, "station_settings.json")
        self._load_settings()
        self.laptop_status: Dict[str, Any] = {
            "cpu": 0.0,
            "ram": 0.0,
            "temp": 0.0,
            "battery": 0,
            "last_seen": 0
        }
        self.current_screen_mode: str = "main"

    def _load_settings(self) -> None:
        try:
            if os.path.exists(self._settings_file):
                with open(self._settings_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    if "device_settings" in saved and isinstance(saved["device_settings"], dict):
                        self.device_settings.update(saved["device_settings"])
                        try:
                            ver = int(self.device_settings.get("hardware_config_version", 1))
                            self.device_settings["hardware_config_version"] = max(1, ver)
                        except (ValueError, TypeError):
                            self.device_settings["hardware_config_version"] = 1
                    if "clock_config" in saved and isinstance(saved["clock_config"], dict):
                        self.clock_config.update(saved["clock_config"])
                    if "notes" in saved and isinstance(saved["notes"], list):
                        self.notes = saved["notes"]
                    if "active_note_id" in saved:
                        self.active_note_id = saved.get("active_note_id")
                    if "countdowns" in saved and isinstance(saved["countdowns"], list):
                        self.countdowns = saved["countdowns"]
                    if "active_countdown_id" in saved:
                        self.active_countdown_id = saved.get("active_countdown_id")
                    if "alarms" in saved and isinstance(saved["alarms"], list):
                        self.alarms = saved["alarms"]
                    if "selected_display_device_id" in saved:
                        self.selected_display_device_id = saved.get("selected_display_device_id")
        except Exception as e:
            logger.warning(f"[StationControl] Failed to load settings from file: {e}")

    def _save_settings(self) -> None:
        with self._config_lock:
            try:
                os.makedirs(self._data_dir, exist_ok=True)
                tmp_path = f"{self._settings_file}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "device_settings": self.device_settings,
                        "clock_config": self.clock_config,
                        "notes": self.notes,
                        "active_note_id": self.active_note_id,
                        "countdowns": self.countdowns,
                        "active_countdown_id": self.active_countdown_id,
                        "alarms": self.alarms,
                        "selected_display_device_id": self.selected_display_device_id
                    }, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self._settings_file)
            except Exception as e:
                logger.error(f"[StationControl] Failed to save settings to file: {e}")
                raise IOError(f"Failed to persist settings: {e}") from e

    def set_mqtt_client(self, client: Any) -> None:
        self._mqtt_client = client

    def set_groq_client(self, client: Any) -> None:
        self._groq_client = client

    def bind_shared_state(
        self,
        device_status: Dict[str, Any],
        device_settings: Dict[str, Any],
        laptop_status: Dict[str, Any],
        groq_client: Optional[Any] = None
    ) -> None:
        self.device_status = device_status
        device_settings.update(self.device_settings)
        self.device_settings = device_settings
        self.laptop_status = laptop_status
        if groq_client:
            self._groq_client = groq_client

    def mqtt_publish(self, topic: str, payload_str: str, qos: int = 1) -> bool:
        if not self._mqtt_client:
            logger.warning(f"[StationControl] MQTT client not configured. Dropped publish to {topic}.")
            return False
        try:
            res = self._mqtt_client.publish(topic, payload_str, qos=qos)
            logger.info(f"[StationControl MQTT OUT] Topic: {topic} | Payload: {payload_str}")
            return getattr(res, "rc", 0) == 0
        except Exception as e:
            logger.error(f"[StationControl] MQTT publish error: {e}")
            return False

    def get_server_metrics(self) -> Dict[str, Any]:
        try:
            import psutil
            cpu_pct = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            disk_used_gb = round(disk.used / (1024 ** 3), 1)
            disk_total_gb = round(disk.total / (1024 ** 3), 1)
            uptime_seconds = int(time.time() - psutil.boot_time())
            uptime_hours = uptime_seconds // 3600
            uptime_mins = (uptime_seconds % 3600) // 60
            return {
                "cpu": round(cpu_pct, 1),
                "ram_pct": round(ram.percent, 1),
                "ram_used_mb": int(ram.used / (1024 * 1024)),
                "ram_total_mb": int(ram.total / (1024 * 1024)),
                "disk_used_gb": disk_used_gb,
                "disk_total_gb": disk_total_gb,
                "disk_pct": round(disk.percent, 1),
                "disk_str": f"{disk_used_gb} / {disk_total_gb}GB",
                "uptime": f"{uptime_hours}h {uptime_mins}m"
            }
        except Exception:
            return {
                "cpu": 0.0,
                "ram_pct": 0.0,
                "ram_used_mb": 0,
                "ram_total_mb": 0,
                "disk_used_gb": 0.0,
                "disk_total_gb": 20.0,
                "disk_pct": 0.0,
                "disk_str": "0.0 / 20.0GB",
                "uptime": "N/A"
            }

    def get_status(self) -> Dict[str, Any]:
        srv = self.get_server_metrics()
        now = time.time()

        sel_id = self.selected_display_device_id
        comp_metrics = None
        is_comp_online = False

        if sel_id:
            if sel_id in self._device_telemetry:
                entry = self._device_telemetry[sel_id]
                is_comp_online = (now - entry.get("last_seen", 0)) < 15.0
                comp_metrics = entry
            else:
                # Device selected but hasn't reported in this memory session: do not fall back to other machines
                dev_name = "Selected Computer"
                try:
                    from focus_mode.service import focus_service
                    d_info = focus_service.get_device(sel_id)
                    if d_info and d_info.get("device_name"):
                        dev_name = d_info["device_name"]
                except Exception:
                    pass
                comp_metrics = {
                    "cpu": 0.0,
                    "ram_pct": 0.0,
                    "temp": 0.0,
                    "battery": 0,
                    "device_name": dev_name
                }
                is_comp_online = False
        elif self._device_telemetry:
            latest = max(self._device_telemetry.values(), key=lambda x: x.get("last_seen", 0), default=None)
            if latest:
                is_comp_online = (now - latest.get("last_seen", 0)) < 15.0
                comp_metrics = latest

        if not comp_metrics:
            is_lp_legacy = (now - self.laptop_status.get("last_seen", 0)) < 60
            comp_metrics = {
                "cpu": self.laptop_status.get("cpu", 0),
                "ram_pct": self.laptop_status.get("ram", 0),
                "temp": self.laptop_status.get("temp", 0),
                "battery": self.laptop_status.get("battery", 0),
                "device_name": self.laptop_status.get("device_name", "Computer")
            }
            is_comp_online = is_lp_legacy

        comp_cpu = float(comp_metrics.get("cpu", 0))
        comp_ram = float(comp_metrics.get("ram_pct", comp_metrics.get("ram", 0)))
        comp_bat = int(comp_metrics.get("battery", 0))
        comp_name = str(comp_metrics.get("device_name", "Computer"))

        return {
            "station": {
                "battery_pct": int(self.device_status.get("battery_pct", 100)),
                "voltage": float(self.device_status.get("voltage", 4.12)),
                "wifi_rssi": int(self.device_status.get("wifi_rssi", -55)),
                "current_view": self.current_screen_mode,
                "weather": self.device_status.get("weather", "N/A"),
                "math_active": self.device_status.get("math_active", False)
            },
            "server": srv,
            "laptop": {
                "cpu": comp_cpu,
                "ram": comp_ram,
                "temp": comp_metrics.get("temp", 0),
                "battery": comp_bat,
                "device_name": comp_name,
                "online": is_comp_online
            },
            "computer": {
                "cpu": comp_cpu,
                "ram": comp_ram,
                "temp": comp_metrics.get("temp", 0),
                "battery": comp_bat,
                "device_name": comp_name,
                "online": is_comp_online
            }
        }

    def get_settings(self) -> Dict[str, Any]:
        return dict(self.device_settings)

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._config_lock:
            # Dark mode (supports off, always, always_on, custom, scheduled)
            darkmode_updated = False
            if "darkmode_mode" in updates:
                mode = parse_strict_darkmode_mode(updates["darkmode_mode"])
                self.device_settings["darkmode_mode"] = mode
                darkmode_updated = True

            if "darkmode_start" in updates:
                self.device_settings["darkmode_start"] = validate_time_str(updates["darkmode_start"], "darkmode_start")
                darkmode_updated = True

            if "darkmode_end" in updates:
                self.device_settings["darkmode_end"] = validate_time_str(updates["darkmode_end"], "darkmode_end")
                darkmode_updated = True

            # Hardware settings (LED Brightness & Buzzer)
            hw_updated = False
            if "led_brightness" in updates:
                b_val = parse_strict_brightness(updates["led_brightness"], clamp=True)
                self.device_settings["led_brightness"] = b_val
                hw_updated = True
            if "buzzer_enabled" in updates:
                bz_val = parse_strict_bool(updates["buzzer_enabled"], "buzzer_enabled")
                self.device_settings["buzzer_enabled"] = bz_val
                hw_updated = True
            if "buzzer_duration" in updates:
                d_val = parse_strict_buzzer_duration(updates["buzzer_duration"])
                self.device_settings["buzzer_duration"] = d_val
                hw_updated = True

            if hw_updated or darkmode_updated:
                self.device_settings["hardware_config_version"] = int(self.device_settings.get("hardware_config_version", 0)) + 1
                self.device_settings["applied_by_station"] = False
                self.device_settings["last_ack_error"] = None
                self._save_settings()
                self.publish_hardware_settings()

            if darkmode_updated:
                payload = {
                    "action": "config_darkmode",
                    "mode": self.device_settings.get("darkmode_mode", "off"),
                    "start": self.device_settings.get("darkmode_start", "22:00"),
                    "end": self.device_settings.get("darkmode_end", "06:00")
                }
                self.mqtt_publish("deskflow/display", json.dumps(payload))

            # Weather alert
            if "weather_alert_enabled" in updates:
                self.device_settings["weather_alert_enabled"] = bool(updates["weather_alert_enabled"])
            if "weather_alert_time" in updates:
                new_t = str(updates["weather_alert_time"]).strip()
                if re.match(r"^\d{1,2}:\d{2}$", new_t):
                    self.device_settings["weather_alert_time"] = new_t
                    try:
                        import scheduler
                        scheduler.reschedule_rain_check(new_t)
                    except Exception as e:
                        logger.warning(f"[StationControl] Could not reschedule rain check: {e}")
            if "weather_city" in updates:
                self.device_settings["weather_city"] = str(updates["weather_city"]).strip()

            # Battery thresholds
            if "battery_low_thresh" in updates:
                val = int(updates["battery_low_thresh"])
                if 5 <= val <= 50:
                    self.device_settings["battery_low_thresh"] = val
            if "battery_high_thresh" in updates:
                val = int(updates["battery_high_thresh"])
                if 60 <= val <= 100:
                    self.device_settings["battery_high_thresh"] = val

            # Quote schedules
            if "quote_schedules" in updates and isinstance(updates["quote_schedules"], list):
                valid_scheds = [s for s in updates["quote_schedules"] if isinstance(s, str) and ":" in s]
                valid_scheds.sort()
                self.device_settings["quote_schedules"] = valid_scheds

            # Clock style
            if "clock_style" in updates:
                style = updates["clock_style"]
                if style in ("modern", "flip", "minimal"):
                    self.device_settings["clock_style"] = style
                    self.mqtt_publish("deskflow/display", json.dumps({"action": "clock_style", "style": style}))

            self._save_settings()
            return dict(self.device_settings)

    def is_darkmode_active(self, now_dt: Optional[datetime] = None) -> bool:
        mode = self.device_settings.get("darkmode_mode", "off")
        if mode in ("always", "always_on"):
            return True
        if mode == "off":
            return False
        if mode in ("scheduled", "custom"):
            if now_dt is None:
                try:
                    import zoneinfo
                    tz = zoneinfo.ZoneInfo("Asia/Ho_Chi_Minh")
                    now_dt = datetime.now(tz)
                except Exception:
                    now_dt = datetime.now()
            cur_min = now_dt.hour * 60 + now_dt.minute
            start_str = self.device_settings.get("darkmode_start", "22:00")
            end_str = self.device_settings.get("darkmode_end", "06:00")
            try:
                sh, sm = map(int, start_str.split(":"))
                eh, em = map(int, end_str.split(":"))
                start_min = sh * 60 + sm
                end_min = eh * 60 + em
            except Exception:
                return False

            if start_min == end_min:
                # Safe, predictable fallback: 0-duration interval is treated as inactive (Light mode)
                return False
            elif start_min < end_min:
                # Daytime span within single day (e.g. 06:30 to 18:30 or 20:00 to 23:00)
                return start_min <= cur_min < end_min
            else:
                # Overnight span crossing midnight (e.g. 22:00 to 06:00)
                return cur_min >= start_min or cur_min < end_min
        return False

    def switch_view(self, view: str) -> Dict[str, Any]:
        view_clean = view.strip().lower()
        valid_views = ("main", "stats", "tkb", "weather", "lyrics", "clock", "notes", "countdown", "focus")
        if view_clean not in valid_views:
            raise ValueError(f"Invalid view '{view}'. Must be one of {valid_views}.")

        self.current_screen_mode = view_clean
        if view_clean == "main":
            self.mqtt_publish("deskflow/display", json.dumps({"action": "switch_view", "view": "main"}))
        elif view_clean == "stats":
            srv = self.get_server_metrics()
            payload = {
                "action": "switch_view",
                "view": "stats",
                "server": srv,
                "laptop": {
                    "cpu": self.laptop_status.get("cpu", 0),
                    "ram": self.laptop_status.get("ram", 0),
                    "temp": self.laptop_status.get("temp", 0),
                    "battery": self.laptop_status.get("battery", 0),
                    "online": (time.time() - self.laptop_status.get("last_seen", 0)) < 60
                }
            }
            self.mqtt_publish("deskflow/display", json.dumps(payload))
        elif view_clean == "weather":
            payload = {
                "action": "switch_view",
                "view": "weather",
                "city": clean_ascii(self.device_settings.get("weather_city", "Ha Long")),
                "weather": clean_ascii(self.device_status.get("weather", "28*C"))
            }
            self.mqtt_publish("deskflow/display", json.dumps(payload, ensure_ascii=False))
        elif view_clean == "lyrics":
            payload = {"action": "switch_view", "view": "lyrics", "curr": "Dang dong bo...", "next": ""}
            self.mqtt_publish("deskflow/display", json.dumps(payload, ensure_ascii=False))
        elif view_clean == "tkb":
            self.mqtt_publish("deskflow/display", json.dumps({"action": "switch_view", "view": "tkb"}))
        elif view_clean == "clock":
            self.mqtt_publish("deskflow/display", json.dumps({"action": "switch_view", "view": "clock"}))
        elif view_clean == "notes":
            self.sync_notes_to_station()
            self.mqtt_publish("deskflow/display", json.dumps({"action": "switch_view", "view": "notes"}))
        elif view_clean == "countdown":
            self.sync_countdowns_to_station()
            self.mqtt_publish("deskflow/display", json.dumps({"action": "switch_view", "view": "countdown"}))
        elif view_clean == "focus":
            self.mqtt_publish("deskflow/display", json.dumps({"action": "switch_view", "view": "focus"}))

        return {"success": True, "view": self.current_screen_mode}

    def close_tkb(self) -> Dict[str, Any]:
        return self.switch_view("main")

    def set_lamp_color(self, hex_color: str) -> Dict[str, Any]:
        """
        Sets the WS2812B NeoPixel LED lamp color. Accepts any arbitrary hex format:
        #RRGGBB, RRGGBB, #RGB, 0xRRGGBB, 8-char #AARRGGBB, or named colors (red, cyan, etc.).
        """
        h = str(hex_color).strip()
        if h.startswith("0x") or h.startswith("0X"):
            h = "#" + h[2:]
        elif not h.startswith("#"):
            h = "#" + h

        clean = h[1:].strip()
        NAMED_COLORS = {
            "red": "FF0000", "green": "00FF00", "blue": "0000FF",
            "white": "FFFFFF", "yellow": "FFFF00", "cyan": "00FFFF",
            "magenta": "FF00FF", "pink": "FF69B4", "orange": "FFA500",
            "purple": "8B5CF6", "amber": "F59E0B", "emerald": "10B981",
            "warm": "FFC58F", "cool": "C8DCFF"
        }
        if clean.lower() in NAMED_COLORS:
            clean = NAMED_COLORS[clean.lower()]
        elif len(clean) == 3:
            clean = "".join(c * 2 for c in clean)
        elif len(clean) == 8:
            clean = clean[2:]  # Strip alpha channel from #AARRGGBB

        try:
            int(clean, 16)
            if len(clean) != 6:
                raise ValueError()
        except ValueError:
            raise ValueError(f"Invalid hex color '{hex_color}'. Use any #RRGGBB (e.g. #FF5500).")

        normalized = f"#{clean.upper()}"
        self.mqtt_publish("deskflow/display", json.dumps({"action": "led_color", "hex": normalized}))
        return {"success": True, "hex": normalized}

    def set_lamp_dim(self, led: Optional[int] = None, screen: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"action": "dim"}
        res: Dict[str, Any] = {"success": True}
        if led is not None:
            led_val = max(0, min(100, int(led)))
            payload["led_bright"] = led_val
            res["led_bright"] = led_val
        if screen is not None:
            screen_val = max(0, min(100, int(screen)))
            payload["screen_bright"] = screen_val
            res["screen_bright"] = screen_val
        self.mqtt_publish("deskflow/display", json.dumps(payload))
        return res

    def set_lamp_off(self) -> Dict[str, Any]:
        self.mqtt_publish("deskflow/display", json.dumps({"action": "led_off"}))
        return {"success": True, "lamp": "off"}

    def set_led(self, action: str) -> Dict[str, Any]:
        if action not in ("toggle", "blink"):
            raise ValueError("LED action must be 'toggle' or 'blink'.")
        self.mqtt_publish("deskflow/display", json.dumps({"action": "led", "act": action}))
        return {"success": True, "action": action}

    def get_clock_config(self) -> Dict[str, Any]:
        return dict(self.clock_config)

    def set_clock_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._config_lock:
            if not isinstance(updates, dict):
                raise ValueError("Payload must be a JSON object.")

        if "style" in updates:
            raw_style = str(updates["style"]).strip()
            num_to_style = {
                "1": "digital_classic",
                "2": "digital_minimal",
                "3": "digital_neon",
                "4": "analog",
                "5": "flip",
                "6": "binary",
                "7": "word"
            }
            if raw_style in num_to_style:
                raw_style = num_to_style[raw_style]
            style = STYLE_LEGACY_MAP.get(raw_style, raw_style)
            if style not in SUPPORTED_CLOCK_STYLES:
                raise ValueError(f"Invalid clock style '{raw_style}'. Must be one of {SUPPORTED_CLOCK_STYLES}.")
            self.clock_config["style"] = style
            self.device_settings["clock_style"] = style

        if "time_format" in updates:
            fmt = str(updates["time_format"]).strip().lower()
            if fmt not in ("12h", "24h"):
                raise ValueError("time_format must be '12h' or '24h'.")
            self.clock_config["time_format"] = fmt

        if "show_seconds" in updates:
            self.clock_config["show_seconds"] = bool(updates["show_seconds"])

        if "show_date" in updates:
            self.clock_config["show_date"] = bool(updates["show_date"])

        if "show_ampm" in updates:
            self.clock_config["show_ampm"] = bool(updates["show_ampm"])

        if "language" in updates:
            lang = str(updates["language"]).strip().lower()
            if lang not in ("vi", "en"):
                raise ValueError("language must be 'vi' or 'en'.")
            self.clock_config["language"] = lang

        if "animation_enabled" in updates:
            self.clock_config["animation_enabled"] = bool(updates["animation_enabled"])

        if "animation_speed" in updates:
            try:
                spd = float(updates["animation_speed"])
                if 0.5 <= spd <= 2.0:
                    self.clock_config["animation_speed"] = round(spd, 2)
            except (ValueError, TypeError):
                pass

        if "accent_color" in updates:
            col = str(updates["accent_color"]).strip()
            if re.match(r"^#[0-9a-fA-F]{6}$", col):
                self.clock_config["accent_color"] = col.upper()

        self.clock_config["config_version"] = int(self.clock_config.get("config_version", 1)) + 1
        self.clock_config["applied_by_station"] = False
        self.clock_config["last_error"] = None

        self._save_settings()
        self.publish_clock_config()

        return dict(self.clock_config)

    def publish_clock_config(self) -> None:
        payload = {
            "action": "clock_style",
            "config_version": self.clock_config["config_version"],
            "style": self.clock_config["style"],
            "time_format": self.clock_config["time_format"],
            "show_seconds": self.clock_config["show_seconds"],
            "show_date": self.clock_config["show_date"],
            "show_ampm": self.clock_config["show_ampm"],
            "language": self.clock_config["language"],
            "animation_enabled": self.clock_config["animation_enabled"],
            "animation_speed": self.clock_config["animation_speed"],
            "accent_color": self.clock_config["accent_color"]
        }
        msg = json.dumps(payload, ensure_ascii=False)
        self.mqtt_publish("deskflow/clock_style", msg)
        self.mqtt_publish("deskflow/display", msg)

    def on_clock_style_ack(self, payload: Dict[str, Any]) -> bool:
        ver = payload.get("config_version")
        status = payload.get("status")
        err = payload.get("error")
        style = payload.get("style")

        logger.info(f"[StationControl] Clock Style ACK received: version={ver}, status={status}, style={style}")

        if ver == self.clock_config.get("config_version"):
            self.clock_config["applied_by_station"] = (status == "applied")
            self.clock_config["last_applied_style"] = style or self.clock_config["style"]
            self.clock_config["last_error"] = err
            self._save_settings()
            return True
        else:
            logger.warning(f"[StationControl] Ignored stale/mismatched ACK (got v{ver}, current is v{self.clock_config.get('config_version')})")
            return False

    def on_clock_style_request(self, payload: Dict[str, Any]) -> None:
        logger.info(f"[StationControl] Station requested latest clock config: {payload}")
        self.publish_clock_config()

    def set_clock_style(self, style: str) -> Dict[str, Any]:
        return self.set_clock_config({"style": style})

    # --- Persistent Notes CRUD & Station Pinning ---
    def sync_notes_to_station(self) -> bool:
        notes_list = []
        for n in self.notes[:10]:
            notes_list.append({
                "id": n.get("id", ""),
                "title": clean_ascii(n.get("title", "")),
                "content": clean_ascii(n.get("content", "")),
                "is_pinned": (n.get("id") == self.active_note_id)
            })
        payload = {"action": "sync_notes", "notes": notes_list}
        return self.mqtt_publish("deskflow/display", json.dumps(payload, ensure_ascii=False))

    def sync_countdowns_to_station(self) -> bool:
        cd_items = self.get_countdowns()
        cd_list = []
        for c in cd_items[:10]:
            cd_list.append({
                "id": c.get("id", ""),
                "title": clean_ascii(c.get("title", "")),
                "target_date": c.get("target_date", ""),
                "days_left": c.get("days_left", 0),
                "is_expired": c.get("is_expired", False),
                "is_pinned": c.get("is_active_on_station", False)
            })
        payload = {"action": "sync_countdowns", "countdowns": cd_list}
        return self.mqtt_publish("deskflow/display", json.dumps(payload, ensure_ascii=False))

    def publish_hardware_settings(self, force: bool = False) -> bool:
        hw_payload = {
            "action": "hardware_settings",
            "hardware_config_version": self.device_settings.get("hardware_config_version", 1),
            "force_sync": force,
            "led_brightness": self.device_settings.get("led_brightness", 100),
            "buzzer_enabled": self.device_settings.get("buzzer_enabled", True),
            "buzzer_duration": self.device_settings.get("buzzer_duration", 40),
            "darkmode_mode": self.device_settings.get("darkmode_mode", "off"),
            "darkmode_start": self.device_settings.get("darkmode_start", "22:00"),
            "darkmode_end": self.device_settings.get("darkmode_end", "06:00")
        }
        return self.mqtt_publish("deskflow/display", json.dumps(hw_payload))

    def on_hardware_settings_ack(self, data: Dict[str, Any]) -> None:
        with self._config_lock:
            try:
                ack_ver = int(data.get("hardware_config_version", 0))
            except (ValueError, TypeError):
                logger.warning(f"[StationControl] Malformed hardware_settings_ack: {data}")
                return

            expected_ver = int(self.device_settings.get("hardware_config_version", 1))
            if ack_ver != expected_ver:
                logger.info(f"[StationControl] Ignored stale/mismatched hardware_settings_ack v{ack_ver} (expected v{expected_ver})")
                return

            status = str(data.get("status", "")).lower()
            if status == "applied":
                self.device_settings["applied_by_station"] = True
                self.device_settings["last_ack_error"] = None
                self._save_settings()
                logger.info(f"[StationControl] ESP32 confirmed applied hardware_config_version v{ack_ver}")
            else:
                err = data.get("error") or data.get("reason") or "Station rejected hardware settings"
                self.device_settings["applied_by_station"] = False
                self.device_settings["last_ack_error"] = str(err)
                self._save_settings()
                logger.warning(f"[StationControl] Station rejected hardware config v{ack_ver}: {err}")

    def on_hardware_settings_request(self, data: Optional[Dict[str, Any]] = None) -> None:
        with self._config_lock:
            logger.info(f"[StationControl] ESP32 requested hardware settings: {data}. Publishing authoritative settings (force=True).")
            self.publish_hardware_settings(force=True)

    def on_hardware_settings_update(self, data: Dict[str, Any]) -> None:
        """
        Handle device-originated physical button change requests from ESP32.
        Backend is the ONLY authority that commits changes and assigns hardware_config_version.
        Validates all fields strictly without silent coercion.
        Serializes updates under _config_lock, persists to disk first, then broadcasts authoritative configuration.
        """
        with self._config_lock:
            candidate: Dict[str, Any] = {}
            changed = False

            if "led_brightness" in data:
                try:
                    candidate["led_brightness"] = parse_strict_brightness(data["led_brightness"])
                    changed = True
                except ValueError as ve:
                    logger.warning(f"[StationControl] Rejected invalid led_brightness from device: {ve}")
                    self.publish_hardware_settings(force=True)
                    return

            if "buzzer_enabled" in data:
                try:
                    candidate["buzzer_enabled"] = parse_strict_bool(data["buzzer_enabled"], "buzzer_enabled")
                    changed = True
                except ValueError as ve:
                    logger.warning(f"[StationControl] Rejected invalid buzzer_enabled from device: {ve}")
                    self.publish_hardware_settings(force=True)
                    return

            if "buzzer_duration" in data:
                try:
                    candidate["buzzer_duration"] = parse_strict_buzzer_duration(data["buzzer_duration"])
                    changed = True
                except ValueError as ve:
                    logger.warning(f"[StationControl] Rejected invalid buzzer_duration from device: {ve}")
                    self.publish_hardware_settings(force=True)
                    return

            if "darkmode_mode" in data:
                try:
                    candidate["darkmode_mode"] = parse_strict_darkmode_mode(data["darkmode_mode"])
                    changed = True
                except ValueError as ve:
                    logger.warning(f"[StationControl] Rejected invalid darkmode_mode from device: {ve}")
                    self.publish_hardware_settings(force=True)
                    return

            if "darkmode_start" in data:
                try:
                    candidate["darkmode_start"] = validate_time_str(data["darkmode_start"], "darkmode_start")
                    changed = True
                except ValueError as ve:
                    logger.warning(f"[StationControl] Rejected invalid darkmode_start from device: {ve}")
                    self.publish_hardware_settings(force=True)
                    return

            if "darkmode_end" in data:
                try:
                    candidate["darkmode_end"] = validate_time_str(data["darkmode_end"], "darkmode_end")
                    changed = True
                except ValueError as ve:
                    logger.warning(f"[StationControl] Rejected invalid darkmode_end from device: {ve}")
                    self.publish_hardware_settings(force=True)
                    return

            if not changed:
                logger.warning(f"[StationControl] Empty or unrecognized hardware_settings_update from device: {data}")
                self.publish_hardware_settings(force=True)
                return

            # Apply candidate values
            self.device_settings.update(candidate)

            # Backend is the ONLY authority that increments hardware_config_version
            new_version = int(self.device_settings.get("hardware_config_version", 0)) + 1
            self.device_settings["hardware_config_version"] = new_version
            self.device_settings["applied_by_station"] = False
            self.device_settings["last_ack_error"] = None

            # Persist to disk BEFORE publishing (Requirement 6)
            self._save_settings()
            logger.info(f"[StationControl] Device change committed as v{new_version}: {candidate}")

            # Publish committed configuration to station
            self.publish_hardware_settings()

    def get_notes(self) -> List[Dict[str, Any]]:
        res = []
        for n in self.notes:
            item = dict(n)
            item["text"] = item.get("content", "")
            item["content"] = item.get("content", "")
            item["title"] = item.get("title", "")
            item["is_pinned"] = (item["id"] == self.active_note_id)
            res.append(item)
        return res

    def add_note(self, content: str, title: Optional[str] = None) -> Dict[str, Any]:
        content = content.strip()
        if not content:
            raise ValueError("Note content cannot be empty.")
        note_id = f"note_{int(time.time())}_{random.randint(1000, 9999)}"
        title = (title or "").strip()
        if not title:
            title = content[:30] + ("..." if len(content) > 30 else "")
        created_at = datetime.now(VN_TZ).isoformat()
        note = {
            "id": note_id,
            "title": title,
            "content": content,
            "created_at": created_at
        }
        self.notes.append(note)
        self._save_settings()
        self.sync_notes_to_station()
        item = dict(note)
        item["is_pinned"] = (item["id"] == self.active_note_id)
        return item

    def delete_note(self, note_id: str) -> bool:
        initial_len = len(self.notes)
        self.notes = [n for n in self.notes if n["id"] != note_id]
        if self.active_note_id == note_id:
            self.active_note_id = None
            self.mqtt_publish("deskflow/display", json.dumps({"action": "clear_sticky_note"}, ensure_ascii=False))
        if len(self.notes) < initial_len:
            self._save_settings()
            self.sync_notes_to_station()
            return True
        return False

    def pin_note_to_station(self, note_id: str) -> Dict[str, Any]:
        target = next((n for n in self.notes if n["id"] == note_id), None)
        if not target:
            raise ValueError(f"Note '{note_id}' not found.")
        self.active_note_id = note_id
        self._save_settings()
        clean_text = clean_ascii(target["content"])
        self.sync_notes_to_station()
        self.mqtt_publish("deskflow/display", json.dumps({"action": "sticky_note", "note": clean_text, "id": note_id}, ensure_ascii=False))
        item = dict(target)
        item["is_pinned"] = True
        return item

    def unpin_sticky_note(self) -> bool:
        self.active_note_id = None
        self._save_settings()
        self.mqtt_publish("deskflow/display", json.dumps({"action": "clear_sticky_note"}, ensure_ascii=False))
        self.sync_notes_to_station()
        return True

    def set_sticky_note(self, note: str) -> Dict[str, Any]:
        # Backward compatibility: creates a note and pins it
        n = self.add_note(content=note)
        pinned = self.pin_note_to_station(n["id"])
        return {"success": True, "note": pinned["content"], "id": pinned["id"]}

    # --- Persistent Countdowns CRUD & Station Pinning ---
    def _parse_target_date(self, date_str: str) -> datetime:
        s = date_str.strip()
        now_vn = datetime.now(VN_TZ)
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.replace(tzinfo=VN_TZ)
            except ValueError:
                pass
        if "/" in s and len(s.split("/")) == 2:
            parts = s.split("/")
            d_p, m_p = int(parts[0]), int(parts[1])
            target_year = now_vn.year
            dt = datetime(target_year, m_p, d_p, 0, 0, 0, tzinfo=VN_TZ)
            if dt < now_vn:
                dt = datetime(target_year + 1, m_p, d_p, 0, 0, 0, tzinfo=VN_TZ)
            return dt
        raise ValueError(f"Unrecognized date format '{date_str}'. Supported: YYYY-MM-DD, DD/MM/YYYY, DD/MM.")

    def get_countdowns(self) -> List[Dict[str, Any]]:
        now_vn = datetime.now(VN_TZ)
        res = []
        for c in self.countdowns:
            item = dict(c)
            item["title"] = item.get("title", "")
            item["name"] = item.get("title", "")
            try:
                target_dt = datetime.fromisoformat(item["target_date"])
                diff = target_dt - now_vn
                total_seconds = int(diff.total_seconds())
                days_left = (target_dt.date() - now_vn.date()).days
                is_expired = total_seconds <= 0
                item["days_left"] = max(0, days_left)
                item["hours_left"] = max(0, total_seconds // 3600)
                item["is_expired"] = is_expired
            except Exception:
                item["days_left"] = 0
                item["is_expired"] = True
            item["is_pinned"] = (item["id"] == self.active_countdown_id)
            item["is_active_on_station"] = (item["id"] == self.active_countdown_id)
            res.append(item)
        return res

    def add_countdown(self, title: str, target_date_str: str) -> Dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("Countdown title cannot be empty.")
        target_dt = self._parse_target_date(target_date_str)
        cid = f"cd_{int(time.time())}_{random.randint(1000, 9999)}"
        cd = {
            "id": cid,
            "title": title,
            "target_date": target_dt.isoformat(),
            "created_at": datetime.now(VN_TZ).isoformat()
        }
        self.countdowns.append(cd)
        self._save_settings()
        self.sync_countdowns_to_station()
        return next(c for c in self.get_countdowns() if c["id"] == cid)

    def delete_countdown(self, countdown_id: str) -> bool:
        initial_len = len(self.countdowns)
        self.countdowns = [c for c in self.countdowns if c["id"] != countdown_id]
        if self.active_countdown_id == countdown_id:
            self.active_countdown_id = None
            self.mqtt_publish("deskflow/display", json.dumps({"action": "clear_countdown"}, ensure_ascii=False))
        if len(self.countdowns) < initial_len:
            self._save_settings()
            self.sync_countdowns_to_station()
            return True
        return False

    def set_active_countdown_on_station(self, countdown_id: str) -> Dict[str, Any]:
        items = self.get_countdowns()
        target = next((c for c in items if c["id"] == countdown_id), None)
        if not target:
            raise ValueError(f"Countdown '{countdown_id}' not found.")
        self.active_countdown_id = countdown_id
        self._save_settings()
        
        payload = {
            "action": "exam_countdown",
            "id": countdown_id,
            "name": clean_ascii(target["title"]),
            "days_left": target["days_left"],
            "is_expired": target["is_expired"]
        }
        self.sync_countdowns_to_station()
        self.mqtt_publish("deskflow/display", json.dumps(payload, ensure_ascii=False))
        target["is_active_on_station"] = True
        return target

    def set_exam_countdown(self, name: str, date_str: str) -> Dict[str, Any]:
        cd = self.add_countdown(title=name, target_date_str=date_str)
        pinned = self.set_active_countdown_on_station(cd["id"])
        return {
            "success": True,
            "name": name,
            "date": date_str.strip(),
            "days_left": pinned["days_left"],
            "id": pinned["id"]
        }

    def generate_groq_quote(self, topic: Optional[str] = "học tập và kiên trì", max_chars: int = 75) -> Dict[str, Any]:
        char_limit = max(20, min(150, max_chars or 75))
        topic_str = topic or "học tập và kiên trì"
        quote = "Kien tri hom nay, thanh cong ngay mai!"

        if self._groq_client:
            try:
                completion = self._groq_client.chat.completions.create(
                    model="openai/gpt-oss-20b",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Bạn là trợ lý tạo động lực DeskFlow. Hãy viết đúng 1 câu danh ngôn ngắn gọn tạo cảm hứng bằng tiếng Việt "
                                f"(tối đa {char_limit} ký tự). Chỉ trả về nội dung câu nói, không dùng ngoặc kép, không thêm lời dẫn."
                            )
                        },
                        {"role": "user", "content": f"Chủ đề: {topic_str}"}
                    ],
                    temperature=0.7,
                    max_completion_tokens=1024,
                    reasoning_effort="low"
                )
                raw_quote = completion.choices[0].message.content
                if raw_quote:
                    quote = raw_quote.strip().replace('"', '').replace('“', '').replace('”', '')
            except Exception as e:
                logger.warning(f"[StationControl] Groq quote error: {e}. Using fallback.")

        clean_q = clean_ascii(quote)
        self.mqtt_publish("deskflow/quote", json.dumps({"action": "quote_update", "quote": clean_q}, ensure_ascii=False))
        return {
            "success": True,
            "quote": quote,
            "topic": topic_str,
            "char_limit": char_limit
        }

    def start_pomodoro_timer(self, duration: str, note: str = "Tập trung học") -> Dict[str, Any]:
        d_clean = duration.strip().lower()
        total_seconds = int(d_clean[:-1]) * 60 if d_clean.endswith("m") else (
            int(d_clean[:-1]) * 3600 if d_clean.endswith("h") else int(d_clean) * 60
        )
        payload = {
            "action": "start_timer",
            "seconds": total_seconds,
            "label": duration,
            "note": clean_ascii(note)
        }
        self.mqtt_publish(getattr(config, "TOPIC_TIMER", "deskflow/timer"), json.dumps(payload, ensure_ascii=False))
        return {
            "success": True,
            "duration": duration,
            "total_seconds": total_seconds,
            "note": note
        }

    async def schedule_alarm(self, time_str: str) -> Dict[str, Any]:
        parts = time_str.strip().split(":")
        target_h = int(parts[0])
        target_m = int(parts[1])

        now_vn = datetime.now(VN_TZ)
        target_time = now_vn.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if target_time <= now_vn:
            target_time += timedelta(days=1)

        delay_seconds = (target_time - now_vn).total_seconds()
        n1 = random.randint(10, 50)
        n2 = random.randint(10, 50)
        correct_ans = n1 + n2
        self.device_status["current_math_answer"] = str(correct_ans)
        math_q = f"{n1} + {n2}"

        async def _wait_and_ring():
            await asyncio.sleep(delay_seconds)
            self.device_status["math_active"] = True
            await alarm_engine.trigger_alarm(question=math_q, solution=correct_ans)

        asyncio.create_task(_wait_and_ring())

        mins_left = int(delay_seconds // 60)
        secs_left = int(delay_seconds % 60)
        return {
            "success": True,
            "target_time": f"{target_h:02d}:{target_m:02d}",
            "delay_seconds": int(delay_seconds),
            "remaining": f"{mins_left}m {secs_left}s"
        }

    async def trigger_buzz(self, duration_ms: int = 250) -> Dict[str, Any]:
        dur = max(50, min(5000, duration_ms))
        await alarm_engine.trigger_buzz(duration=dur)
        return {"success": True, "duration_ms": dur}

    async def play_morse(self, pattern: str, unit_ms: int = 80, freq: int = 2400) -> Dict[str, Any]:
        return await alarm_engine.play_morse(pattern=pattern, unit_ms=unit_ms, freq=freq)

    async def stop_morse(self) -> Dict[str, Any]:
        return await alarm_engine.stop_morse()

    async def play_melody(self, notes: Optional[List[List[int]]] = None, title: str = "Melody") -> Dict[str, Any]:
        return await alarm_engine.play_melody(notes=notes, title=title)

    async def stop_melody(self) -> Dict[str, Any]:
        return await alarm_engine.stop_melody()

    def get_selected_display_device(self) -> Dict[str, Any]:
        with self._config_lock:
            return {
                "success": True,
                "selected_device_id": self.selected_display_device_id
            }

    def set_selected_display_device(self, device_id: Optional[str]) -> Dict[str, Any]:
        with self._config_lock:
            self.selected_display_device_id = str(device_id).strip() if device_id else None
            self._save_settings()
            return {
                "success": True,
                "selected_device_id": self.selected_display_device_id
            }

    def rename_device(self, device_id: str, new_name: str) -> bool:
        with self._config_lock:
            did = str(device_id).strip()
            name = str(new_name).strip()
            if not name or not did:
                return False
            if did in self._device_telemetry:
                self._device_telemetry[did]["device_name"] = name
            if self.laptop_status.get("device_id") == did or self.selected_display_device_id == did:
                self.laptop_status["device_name"] = name
            return True

    def report_device_telemetry(self, device_id: str, device_name: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
        with self._config_lock:
            now = time.time()
            clean_metrics = {
                "cpu": float(metrics.get("cpu", 0.0) or 0.0),
                "ram_pct": float(metrics.get("ram_pct", metrics.get("ram", 0.0)) or 0.0),
                "battery": int(metrics.get("battery", metrics.get("battery_pct", 0)) or 0),
                "temp": float(metrics.get("temp", metrics.get("temperature", 0.0)) or 0.0),
                "gpu": float(metrics.get("gpu", metrics.get("gpu_pct", 0.0)) or 0.0),
                "timestamp": float(metrics.get("timestamp", now) or now),
                "last_seen": now,
                "device_id": str(device_id),
                "device_name": str(device_name),
            }
            self._device_telemetry[str(device_id)] = clean_metrics
            # If no valid device selected yet, default to this reporting device
            if not self.selected_display_device_id or self.selected_display_device_id in ("unknown", "unpaired_computer"):
                self.selected_display_device_id = str(device_id)
            return {"success": True, "device_id": str(device_id), "timestamp": now}

    def get_active_telemetry(self) -> Dict[str, Any]:
        with self._config_lock:
            now = time.time()
            selected_id = self.selected_display_device_id
            selected_data = None
            is_online = False

            if selected_id and selected_id in self._device_telemetry:
                entry = self._device_telemetry[selected_id]
                if (now - entry.get("last_seen", 0)) <= 15.0:
                    is_online = True
                    selected_data = dict(entry)
                    selected_data["online"] = True
                else:
                    selected_data = dict(entry)
                    selected_data["online"] = False

            if not selected_data and self._device_telemetry:
                latest = max(self._device_telemetry.values(), key=lambda x: x.get("last_seen", 0), default=None)
                if latest:
                    is_online = (now - latest.get("last_seen", 0)) <= 15.0
                    selected_data = dict(latest)
                    selected_data["online"] = is_online
                    selected_id = latest.get("device_id")

            return {
                "success": True,
                "selected_device_id": selected_id,
                "is_online": is_online,
                "metrics": selected_data,
                "all_devices": {
                    d_id: {**d_val, "online": (now - d_val.get("last_seen", 0)) <= 15.0}
                    for d_id, d_val in self._device_telemetry.items()
                }
            }

    def get_alarms(self) -> List[Dict[str, Any]]:
        with self._config_lock:
            return list(self.alarms)

    def save_alarm(self, alarm_data: Dict[str, Any]) -> Dict[str, Any]:
        with self._config_lock:
            alarm_id = str(alarm_data.get("id") or uuid.uuid4())
            alarm_obj = {
                "id": alarm_id,
                "time": str(alarm_data.get("time", "07:00")),
                "label": str(alarm_data.get("label", "Alarm")),
                "enabled": bool(alarm_data.get("enabled", True)),
                "repeat_days": list(alarm_data.get("repeat_days", [])),
                "buzzer_duration": int(alarm_data.get("buzzer_duration", 250)),
                "math_challenge": bool(alarm_data.get("math_challenge", True)),
                "updated_at": int(time.time())
            }
            idx = next((i for i, a in enumerate(self.alarms) if a.get("id") == alarm_id), None)
            if idx is not None:
                self.alarms[idx] = alarm_obj
            else:
                self.alarms.append(alarm_obj)
            self._save_settings()
            return alarm_obj

    def delete_alarm(self, alarm_id: str) -> bool:
        with self._config_lock:
            initial_len = len(self.alarms)
            self.alarms = [a for a in self.alarms if a.get("id") != alarm_id]
            if len(self.alarms) != initial_len:
                self._save_settings()
                return True
            return False

    def toggle_alarm(self, alarm_id: str, enabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        with self._config_lock:
            for a in self.alarms:
                if a.get("id") == alarm_id:
                    if enabled is None:
                        a["enabled"] = not a.get("enabled", True)
                    else:
                        a["enabled"] = bool(enabled)
                    a["updated_at"] = int(time.time())
                    self._save_settings()
                    return a
            return None


# Global singleton instance
station_control = StationControlManager()
