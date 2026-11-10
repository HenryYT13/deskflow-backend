"""
Regression tests for DeskFlow Backend:
- Verify existing Discord commands remain registered and untouched:
  alarm, solve, close_tkb, buzz, led, quote, quote_schedule
- Verify original /solve behavior is preserved
- Verify MQTT topics and configuration remain unchanged
- Verify PocketBase configuration and client remain unchanged
- Verify Spotify service and scheduler setup remain unchanged
"""

import os
import unittest
import importlib

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

import config


class TestBackendRegression(unittest.TestCase):
    def test_mqtt_topics_and_config_unchanged(self):
        # Verify all existing MQTT topics are preserved exactly
        self.assertEqual(config.TOPIC_ALARM, "deskflow/alarm")
        self.assertEqual(config.TOPIC_SOLVE, "deskflow/solve")
        self.assertEqual(config.TOPIC_CLOSE_TKB, "deskflow/close_tkb")
        self.assertEqual(config.TOPIC_TIMER, "deskflow/timer")
        self.assertEqual(config.TOPIC_EXAM, "deskflow/exam")
        self.assertEqual(config.TOPIC_LAMP, "deskflow/lamp")
        self.assertEqual(config.TOPIC_CLOCK_STYLE, "deskflow/clock_style")
        self.assertEqual(config.TOPIC_SAY, "deskflow/say")
        self.assertEqual(config.TOPIC_QUOTE, "deskflow/quote")
        self.assertEqual(config.TOPIC_CONFIG, "deskflow/config")
        self.assertEqual(config.TOPIC_STATUS_UPDATE, "deskflow/status_update")
        self.assertEqual(config.TOPIC_MATH_TRIGGER, "deskflow/math_trigger")
        self.assertEqual(config.TOPIC_BATTERY_ALERT, "deskflow/battery_alert")

    def test_original_source_integrity(self):
        main_py_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src/main.py"))
        if not os.path.exists(main_py_path):
            main_py_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src/main.py"))
        with open(main_py_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check existing commands still exist in main.py
        self.assertIn('@bot.tree.command(name="alarm"', content)
        self.assertIn('@bot.tree.command(name="solve"', content)
        self.assertIn('@bot.tree.command(name="close_tkb"', content)
        self.assertIn('@bot.tree.command(name="buzz"', content)
        self.assertIn('@bot.tree.command(name="led"', content)
        self.assertIn('@bot.tree.command(name="quote"', content)

        # Check /solve command logic
        self.assertIn('alarm_engine', content)
        self.assertIn('cmd_solve', content)
        self.assertIn('send_morning_briefing', content)

        # Check PocketBase, Spotify, and scheduler are untouched
        self.assertIn('pb = PocketBase(pb_url)', content)
        self.assertIn('scheduler.setup_scheduler', content)
        self.assertIn('spotify_service.init_spotify', content)


if __name__ == "__main__":
    unittest.main()
