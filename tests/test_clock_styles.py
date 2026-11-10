import sys
import os
import unittest
import json
import tempfile
import shutil
from unittest.mock import MagicMock

# Add _server_copy/src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_server_copy", "src")))

from station_control import (
    StationControlManager,
    SUPPORTED_CLOCK_STYLES,
    DEFAULT_CLOCK_CONFIG
)


class TestClockStyles(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.mgr = StationControlManager()
        self.mgr._data_dir = self.tmp_dir
        self.mgr._settings_file = os.path.join(self.tmp_dir, "station_settings.json")
        self.mock_mqtt = MagicMock()
        self.mgr.set_mqtt_client(self.mock_mqtt)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_default_config(self):
        cfg = self.mgr.get_clock_config()
        self.assertEqual(cfg["style"], "digital_classic")
        self.assertEqual(cfg["time_format"], "24h")
        self.assertTrue(cfg["show_seconds"])
        self.assertTrue(cfg["show_date"])
        self.assertFalse(cfg["show_ampm"])
        self.assertEqual(cfg["language"], "vi")
        self.assertTrue(cfg["animation_enabled"])
        self.assertEqual(cfg["animation_speed"], 1.0)
        self.assertEqual(cfg["accent_color"], "#38BDF8")
        self.assertFalse(cfg["applied_by_station"])

    def test_all_seven_styles_accepted(self):
        for style in SUPPORTED_CLOCK_STYLES:
            cfg = self.mgr.set_clock_config({"style": style})
            self.assertEqual(cfg["style"], style)

    def test_legacy_style_mapping(self):
        cfg = self.mgr.set_clock_config({"style": "modern"})
        self.assertEqual(cfg["style"], "digital_classic")
        cfg = self.mgr.set_clock_config({"style": "minimal"})
        self.assertEqual(cfg["style"], "digital_minimal")

    def test_invalid_style_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.set_clock_config({"style": "rainbow_warp"})

    def test_time_format_validation(self):
        cfg = self.mgr.set_clock_config({"time_format": "12h"})
        self.assertEqual(cfg["time_format"], "12h")
        cfg = self.mgr.set_clock_config({"time_format": "24h"})
        self.assertEqual(cfg["time_format"], "24h")
        with self.assertRaises(ValueError):
            self.mgr.set_clock_config({"time_format": "48h"})

    def test_language_validation(self):
        cfg = self.mgr.set_clock_config({"language": "en"})
        self.assertEqual(cfg["language"], "en")
        cfg = self.mgr.set_clock_config({"language": "vi"})
        self.assertEqual(cfg["language"], "vi")
        with self.assertRaises(ValueError):
            self.mgr.set_clock_config({"language": "de"})

    def test_accent_color_and_speed(self):
        cfg = self.mgr.set_clock_config({
            "accent_color": "#ff5500",
            "animation_speed": 1.5
        })
        self.assertEqual(cfg["accent_color"], "#FF5500")
        self.assertEqual(cfg["animation_speed"], 1.5)

    def test_version_increments_and_applied_resets(self):
        v1 = self.mgr.get_clock_config()["config_version"]
        cfg = self.mgr.set_clock_config({"style": "analog"})
        self.assertEqual(cfg["config_version"], v1 + 1)
        self.assertFalse(cfg["applied_by_station"])

    def test_persistence_across_restart(self):
        self.mgr.set_clock_config({
            "style": "flip",
            "time_format": "12h",
            "language": "en",
            "accent_color": "#A855F7"
        })

        # Create new manager instance pointing to same file
        new_mgr = StationControlManager()
        new_mgr._data_dir = self.tmp_dir
        new_mgr._settings_file = os.path.join(self.tmp_dir, "station_settings.json")
        new_mgr._load_settings()

        loaded_cfg = new_mgr.get_clock_config()
        self.assertEqual(loaded_cfg["style"], "flip")
        self.assertEqual(loaded_cfg["time_format"], "12h")
        self.assertEqual(loaded_cfg["language"], "en")
        self.assertEqual(loaded_cfg["accent_color"], "#A855F7")

    def test_mqtt_publishing(self):
        self.mock_mqtt.reset_mock()
        self.mgr.set_clock_config({"style": "binary"})

        # Should publish to both deskflow/clock_style and deskflow/display
        self.assertEqual(self.mock_mqtt.publish.call_count, 2)
        topics = [call.args[0] for call in self.mock_mqtt.publish.call_args_list]
        self.assertIn("deskflow/clock_style", topics)
        self.assertIn("deskflow/display", topics)

    def test_clock_style_ack_matching(self):
        cfg = self.mgr.set_clock_config({"style": "neon" if False else "digital_neon"})
        cur_ver = cfg["config_version"]
        self.assertFalse(cfg["applied_by_station"])

        # Matching ACK arrives
        ack_payload = {
            "device_id": "DeskFlow-ESP32",
            "config_version": cur_ver,
            "status": "applied",
            "style": "digital_neon",
            "error": None
        }
        res = self.mgr.on_clock_style_ack(ack_payload)
        self.assertTrue(res)
        updated_cfg = self.mgr.get_clock_config()
        self.assertTrue(updated_cfg["applied_by_station"])
        self.assertEqual(updated_cfg["last_applied_style"], "digital_neon")

    def test_clock_style_ack_stale_rejected(self):
        cfg = self.mgr.set_clock_config({"style": "word"})
        cur_ver = cfg["config_version"]

        # Stale ACK for old version arrives
        stale_ack = {
            "device_id": "DeskFlow-ESP32",
            "config_version": cur_ver - 1,
            "status": "applied",
            "style": "analog",
            "error": None
        }
        res = self.mgr.on_clock_style_ack(stale_ack)
        self.assertFalse(res)
        self.assertFalse(self.mgr.get_clock_config()["applied_by_station"])

    def test_clock_style_reconnect_request(self):
        self.mock_mqtt.reset_mock()
        self.mgr.on_clock_style_request({"device_id": "DeskFlow-ESP32"})
        self.assertEqual(self.mock_mqtt.publish.call_count, 2)


if __name__ == "__main__":
    unittest.main()
