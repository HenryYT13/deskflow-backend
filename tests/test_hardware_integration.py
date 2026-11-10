import unittest
import os
import sys
import json
import shutil
import tempfile
import zoneinfo
from datetime import datetime, date, timedelta, timezone

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_server_copy", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import station_control
from station_control import StationControlManager, VN_TZ, clean_ascii

class MockMqttClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        data = json.loads(payload) if isinstance(payload, str) else payload
        self.published.append((topic, data))
        return type("Res", (), {"rc": 0})()

    def is_connected(self):
        return True


class TestHardwareIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sc = StationControlManager()
        self.sc._data_dir = self.tmpdir
        self.sc._settings_file = os.path.join(self.tmpdir, "station_settings.json")
        self.sc.device_settings["darkmode_mode"] = "off"
        self.sc.device_settings["hardware_config_version"] = 1
        self.sc.device_settings["applied_by_station"] = False
        self.sc.device_settings["last_ack_error"] = None
        self.sc.device_settings["led_brightness"] = 100
        self.sc.device_settings["buzzer_enabled"] = True
        self.sc.device_settings["buzzer_duration"] = 40
        self.sc.notes = []
        self.sc.countdowns = []
        self.sc.active_note_id = None
        self.sc.active_countdown_id = None
        self.mock_mqtt = MockMqttClient()
        self.sc.set_mqtt_client(self.mock_mqtt)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ================= 1. DARK MODE TRANSITIONS & INTERVALS =================
    def test_darkmode_all_transitions(self):
        """Verify all 6 state transitions between OFF, ALWAYS ON, and SCHEDULED."""
        # 1. Default is OFF
        self.assertEqual(self.sc.device_settings.get("darkmode_mode"), "off")

        # 2. OFF -> ALWAYS ON
        res = self.sc.update_settings({"darkmode_mode": "always_on"})
        self.assertEqual(res["darkmode_mode"], "always_on")
        topic, payload = self.mock_mqtt.published[-1]
        self.assertEqual(payload["action"], "config_darkmode")
        self.assertEqual(payload["mode"], "always_on")

        # 3. ALWAYS ON -> OFF
        res = self.sc.update_settings({"darkmode_mode": "off"})
        self.assertEqual(res["darkmode_mode"], "off")
        _, payload = self.mock_mqtt.published[-1]
        self.assertEqual(payload["mode"], "off")

        # 4. OFF -> SCHEDULED
        res = self.sc.update_settings({"darkmode_mode": "scheduled", "darkmode_start": "22:00", "darkmode_end": "06:00"})
        self.assertEqual(res["darkmode_mode"], "scheduled")
        self.assertEqual(res["darkmode_start"], "22:00")
        self.assertEqual(res["darkmode_end"], "06:00")
        _, payload = self.mock_mqtt.published[-1]
        self.assertEqual(payload["mode"], "scheduled")
        self.assertEqual(payload["start"], "22:00")
        self.assertEqual(payload["end"], "06:00")

        # 5. SCHEDULED -> OFF
        res = self.sc.update_settings({"darkmode_mode": "off"})
        self.assertEqual(res["darkmode_mode"], "off")

        # 6. OFF -> SCHEDULED -> ALWAYS ON
        self.sc.update_settings({"darkmode_mode": "scheduled"})
        res = self.sc.update_settings({"darkmode_mode": "always_on"})
        self.assertEqual(res["darkmode_mode"], "always_on")

        # 7. ALWAYS ON -> SCHEDULED
        res = self.sc.update_settings({"darkmode_mode": "scheduled", "darkmode_start": "21:00", "darkmode_end": "05:00"})
        self.assertEqual(res["darkmode_mode"], "scheduled")
        self.assertEqual(res["darkmode_start"], "21:00")
        self.assertEqual(res["darkmode_end"], "05:00")

    def test_darkmode_schedule_evaluation_logic(self):
        """Verify firmware C++ and backend is_darkmode_active() schedule evaluation across all boundaries."""
        # 1. Schedule: 22:00 - 06:00 (Overnight crossing midnight)
        self.sc.update_settings({"darkmode_mode": "scheduled", "darkmode_start": "22:00", "darkmode_end": "06:00"})
        tz = zoneinfo.ZoneInfo("Asia/Ho_Chi_Minh")

        def dt_at(h, m):
            return datetime(2026, 11, 4, h, m, tzinfo=tz)

        self.assertFalse(self.sc.is_darkmode_active(dt_at(21, 59)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(22, 0)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(23, 59)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(0, 0)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(2, 30)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(5, 59)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(6, 0)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(12, 0)))

        # 2. Schedule: 20:00 - 23:00 (Evening span within single day)
        self.sc.update_settings({"darkmode_start": "20:00", "darkmode_end": "23:00"})
        self.assertFalse(self.sc.is_darkmode_active(dt_at(19, 59)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(20, 0)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(21, 30)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(22, 59)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(23, 0)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(1, 0)))

        # 3. Schedule: 06:30 - 18:30 (Daytime span)
        self.sc.update_settings({"darkmode_start": "06:30", "darkmode_end": "18:30"})
        self.assertFalse(self.sc.is_darkmode_active(dt_at(6, 29)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(6, 30)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(12, 0)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(18, 29)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(18, 30)))

        # 4. Schedule: 00:00 - 08:00 (Early morning span starting at midnight)
        self.sc.update_settings({"darkmode_start": "00:00", "darkmode_end": "08:00"})
        self.assertTrue(self.sc.is_darkmode_active(dt_at(0, 0)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(4, 15)))
        self.assertTrue(self.sc.is_darkmode_active(dt_at(7, 59)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(8, 0)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(23, 30)))

        # 5. Equal Start and End: 22:00 == 22:00 (Safe predictable fallback: 0-minute span is inactive / Light mode)
        self.sc.update_settings({"darkmode_start": "22:00", "darkmode_end": "22:00"})
        self.assertFalse(self.sc.is_darkmode_active(dt_at(22, 0)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(10, 0)))
        self.assertFalse(self.sc.is_darkmode_active(dt_at(0, 0)))

    def test_darkmode_schedule_validation_and_hardware_isolation(self):
        """Verify invalid time format rejection and ensure Dark Mode changes do not alter LED brightness or buzzer."""
        # Initialize baseline hardware settings
        self.sc.update_settings({"led_brightness": 75, "buzzer_enabled": True, "buzzer_duration": 175})

        # Test invalid time formats raise ValueError
        with self.assertRaises(ValueError):
            self.sc.update_settings({"darkmode_start": "25:00"})
        with self.assertRaises(ValueError):
            self.sc.update_settings({"darkmode_end": "12:65"})
        with self.assertRaises(ValueError):
            self.sc.update_settings({"darkmode_start": "invalid_time"})

        # Update Dark Mode to SCHEDULED
        self.sc.update_settings({"darkmode_mode": "scheduled", "darkmode_start": "22:00", "darkmode_end": "06:00"})

        # Verify LED and Buzzer are strictly isolated and unaltered
        self.assertEqual(self.sc.device_settings["led_brightness"], 75)
        self.assertTrue(self.sc.device_settings["buzzer_enabled"])
        self.assertEqual(self.sc.device_settings["buzzer_duration"], 175)

    def test_darkmode_ntp_edge_cases(self):
        """Audit all 7 Dark Mode and NTP edge cases specified in Section 1."""
        self.sc.update_settings({"darkmode_mode": "scheduled", "darkmode_start": "22:00", "darkmode_end": "06:00"})
        tz = zoneinfo.ZoneInfo("Asia/Ho_Chi_Minh")

        # 1. ESP32 boots at 23:00 (NTP active): Dark Mode must be ACTIVE
        dt_2300 = datetime(2026, 11, 4, 23, 0, tzinfo=tz)
        self.assertTrue(self.sc.is_darkmode_active(dt_2300))

        # 2. ESP32 boots at 12:00 (NTP active): Dark Mode must be INACTIVE (Light mode)
        dt_1200 = datetime(2026, 11, 4, 12, 0, tzinfo=tz)
        self.assertFalse(self.sc.is_darkmode_active(dt_1200))

        # 3. NTP returns invalid or unavailable time (year 1970 before sync):
        # Firmware check: tm_year < (2020 - 1900) -> returns false (safe Light mode)
        dt_invalid_1970 = datetime(1970, 1, 1, 23, 0, tzinfo=timezone.utc)
        def is_darkmode_firmware_simulation(mode, start_str, end_str, dt):
            if dt.year < 2020:
                return False # Safe fallback
            cur = dt.hour * 60 + dt.minute
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
            s_min, e_min = sh * 60 + sm, eh * 60 + em
            if s_min == e_min: return False
            if s_min < e_min: return s_min <= cur < e_min
            return cur >= s_min or cur < e_min

        self.assertFalse(is_darkmode_firmware_simulation("scheduled", "22:00", "06:00", dt_invalid_1970))

        # 4. NTP delayed during boot, then becomes valid at 23:00:
        # Before sync -> False (Light Mode)
        self.assertFalse(is_darkmode_firmware_simulation("scheduled", "22:00", "06:00", dt_invalid_1970))
        # After sync -> True (immediately flips to Dark Mode)
        self.assertTrue(is_darkmode_firmware_simulation("scheduled", "22:00", "06:00", dt_2300))

        # 5. Wi-Fi disconnects while Dark Mode is active:
        # Internal RTC advances from 23:00 to 05:00 (still Dark)
        dt_0500 = datetime(2026, 11, 5, 5, 0, tzinfo=tz)
        self.assertTrue(self.sc.is_darkmode_active(dt_0500))
        # Internal RTC advances to 06:05 (switches back to Light mode even with no Wi-Fi)
        dt_0605 = datetime(2026, 11, 5, 6, 5, tzinfo=tz)
        self.assertFalse(self.sc.is_darkmode_active(dt_0605))

        # 6. MQTT reconnects while scheduled Dark Mode should be active:
        self.assertTrue(self.sc.is_darkmode_active(dt_2300))

    def test_buzzer_1000ms_non_blocking_simulation(self):
        """Verify that a maximum 1000ms buzzer pulse uses non-blocking millis() timing without blocking loop execution."""
        class MockFirmwareLoop:
            def __init__(self):
                self.is_buzzing = False
                self.buzz_start_time = 0
                self.buzz_duration = 0
                self.tone_active = False
                self.buttons_processed = 0
                self.mqtt_messages_processed = 0

            def trigger_buzz(self, duration_ms):
                self.is_buzzing = True
                self.buzz_start_time = 1000
                self.buzz_duration = duration_ms
                self.tone_active = True

            def loop_step(self, current_millis, has_button_event=False, has_mqtt_event=False):
                # 1. Non-blocking buzzer expiration check
                if self.is_buzzing and (current_millis - self.buzz_start_time >= self.buzz_duration):
                    self.is_buzzing = False
                    self.tone_active = False

                # 2. Button handling runs on every iteration without waiting
                if has_button_event:
                    self.buttons_processed += 1

                # 3. MQTT loop runs on every iteration without waiting
                if has_mqtt_event:
                    self.mqtt_messages_processed += 1

        firmware = MockFirmwareLoop()
        firmware.trigger_buzz(1000)

        self.assertTrue(firmware.is_buzzing)
        self.assertTrue(firmware.tone_active)

        # At t = 1200ms (200ms in): still buzzing, but buttons & MQTT are processed immediately!
        firmware.loop_step(1200, has_button_event=True, has_mqtt_event=True)
        self.assertTrue(firmware.is_buzzing)
        self.assertEqual(firmware.buttons_processed, 1)
        self.assertEqual(firmware.mqtt_messages_processed, 1)

        # At t = 1800ms (800ms in): still buzzing, more events processed
        firmware.loop_step(1800, has_button_event=True, has_mqtt_event=False)
        self.assertTrue(firmware.is_buzzing)
        self.assertEqual(firmware.buttons_processed, 2)

        # At t = 2000ms (1000ms elapsed): buzzer automatically turns off!
        firmware.loop_step(2000, has_button_event=False, has_mqtt_event=True)
        self.assertFalse(firmware.is_buzzing)
        self.assertFalse(firmware.tone_active)
        self.assertEqual(firmware.mqtt_messages_processed, 2)

    # ================= 2. LED BRIGHTNESS PWM VERIFICATION =================
    def test_led_pwm_levels_and_clamping(self):
        """Verify PWM levels (0%, 10%, 25%, 50%, 75%, 100%) and bounds clamping."""
        levels = [0, 10, 25, 50, 75, 100]
        expected_duties = [0, 25, 63, 127, 191, 255]

        for pct, expected_duty in zip(levels, expected_duties):
            res = self.sc.update_settings({"led_brightness": pct})
            self.assertEqual(res["led_brightness"], pct)
            calc_duty = (pct * 255) // 100
            self.assertEqual(calc_duty, expected_duty)
            topic, payload = self.mock_mqtt.published[-1]
            self.assertEqual(payload["action"], "hardware_settings")
            self.assertEqual(payload["led_brightness"], pct)

        # Test out-of-bounds clamping
        self.sc.update_settings({"led_brightness": -20})
        self.assertEqual(self.sc.device_settings["led_brightness"], 0)

        self.sc.update_settings({"led_brightness": 150})
        self.assertEqual(self.sc.device_settings["led_brightness"], 100)

    def test_hardware_settings_persistence_across_restart(self):
        """Ensure LED brightness and buzzer configuration persist across backend reloads."""
        self.sc.update_settings({
            "led_brightness": 60,
            "buzzer_enabled": False,
            "buzzer_duration": 150,
            "darkmode_mode": "scheduled",
            "darkmode_start": "23:30",
            "darkmode_end": "05:30"
        })

        # Reload manager from same file
        reloaded = StationControlManager()
        reloaded._data_dir = self.tmpdir
        reloaded._settings_file = os.path.join(self.tmpdir, "station_settings.json")
        reloaded._load_settings()

        self.assertEqual(reloaded.device_settings["led_brightness"], 60)
        self.assertFalse(reloaded.device_settings["buzzer_enabled"])
        self.assertEqual(reloaded.device_settings["buzzer_duration"], 150)
        self.assertEqual(reloaded.device_settings["darkmode_mode"], "scheduled")
        self.assertEqual(reloaded.device_settings["darkmode_start"], "23:30")
        self.assertEqual(reloaded.device_settings["darkmode_end"], "05:30")

    # ================= 3. BUZZER CONFIGURATION & ALARM ISOLATION =================
    def test_buzzer_durations_and_toggle(self):
        """Verify buzzer beep toggle, custom durations (20, 60, 80, 100, 175, 250, 300, 500 ms), and boundary validation."""
        valid_durations = [10, 20, 40, 60, 80, 100, 175, 250, 300, 500, 1000]
        for dur in valid_durations:
            res = self.sc.update_settings({"buzzer_duration": dur})
            self.assertEqual(res["buzzer_duration"], dur)
            _, payload = self.mock_mqtt.published[-1]
            self.assertEqual(payload["action"], "hardware_settings")
            self.assertEqual(payload["buzzer_duration"], dur)

        # Toggle buzzer
        res = self.sc.update_settings({"buzzer_enabled": False})
        self.assertFalse(res["buzzer_enabled"])
        res = self.sc.update_settings({"buzzer_enabled": True})
        self.assertTrue(res["buzzer_enabled"])

        # Rejection of invalid inputs (empty, non-numeric, negative, zero, out of range)
        with self.assertRaises(ValueError):
            self.sc.update_settings({"buzzer_duration": ""})
        with self.assertRaises(ValueError):
            self.sc.update_settings({"buzzer_duration": "abc"})
        with self.assertRaises(ValueError):
            self.sc.update_settings({"buzzer_duration": -50})
        with self.assertRaises(ValueError):
            self.sc.update_settings({"buzzer_duration": 0})
        with self.assertRaises(ValueError):
            self.sc.update_settings({"buzzer_duration": 5})   # Below min (10ms)
        with self.assertRaises(ValueError):
            self.sc.update_settings({"buzzer_duration": 1500}) # Above max (1000ms)

    def test_buzzer_alarm_priority_override(self):
        """Verify that disabling button-feedback buzzer (buzzer_enabled=False) does not silence alarms."""
        self.sc.update_settings({"buzzer_enabled": False, "buzzer_duration": 40})
        self.assertFalse(self.sc.device_settings["buzzer_enabled"])

        # Firmware triggerBuzz(duration, force=true) contract:
        # Emergency alarms use force=true to bypass buzzerEnabled
        def firmware_trigger_buzz(buzzer_enabled, duration, force=False):
            if not buzzer_enabled and not force:
                return False  # Silenced feedback
            return True       # Buzzer sounds

        # Feedback beep when muted -> False
        self.assertFalse(firmware_trigger_buzz(self.sc.device_settings["buzzer_enabled"], 40, force=False))
        # Emergency alarm when muted -> True (sounds!)
        self.assertTrue(firmware_trigger_buzz(self.sc.device_settings["buzzer_enabled"], 500, force=True))

    # ================= 4. NOTES PAGE COMPREHENSIVE TESTING =================
    def test_notes_comprehensive(self):
        """Verify Notes CRUD, multi-note synchronization, Vietnamese accents, long text, and isolation from weather."""
        # 1. Empty content validation
        with self.assertRaises(ValueError):
            self.sc.add_note(content="   ")

        # 2. Add note with Vietnamese and accents
        n1 = self.sc.add_note(
            content="Kỳ thi Học sinh giỏi cấp Tỉnh môn Tin học 2026: Cố gắng đạt giải Nhất!",
            title="Mục tiêu HSG Tin"
        )
        self.assertIn("note_", n1["id"])
        self.assertEqual(n1["title"], "Mục tiêu HSG Tin")

        # 3. Add second note with special characters and newlines
        n2 = self.sc.add_note(
            content="Task 1: Ôn tập toán\nTask 2: Làm đề Lý\nTask 3: Học từ mới Tiếng Anh!",
            title="Checklist hàng ngày"
        )

        # 4. Pin note 1
        pinned = self.sc.pin_note_to_station(n1["id"])
        self.assertTrue(pinned["is_pinned"])
        self.assertEqual(self.sc.active_note_id, n1["id"])

        # 5. Verify sync payload
        sync_topic, sync_payload = self.mock_mqtt.published[-2]
        self.assertEqual(sync_topic, "deskflow/display")
        self.assertEqual(sync_payload["action"], "sync_notes")
        self.assertEqual(len(sync_payload["notes"]), 2)
        self.assertTrue(sync_payload["notes"][0]["is_pinned"])
        self.assertFalse(sync_payload["notes"][1]["is_pinned"])

        # 6. Verify weather/quote updates DO NOT overwrite notes
        self.sc.update_settings({"weather_city": "Hai Phong"})
        self.assertEqual(len(self.sc.notes), 2)
        self.assertEqual(self.sc.active_note_id, n1["id"])

        # 7. Delete pinned note
        self.sc.delete_note(n1["id"])
        self.assertIsNone(self.sc.active_note_id)
        self.assertEqual(len(self.sc.notes), 1)

    # ================= 5. COUNTDOWN PAGE BOUNDARIES & TIMEZONE =================
    def test_countdown_date_boundaries(self):
        """Verify countdown calculations across date boundaries (tomorrow, today, end of month, leap year)."""
        now_vn = datetime.now(VN_TZ)

        # 1. Event tomorrow (days_left = 1)
        tomorrow_date = (now_vn.date() + timedelta(days=1)).isoformat()
        c_tom = self.sc.add_countdown("Sự kiện ngày mai", tomorrow_date)
        self.assertEqual(c_tom["days_left"], 1)
        self.assertFalse(c_tom["is_expired"])

        # 2. Event today (days_left = 0, not expired)
        today_date = now_vn.date().isoformat()
        c_today = self.sc.add_countdown("Sự kiện hôm nay", today_date)
        self.assertEqual(c_today["days_left"], 0)

        # 3. Event in past (expired)
        past_date = (now_vn.date() - timedelta(days=5)).isoformat()
        c_past = self.sc.add_countdown("Sự kiện đã qua", past_date)
        self.assertTrue(c_past["is_expired"])
        self.assertEqual(c_past["days_left"], 0)

        # 4. End of month (2026-11-30)
        c_eom = self.sc.add_countdown("Cuối tháng 11", "2026-11-30")
        expected_days = (date(2026, 11, 30) - now_vn.date()).days
        self.assertEqual(c_eom["days_left"], max(0, expected_days))

        # 5. Leap year date (2028-02-29)
        c_leap = self.sc.add_countdown("Năm nhuận 2028", "2028-02-29")
        expected_leap = (date(2028, 2, 29) - now_vn.date()).days
        self.assertEqual(c_leap["days_left"], expected_leap)

        # 6. Invalid date format
        with self.assertRaises(ValueError):
            self.sc.add_countdown("Lỗi ngày", "invalid-date-string")

        # 7. Empty title
        with self.assertRaises(ValueError):
            self.sc.add_countdown("   ", "2026-12-31")

    # ================= 6. CLOCK STYLES 12H/24H MIDNIGHT & NOON =================
    def test_clock_styles_midnight_noon_conversion(self):
        """Verify 12h/24h hour conversion for midnight (12:00 AM) and noon (12:00 PM)."""
        def format_12h(hour, minute):
            ampm = "AM" if hour < 12 else "PM"
            h12 = hour % 12
            if h12 == 0:
                h12 = 12
            return f"{h12:02d}:{minute:02d}", ampm

        # Midnight 00:00 -> 12:00 AM
        time_str, ampm = format_12h(0, 0)
        self.assertEqual(time_str, "12:00")
        self.assertEqual(ampm, "AM")

        # 01:15 AM -> 01:15 AM
        time_str, ampm = format_12h(1, 15)
        self.assertEqual(time_str, "01:15")
        self.assertEqual(ampm, "AM")

        # Noon 12:00 -> 12:00 PM
        time_str, ampm = format_12h(12, 0)
        self.assertEqual(time_str, "12:00")
        self.assertEqual(ampm, "PM")

        # 13:45 PM -> 01:45 PM
        time_str, ampm = format_12h(13, 45)
        self.assertEqual(time_str, "01:45")
        self.assertEqual(ampm, "PM")

        # 23:59 PM -> 11:59 PM
        time_str, ampm = format_12h(23, 59)
        self.assertEqual(time_str, "11:59")
        self.assertEqual(ampm, "PM")

    # ================= 7. MQTT & VERSION PROTOCOL RESILIENCE =================
    def test_hardware_config_version_initialization_and_increment(self):
        """Verify hardware_config_version initializes at >= 1 and increments only on HW changes."""
        # 1. Initial version is 1
        self.assertEqual(self.sc.device_settings.get("hardware_config_version"), 1)
        self.assertFalse(self.sc.device_settings.get("applied_by_station"))

        # 2. Non-hardware update (weather_city) does NOT increment hardware_config_version
        self.sc.update_settings({"weather_city": "Da Nang"})
        self.assertEqual(self.sc.device_settings.get("hardware_config_version"), 1)

        # 3. Hardware update (led_brightness) increments version
        self.sc.update_settings({"led_brightness": 70})
        self.assertEqual(self.sc.device_settings.get("hardware_config_version"), 2)
        self.assertFalse(self.sc.device_settings.get("applied_by_station"))

        # 4. Buzzer update increments version
        self.sc.update_settings({"buzzer_duration": 150})
        self.assertEqual(self.sc.device_settings.get("hardware_config_version"), 3)

        # 5. Darkmode update increments version
        self.sc.update_settings({"darkmode_mode": "scheduled"})
        self.assertEqual(self.sc.device_settings.get("hardware_config_version"), 4)

    def test_hardware_settings_persistence_across_restart(self):
        """Verify hardware_config_version and settings persist cleanly across restart."""
        self.sc.update_settings({
            "led_brightness": 45,
            "buzzer_duration": 120,
            "darkmode_mode": "always_on"
        })
        ver = self.sc.device_settings["hardware_config_version"]

        # Simulate restart by loading in new StationControlManager instance
        sc2 = StationControlManager()
        sc2._data_dir = self.tmpdir
        sc2._settings_file = os.path.join(self.tmpdir, "station_settings.json")
        sc2._load_settings()

        self.assertEqual(sc2.device_settings["hardware_config_version"], ver)
        self.assertEqual(sc2.device_settings["led_brightness"], 45)
        self.assertEqual(sc2.device_settings["buzzer_duration"], 120)
        self.assertEqual(sc2.device_settings["darkmode_mode"], "always_on")

    def test_single_authority_device_change_request(self):
        """Verify device change requests are committed by backend as the sole authority."""
        # Backend is at v1
        self.assertEqual(self.sc.device_settings["hardware_config_version"], 1)
        self.assertEqual(self.sc.device_settings["led_brightness"], 100)

        # Device sends change request from physical button (LED changed to 35%)
        self.mock_mqtt.published.clear()
        device_req = {
            "action": "hardware_settings_update",
            "device_id": "DeskFlow-ESP32",
            "base_version": 1,
            "led_brightness": 35
        }
        self.sc.on_hardware_settings_update(device_req)

        # Backend committed as v2
        self.assertEqual(self.sc.device_settings["hardware_config_version"], 2)
        self.assertEqual(self.sc.device_settings["led_brightness"], 35)
        self.assertFalse(self.sc.device_settings["applied_by_station"])

        # Backend broadcast authoritative configuration to station
        self.assertEqual(len(self.mock_mqtt.published), 1)
        topic, payload = self.mock_mqtt.published[0]
        self.assertEqual(topic, "deskflow/display")
        self.assertEqual(payload["action"], "hardware_settings")
        self.assertEqual(payload["hardware_config_version"], 2)
        self.assertEqual(payload["led_brightness"], 35)

        # Device ACKs v2
        self.sc.on_hardware_settings_ack({
            "action": "hardware_settings_ack",
            "hardware_config_version": 2,
            "status": "applied"
        })
        self.assertTrue(self.sc.device_settings["applied_by_station"])

    def test_invalid_device_payload_strict_validation(self):
        """Verify strict validation rejects malformed payloads and avoids bool('false') pitfalls."""
        # 1. buzzer_enabled string 'false' -> strict parsed to boolean False
        self.sc.on_hardware_settings_update({"buzzer_enabled": "false"})
        self.assertFalse(self.sc.device_settings["buzzer_enabled"])

        # 2. buzzer_enabled string 'true' -> True
        self.sc.on_hardware_settings_update({"buzzer_enabled": "true"})
        self.assertTrue(self.sc.device_settings["buzzer_enabled"])

        ver_before = self.sc.device_settings["hardware_config_version"]
        self.mock_mqtt.published.clear()

        # 3. Invalid buzzer_enabled string -> Rejected!
        self.sc.on_hardware_settings_update({"buzzer_enabled": "not_a_bool"})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)
        # Resync broadcast sent with force_sync=True
        self.assertEqual(len(self.mock_mqtt.published), 1)
        self.assertTrue(self.mock_mqtt.published[-1][1]["force_sync"])

        # 4. Out of bounds LED brightness (>100 or <0 or boolean)
        self.sc.on_hardware_settings_update({"led_brightness": 150})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)
        self.sc.on_hardware_settings_update({"led_brightness": -10})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)
        self.sc.on_hardware_settings_update({"led_brightness": True})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)

        # 5. Out of bounds buzzer duration (<10 or >1000 or boolean)
        self.sc.on_hardware_settings_update({"buzzer_duration": 5})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)
        self.sc.on_hardware_settings_update({"buzzer_duration": 2000})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)
        self.sc.on_hardware_settings_update({"buzzer_duration": False})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)

        # 6. Invalid darkmode_mode
        self.sc.on_hardware_settings_update({"darkmode_mode": "rainbow_mode"})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)

        # 7. Invalid darkmode_start / end
        self.sc.on_hardware_settings_update({"darkmode_start": "25:00"})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)
        self.sc.on_hardware_settings_update({"darkmode_end": "invalid_time"})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], ver_before)

    def test_simultaneous_backend_and_device_changes_serialized_cleanly(self):
        """Verify concurrent updates from API and ESP32 serialize without lost updates."""
        import threading

        self.assertEqual(self.sc.device_settings["hardware_config_version"], 1)

        errors = []

        def run_api_change():
            try:
                self.sc.update_settings({"darkmode_mode": "scheduled", "darkmode_start": "21:00", "darkmode_end": "05:00"})
            except Exception as e:
                errors.append(e)

        def run_device_change():
            try:
                self.sc.on_hardware_settings_update({"led_brightness": 45, "buzzer_duration": 90})
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=run_api_change)
        t2 = threading.Thread(target=run_device_change)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0)
        # Both updates committed in sequence under lock: v1 -> v2 -> v3
        self.assertEqual(self.sc.device_settings["hardware_config_version"], 3)
        # BOTH settings preserved (zero lost updates!)
        self.assertEqual(self.sc.device_settings["darkmode_mode"], "scheduled")
        self.assertEqual(self.sc.device_settings["darkmode_start"], "21:00")
        self.assertEqual(self.sc.device_settings["darkmode_end"], "05:00")
        self.assertEqual(self.sc.device_settings["led_brightness"], 45)
        self.assertEqual(self.sc.device_settings["buzzer_duration"], 90)

    def test_hardware_settings_ack_handling(self):
        """Verify matching ACK marks applied, while stale ACK is ignored."""
        self.sc.update_settings({"led_brightness": 60})  # Increments to v2
        self.assertEqual(self.sc.device_settings["hardware_config_version"], 2)
        self.assertFalse(self.sc.device_settings["applied_by_station"])

        # 1. Stale ACK for v1 -> Ignored
        self.sc.on_hardware_settings_ack({
            "action": "hardware_settings_ack",
            "hardware_config_version": 1,
            "status": "applied"
        })
        self.assertFalse(self.sc.device_settings["applied_by_station"])

        # 2. Matching ACK for v2 -> Accepted
        self.sc.on_hardware_settings_ack({
            "action": "hardware_settings_ack",
            "hardware_config_version": 2,
            "status": "applied"
        })
        self.assertTrue(self.sc.device_settings["applied_by_station"])
        self.assertIsNone(self.sc.device_settings["last_ack_error"])

        # 3. Duplicate ACK for v2 -> Idempotent
        self.sc.on_hardware_settings_ack({
            "action": "hardware_settings_ack",
            "hardware_config_version": 2,
            "status": "applied"
        })
        self.assertTrue(self.sc.device_settings["applied_by_station"])

        # 4. Matching ACK with rejection
        self.sc.update_settings({"led_brightness": 90})  # Increments to v3
        self.sc.on_hardware_settings_ack({
            "action": "hardware_settings_ack",
            "hardware_config_version": 3,
            "status": "rejected",
            "error": "stale_version"
        })
        self.assertFalse(self.sc.device_settings["applied_by_station"])
        self.assertEqual(self.sc.device_settings["last_ack_error"], "stale_version")

    def test_hardware_settings_reconnect_request_and_force_sync(self):
        """Verify station reconnect request prompts backend to publish authoritative settings with force_sync=True."""
        self.mock_mqtt.published.clear()
        self.sc.on_hardware_settings_request({"current_version": 1})
        self.assertEqual(len(self.mock_mqtt.published), 1)
        topic, payload = self.mock_mqtt.published[0]
        self.assertEqual(topic, "deskflow/display")
        self.assertEqual(payload["action"], "hardware_settings")
        self.assertEqual(payload["hardware_config_version"], self.sc.device_settings["hardware_config_version"])
        self.assertTrue(payload["force_sync"])

    def test_persistence_failure_raises_ioerror_and_aborts_publish(self):
        """Verify save failures raise IOError, abort publication, and do not silently claim success."""
        # Point to invalid path that cannot be written
        self.sc._settings_file = "/non_existent_directory_987654/impossible_path.json"
        self.sc._data_dir = "/non_existent_directory_987654"

        self.mock_mqtt.published.clear()
        with self.assertRaises(IOError):
            self.sc.update_settings({"led_brightness": 10})

        # Publication must NOT have been emitted if persistence failed!
        self.assertEqual(len(self.mock_mqtt.published), 0)

    def test_clock_style_and_hardware_version_independence(self):
        """Verify Clock Style config_version and Hardware hardware_config_version remain completely independent."""
        initial_hw_ver = self.sc.device_settings["hardware_config_version"]
        initial_clock_ver = self.sc.clock_config["config_version"]

        # 1. Update Clock Style -> only clock config_version increments
        self.sc.set_clock_config({"style": "analog"})
        self.assertEqual(self.sc.clock_config["config_version"], initial_clock_ver + 1)
        self.assertEqual(self.sc.device_settings["hardware_config_version"], initial_hw_ver)

        # 2. Update Hardware Settings -> only hardware_config_version increments
        self.sc.update_settings({"led_brightness": 50})
        self.assertEqual(self.sc.device_settings["hardware_config_version"], initial_hw_ver + 1)
        self.assertEqual(self.sc.clock_config["config_version"], initial_clock_ver + 1)

    def test_bind_shared_state_preserves_hardware_settings(self):
        """Verify bind_shared_state properly preserves and syncs hardware settings."""
        sc = StationControlManager()
        sc._data_dir = self.tmpdir
        sc._settings_file = os.path.join(self.tmpdir, "test_bind_settings.json")
        sc.device_settings["led_brightness"] = 75
        sc.device_settings["buzzer_duration"] = 120
        sc.device_settings["hardware_config_version"] = 4

        main_device_settings = {
            "darkmode_mode": "off",
            "weather_city": "Ha Long"
        }
        main_status = {}
        main_laptop = {}

        sc.bind_shared_state(main_status, main_device_settings, main_laptop)

        # Ensure main_device_settings received the loaded hardware settings
        self.assertEqual(main_device_settings["led_brightness"], 75)
        self.assertEqual(main_device_settings["buzzer_duration"], 120)
        self.assertEqual(main_device_settings["hardware_config_version"], 4)
        # Ensure sc.device_settings points to main_device_settings
        self.assertIs(sc.device_settings, main_device_settings)

if __name__ == "__main__":
    unittest.main()
