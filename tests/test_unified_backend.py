"""
Test Suite for DeskFlow Unified Backend Components using standard unittest:
- AlarmEngine state machine & concurrency serialization
- FocusAuth scope verification
- aiohttp App routes authorization & input validation
"""

import sys
import os
import unittest
from unittest.mock import MagicMock

# Add src to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_server_copy", "src")))

from alarm_engine import AlarmEngine, AlarmPublishError
from station_control import station_control, StationControlManager
from focus_mode.auth import AuthError
from focus_mode.storage import FocusStorage
from focus_mode.service import FocusService
from focus_mode.challenge import MathChallengeSystem
from focus_mode.classifier import WebsiteClassifier
from focus_mode.auth import FocusAuth
from focus_mode.app_routes import require_scope


class TestAlarmEngine(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_and_solve_success(self):
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        msg_info = MagicMock()
        msg_info.rc = 0
        mock_mqtt.publish.return_value = msg_info

        engine = AlarmEngine(mqtt_client=mock_mqtt)

        # 1. Trigger
        res = await engine.trigger_alarm("12 + 15", 27)
        self.assertTrue(res["success"])
        sid = res["session_id"]
        self.assertEqual(engine._alarm_epoch, 1)
        self.assertEqual(mock_mqtt.publish.call_count, 1)

        active = await engine.get_active_session_public()
        self.assertTrue(active["active"])
        self.assertEqual(active["question"], "12 + 15")
        self.assertEqual(active["session_id"], sid)

        # 2. Solve with correct answer
        solve_res = await engine.solve_alarm(sid, 27)
        self.assertTrue(solve_res["success"])
        self.assertEqual(engine._alarm_epoch, 2)
        self.assertEqual(mock_mqtt.publish.call_count, 2)

        active_after = await engine.get_active_session_public()
        self.assertFalse(active_after["active"])

    async def test_incorrect_attempts_and_cooldown(self):
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = True
        msg_info = MagicMock()
        msg_info.rc = 0
        mock_mqtt.publish.return_value = msg_info

        engine = AlarmEngine(mqtt_client=mock_mqtt)
        res = await engine.trigger_alarm("10 + 10", 20)
        sid = res["session_id"]

        # 4 incorrect attempts
        for _ in range(4):
            with self.assertRaises(ValueError):
                await engine.solve_alarm(sid, 99)

        # 5th incorrect attempt -> triggers cooldown
        with self.assertRaises(TimeoutError):
            await engine.solve_alarm(sid, 99)

        # During cooldown, any attempt fails with cooldown_active
        with self.assertRaises(TimeoutError):
            await engine.solve_alarm(sid, 20)

    async def test_mqtt_failure_rollback(self):
        mock_mqtt = MagicMock()
        mock_mqtt.is_connected.return_value = False  # Disconnected!

        engine = AlarmEngine(mqtt_client=mock_mqtt)
        with self.assertRaises(AlarmPublishError):
            await engine.trigger_alarm("5 + 5", 10)

        # State should be rolled back
        self.assertEqual(engine._alarm_epoch, 0)
        active = await engine.get_active_session_public()
        self.assertFalse(active["active"])


class TestScopeAuthorization(unittest.TestCase):
    def test_require_scope(self):
        dev_focus = {"device_id": "dev1", "scopes": ["focus"]}
        dev_full = {"device_id": "dev2", "scopes": ["focus", "control", "spotify"]}

        # device with focus only cannot access control
        with self.assertRaises(AuthError) as ctx:
            require_scope(dev_focus, "control")
        self.assertEqual(ctx.exception.status, 403)

        # device with full access can access control and spotify without raising
        require_scope(dev_full, "control")
        require_scope(dev_full, "spotify")


class TestStationControl(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_mqtt = MagicMock()
        msg_info = MagicMock()
        msg_info.rc = 0
        self.mock_mqtt.publish.return_value = msg_info
        self.mgr = StationControlManager()
        self.mgr.set_mqtt_client(self.mock_mqtt)

    def test_switch_view(self):
        res = self.mgr.switch_view("weather")
        self.assertTrue(res["success"])
        self.assertEqual(res["view"], "weather")
        self.assertEqual(self.mgr.current_screen_mode, "weather")
        self.assertEqual(self.mock_mqtt.publish.call_count, 1)

        with self.assertRaises(ValueError):
            self.mgr.switch_view("invalid_screen")

    def test_lamp_controls(self):
        # Color - standard
        res_color = self.mgr.set_lamp_color("#FF5500")
        self.assertTrue(res_color["success"])
        self.assertEqual(res_color["hex"], "#FF5500")

        # Color - shorthand #F00 -> #FF0000
        res_short = self.mgr.set_lamp_color("#F00")
        self.assertEqual(res_short["hex"], "#FF0000")

        # Color - 8-character ARGB #FFFF5500
        res_argb = self.mgr.set_lamp_color("#FFFF5500")
        self.assertEqual(res_argb["hex"], "#FF5500")

        # Color - named color 'cyan'
        res_name = self.mgr.set_lamp_color("cyan")
        self.assertEqual(res_name["hex"], "#00FFFF")

        # Color - without #
        res_no_hash = self.mgr.set_lamp_color("10b981")
        self.assertEqual(res_no_hash["hex"], "#10B981")

        # Invalid color
        with self.assertRaises(ValueError):
            self.mgr.set_lamp_color("not_a_color")

        # Dim
        res_dim = self.mgr.set_lamp_dim(led=50, screen=80)
        self.assertTrue(res_dim["success"])
        self.assertEqual(res_dim["led_bright"], 50)
        self.assertEqual(res_dim["screen_bright"], 80)

        # Off
        res_off = self.mgr.set_lamp_off()
        self.assertTrue(res_off["success"])

        # LED action
        res_led = self.mgr.set_led("blink")
        self.assertTrue(res_led["success"])

    def test_sticky_and_countdown(self):
        res_note = self.mgr.set_sticky_note("Lam bai tap Toan")
        self.assertTrue(res_note["success"])

        res_exam = self.mgr.set_exam_countdown("Thi Hoc Ky", "25/12")
        self.assertTrue(res_exam["success"])
        self.assertEqual(res_exam["name"], "Thi Hoc Ky")
        self.assertGreaterEqual(res_exam["days_left"], 0)

    def test_settings_mutation(self):
        updates = {
            "darkmode_mode": "custom",
            "darkmode_start": "23:00",
            "darkmode_end": "05:00",
            "weather_alert_enabled": False,
            "battery_low_thresh": 25,
            "battery_high_thresh": 85,
            "clock_style": "flip"
        }
        res = self.mgr.update_settings(updates)
        self.assertEqual(res["darkmode_mode"], "custom")
        self.assertEqual(res["darkmode_start"], "23:00")
        self.assertEqual(res["weather_alert_enabled"], False)
        self.assertEqual(res["battery_low_thresh"], 25)
        self.assertEqual(res["battery_high_thresh"], 85)
        self.assertEqual(res["clock_style"], "flip")


class TestFocusHistory(unittest.IsolatedAsyncioTestCase):
    async def test_session_lifecycle_and_history(self):
        import tempfile
        import shutil
        tmpdir = tempfile.mkdtemp()
        try:
            storage = FocusStorage(data_dir=tmpdir)
            storage.initialize()
            auth = FocusAuth(storage=storage)
            challenge = MathChallengeSystem(storage=storage, auth=auth, groq_client=None)
            classifier = WebsiteClassifier(storage=storage, groq_client=None)
            service = FocusService(storage=storage, auth=auth, challenge=challenge, classifier=classifier)

            # Initially empty history
            hist = service.get_history()
            self.assertEqual(hist["history"], [])
            self.assertIsNone(hist["current_session"])

            # Enable focus mode -> creates current_session
            await service.enable(language="vi")
            hist = service.get_history()
            self.assertIsNotNone(hist["current_session"])
            self.assertEqual(hist["current_session"]["status"], "active")

            # Request challenge and solve
            ch = await service.request_challenge(language="vi", force_new=True)
            cid = ch["challenge_id"]

            # Compute answer directly from storage for test
            state = storage.get_state()
            active_ch = state["challenge"]
            correct_answer = 0
            for test_ans in range(-1000, 1001):
                payload = f"{cid}:{test_ans}"
                if auth.compute_hmac(payload) == active_ch["answer_hmac"]:
                    correct_answer = test_ans
                    break

            solve_res = await service.verify_challenge(challenge_id=cid, answer=correct_answer)
            self.assertTrue(solve_res["correct"])

            # History now contains completed session
            hist_after = service.get_history()
            self.assertIsNone(hist_after["current_session"])
            self.assertEqual(len(hist_after["history"]), 1)
            self.assertEqual(hist_after["history"][0]["status"], "completed")
            self.assertEqual(hist_after["history"][0]["reason"], "Challenge solved successfully")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
