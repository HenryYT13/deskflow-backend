import unittest
import os
import sys
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_server_copy", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import station_control
from station_control import StationControlManager, VN_TZ

class TestNotesCountdownsWeather(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sc = StationControlManager()
        self.sc._data_dir = self.tmpdir
        self.sc._settings_file = os.path.join(self.tmpdir, "station_settings.json")
        self.sc.device_settings["hardware_config_version"] = 1
        self.sc.device_settings["applied_by_station"] = False
        self.sc.notes = []
        self.sc.countdowns = []
        self.sc.active_note_id = None
        self.sc.active_countdown_id = None
        self.published = []
        self.sc.set_mqtt_client(self)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload) if isinstance(payload, str) else payload))
        return type("Res", (), {"rc": 0})()

    def is_connected(self):
        return True

    def test_notes_crud_and_persistence(self):
        # 1. Add notes
        n1 = self.sc.add_note("Học thuộc 20 từ vựng tiếng Anh", title="Bài tập Tiếng Anh")
        self.assertEqual(n1["title"], "Bài tập Tiếng Anh")
        self.assertEqual(n1["content"], "Học thuộc 20 từ vựng tiếng Anh")
        self.assertFalse(n1["is_pinned"])

        n2 = self.sc.add_note("Ôn tập hình học không gian chương 2")
        self.assertTrue(n2["title"].startswith("Ôn tập hình học"))

        # 2. Get notes
        notes = self.sc.get_notes()
        self.assertEqual(len(notes), 2)

        # 3. Pin note to station
        pinned = self.sc.pin_note_to_station(n1["id"])
        self.assertTrue(pinned["is_pinned"])
        self.assertEqual(self.sc.active_note_id, n1["id"])
        
        # Verify MQTT publish
        last_topic, last_payload = self.published[-1]
        self.assertEqual(last_topic, "deskflow/display")
        self.assertEqual(last_payload["action"], "sticky_note")
        self.assertEqual(last_payload["id"], n1["id"])

        # 4. Verify persistence across reload
        sc_reloaded = StationControlManager()
        sc_reloaded._data_dir = self.tmpdir
        sc_reloaded._settings_file = os.path.join(self.tmpdir, "station_settings.json")
        sc_reloaded._load_settings()
        self.assertEqual(len(sc_reloaded.notes), 2)
        self.assertEqual(sc_reloaded.active_note_id, n1["id"])

        # 5. Delete note
        ok = self.sc.delete_note(n1["id"])
        self.assertTrue(ok)
        self.assertEqual(len(self.sc.get_notes()), 1)
        self.assertIsNone(self.sc.active_note_id)

    def test_countdowns_crud_and_date_handling(self):
        # 1. Add countdown with YYYY-MM-DD
        future_dt = datetime.now(VN_TZ) + timedelta(days=10)
        c1 = self.sc.add_countdown("Thi Tốt Nghiệp", future_dt.strftime("%Y-%m-%d"))
        self.assertEqual(c1["title"], "Thi Tốt Nghiệp")
        self.assertEqual(c1["days_left"], 10)
        self.assertFalse(c1["is_expired"])
        self.assertFalse(c1["is_active_on_station"])

        # 2. Add countdown with DD/MM
        c2 = self.sc.add_countdown("Kiểm tra 15p Toán", future_dt.strftime("%d/%m"))
        self.assertEqual(c2["days_left"], 10)

        # 3. Add expired countdown
        past_dt = datetime.now(VN_TZ) - timedelta(days=2)
        c3 = self.sc.add_countdown("Hạn nộp bài tập", past_dt.strftime("%Y-%m-%d"))
        self.assertTrue(c3["is_expired"])
        self.assertEqual(c3["days_left"], 0)

        # 4. Pin countdown to station
        pinned_cd = self.sc.set_active_countdown_on_station(c1["id"])
        self.assertTrue(pinned_cd["is_active_on_station"])
        self.assertEqual(self.sc.active_countdown_id, c1["id"])
        
        # Verify MQTT publish
        last_topic, last_payload = self.published[-1]
        self.assertEqual(last_topic, "deskflow/display")
        self.assertEqual(last_payload["action"], "exam_countdown")
        self.assertEqual(last_payload["name"], "Thi Tot Nghiep")
        self.assertEqual(last_payload["days_left"], 10)

        # 5. Verify persistence across reload
        sc_reloaded = StationControlManager()
        sc_reloaded._data_dir = self.tmpdir
        sc_reloaded._settings_file = os.path.join(self.tmpdir, "station_settings.json")
        sc_reloaded._load_settings()
        self.assertEqual(len(sc_reloaded.countdowns), 3)
        self.assertEqual(sc_reloaded.active_countdown_id, c1["id"])

        # 6. Delete countdown
        ok = self.sc.delete_countdown(c1["id"])
        self.assertTrue(ok)
        self.assertIsNone(self.sc.active_countdown_id)

    def test_weather_alert_time_update(self):
        res = self.sc.update_settings({"weather_alert_time": "07:30"})
        self.assertEqual(self.sc.device_settings["weather_alert_time"], "07:30")

    def test_darkmode_transitions(self):
        # 1. OFF -> ALWAYS ON
        res1 = self.sc.update_settings({"darkmode_mode": "always_on"})
        self.assertEqual(res1["darkmode_mode"], "always_on")
        last_topic, last_payload = self.published[-1]
        self.assertEqual(last_topic, "deskflow/display")
        self.assertEqual(last_payload["action"], "config_darkmode")
        self.assertEqual(last_payload["mode"], "always_on")

        # 2. ALWAYS ON -> SCHEDULED
        res2 = self.sc.update_settings({"darkmode_mode": "scheduled", "darkmode_start": "21:30", "darkmode_end": "06:30"})
        self.assertEqual(res2["darkmode_mode"], "scheduled")
        self.assertEqual(res2["darkmode_start"], "21:30")
        self.assertEqual(res2["darkmode_end"], "06:30")

        # 3. SCHEDULED -> OFF
        res3 = self.sc.update_settings({"darkmode_mode": "off"})
        self.assertEqual(res3["darkmode_mode"], "off")

    def test_hardware_settings_sync(self):
        # 1. Update from backend
        res = self.sc.update_settings({
            "led_brightness": 70,
            "buzzer_enabled": False,
            "buzzer_duration": 80
        })
        self.assertEqual(res["led_brightness"], 70)
        self.assertFalse(res["buzzer_enabled"])
        self.assertEqual(res["buzzer_duration"], 80)
        last_topic, last_payload = self.published[-1]
        self.assertEqual(last_topic, "deskflow/display")
        self.assertEqual(last_payload["action"], "hardware_settings")
        self.assertEqual(last_payload["led_brightness"], 70)
        self.assertFalse(last_payload["buzzer_enabled"])
        self.assertEqual(last_payload["buzzer_duration"], 80)

        # 2. Synchronize update from physical ESP32 (versioned from device interaction)
        cur_ver = self.sc.device_settings["hardware_config_version"]
        esp_update = {
            "action": "hardware_settings_update",
            "hardware_config_version": cur_ver + 1,
            "led_brightness": 50,
            "buzzer_enabled": True,
            "buzzer_duration": 40,
            "darkmode_mode": "always_on"
        }
        self.sc.on_hardware_settings_update(esp_update)
        self.assertEqual(self.sc.device_settings["led_brightness"], 50)
        self.assertTrue(self.sc.device_settings["buzzer_enabled"])
        self.assertEqual(self.sc.device_settings["buzzer_duration"], 40)
        self.assertEqual(self.sc.device_settings["darkmode_mode"], "always_on")

    def test_sync_notes_and_countdowns_payloads(self):
        self.sc.add_note(content="Test sync note 1")
        self.sc.add_note(content="Test sync note 2")
        self.assertTrue(self.sc.sync_notes_to_station())
        sync_topic, sync_payload = self.published[-1]
        self.assertEqual(sync_topic, "deskflow/display")
        self.assertEqual(sync_payload["action"], "sync_notes")
        self.assertEqual(len(sync_payload["notes"]), 2)

        self.sc.add_countdown(title="Kiem tra 15p", target_date_str="2026-11-20")
        self.assertTrue(self.sc.sync_countdowns_to_station())
        sync_cd_topic, sync_cd_payload = self.published[-1]
        self.assertEqual(sync_cd_topic, "deskflow/display")
        self.assertEqual(sync_cd_payload["action"], "sync_countdowns")
        self.assertEqual(len(sync_cd_payload["countdowns"]), 1)
        self.assertEqual(sync_cd_payload["countdowns"][0]["title"], "Kiem tra 15p")

if __name__ == "__main__":
    unittest.main()
