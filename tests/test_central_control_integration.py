import unittest
import os
import sys
import json
import tempfile
import shutil

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_server_copy", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import station_control
from station_control import StationControlManager, parse_strict_darkmode_mode


class TestCentralControlIntegration(unittest.TestCase):
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
        parsed = json.loads(payload) if isinstance(payload, str) else payload
        self.published.append((topic, parsed))
        return type("Res", (), {"rc": 0})()

    def is_connected(self):
        return True

    def test_all_9_display_views(self):
        views = ("main", "stats", "tkb", "weather", "lyrics", "clock", "notes", "countdown", "focus")
        for v in views:
            res = self.sc.switch_view(v)
            self.assertTrue(res["success"])
            self.assertEqual(res["view"], v)
            last_topic, last_payload = self.published[-1]
            self.assertEqual(last_topic, "deskflow/display")
            self.assertEqual(last_payload.get("action"), "switch_view")
            self.assertEqual(last_payload.get("view"), v)

        # Invalid view raises ValueError
        with self.assertRaises(ValueError):
            self.sc.switch_view("invalid_view_xyz")

    def test_darkmode_scheduled_mode(self):
        # Test parser
        self.assertEqual(parse_strict_darkmode_mode("scheduled"), "scheduled")
        self.assertEqual(parse_strict_darkmode_mode("custom"), "custom")
        self.assertEqual(parse_strict_darkmode_mode("always"), "always_on")
        self.assertEqual(parse_strict_darkmode_mode("always_on"), "always_on")
        self.assertEqual(parse_strict_darkmode_mode("off"), "off")

        # Test update_settings
        res = self.sc.update_settings({
            "darkmode_mode": "scheduled",
            "darkmode_start": "22:00",
            "darkmode_end": "06:00"
        })
        self.assertEqual(self.sc.device_settings["darkmode_mode"], "scheduled")
        self.assertEqual(self.sc.device_settings["darkmode_start"], "22:00")
        self.assertEqual(self.sc.device_settings["darkmode_end"], "06:00")

        # Verify MQTT publish
        dark_publishes = [p for t, p in self.published if p.get("action") == "config_darkmode"]
        self.assertTrue(len(dark_publishes) > 0)
        self.assertEqual(dark_publishes[-1]["mode"], "scheduled")
        self.assertEqual(dark_publishes[-1]["start"], "22:00")
        self.assertEqual(dark_publishes[-1]["end"], "06:00")

    def test_notes_and_countdowns_dual_schema(self):
        # 1. Notes dual schema
        note = self.sc.add_note("Test content for note", title="Test Note")
        notes = self.sc.get_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], "Test content for note")
        self.assertEqual(notes[0]["content"], "Test content for note")
        self.assertEqual(notes[0]["title"], "Test Note")

        # 2. Countdowns dual schema
        cd = self.sc.add_countdown("New Year Event", "2026-12-31")
        cds = self.sc.get_countdowns()
        self.assertEqual(len(cds), 1)
        self.assertEqual(cds[0]["name"], "New Year Event")
        self.assertEqual(cds[0]["title"], "New Year Event")
        self.assertIn("days_left", cds[0])
        self.assertIn("is_pinned", cds[0])


if __name__ == "__main__":
    unittest.main()
