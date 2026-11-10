"""
Unit and Contract Integration Tests for ESP32 Focus Page & Backend Synchronization.
Verifies:
1. Backend MQTT Status Contract (deskflow/focus/status) matching ESP32 ArduinoJson expectations.
2. Representation of all authoritative phases: WORK, BREAK, EXPIRED_LOCKED, IDLE.
3. Display-Only compliance: No local business logic or local phase mutation in ESP32 firmware.
4. Firmware code verification of /home/ttd/Arduino/khkt/khkt.ino.
"""

import unittest
import json
import re
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from src.focus_mode.service import FocusService
from src.focus_mode.storage import FocusStorage

class TestESP32FocusIntegration(unittest.TestCase):
    def setUp(self):
        self.published_messages = []
        def mock_mqtt_publisher(topic, payload, retain=False, qos=0):
            self.published_messages.append({
                "topic": topic,
                "payload": payload,
                "retain": retain,
                "qos": qos
            })
        self.mock_mqtt_publisher = mock_mqtt_publisher
        self.storage = MagicMock()
        self.auth = MagicMock()
        self.challenge = MagicMock()
        self.classifier = MagicMock()
        self.service = FocusService(
            storage=self.storage,
            auth=self.auth,
            challenge=self.challenge,
            classifier=self.classifier
        )
        self.service.set_mqtt_publisher(mock_mqtt_publisher)

    def test_backend_mqtt_payload_contract(self):
        """Verifies that the backend formats deskflow/focus/status with all fields expected by ESP32."""
        now = datetime.now(timezone.utc)
        ends_at = (now + timedelta(minutes=25)).isoformat()
        
        state = {
            "version": 1,
            "enabled": True,
            "session": {
                "id": "session-1",
                "type": "pomodoro",
                "phase": "WORK",
                "total_seconds": 1500,
                "ends_at": ends_at,
                "started_at": now.isoformat(),
                "cycle_count": 2
            }
        }
        
        self.service._publish_mqtt(state)
        self.assertEqual(len(self.published_messages), 1)
        msg = self.published_messages[0]
        self.assertEqual(msg["topic"], "deskflow/focus/status")
        self.assertTrue(msg["retain"])
        self.assertEqual(msg["qos"], 1)
        
        payload = msg["payload"]
        self.assertEqual(payload["action"], "focus_status")
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["focus_active"])
        self.assertEqual(payload["phase"], "WORK")
        self.assertEqual(payload["session_type"], "pomodoro")
        self.assertGreater(payload["remaining_seconds"], 1400)
        self.assertEqual(payload["total_seconds"], 1500)
        self.assertEqual(payload["cycle_count"], 2)
        self.assertEqual(payload["ends_at"], ends_at)
        self.assertIsNotNone(payload["server_time"])

    def test_indefinite_timed_focus(self):
        """Verifies indefinite focus formats correctly with null remaining_seconds."""
        now = datetime.now(timezone.utc)
        state = {
            "version": 5,
            "enabled": True,
            "session": {
                "id": "session-indefinite",
                "type": "focus",
                "phase": "FOCUS",
                "total_seconds": None,
                "ends_at": None,
                "started_at": now.isoformat()
            }
        }
        self.service._publish_mqtt(state)
        msg = self.published_messages[-1]
        payload = msg["payload"]
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["focus_active"])
        self.assertEqual(payload["phase"], "FOCUS")
        self.assertIsNone(payload["remaining_seconds"])
        self.assertIsNone(payload["total_seconds"])

        # ArduinoJson simulation: doc["remaining_seconds"] | 0
        rem_sec = payload["remaining_seconds"] or 0
        self.assertEqual(rem_sec, 0)

    def test_break_phase_suspends_blocking(self):
        """Verifies Pomodoro BREAK sets phase='BREAK' and focus_active=False for ESP32 display."""
        now = datetime.now(timezone.utc)
        ends_at = (now + timedelta(minutes=5)).isoformat()
        state = {
            "version": 2,
            "enabled": True,
            "session": {
                "id": "session-2",
                "type": "pomodoro",
                "phase": "BREAK",
                "total_seconds": 300,
                "ends_at": ends_at,
                "started_at": now.isoformat(),
                "cycle_count": 2
            }
        }
        status = self.service._format_status_payload(state)
        self.assertTrue(status["enabled"])
        self.assertFalse(status["focus_active"])  # Suspended during break
        self.assertEqual(status["phase"], "BREAK")
        self.assertEqual(status["session_type"], "pomodoro")
        self.assertEqual(status["cycle_count"], 2)

    def test_expired_locked_phase_for_esp32(self):
        """Verifies that expired regular focus produces phase='EXPIRED_LOCKED' and focus_active=True."""
        now = datetime.now(timezone.utc)
        past = (now - timedelta(seconds=10)).isoformat()
        state = {
            "version": 3,
            "enabled": True,
            "session": {
                "id": "session-3",
                "type": "focus",
                "phase": "FOCUS",
                "total_seconds": 1800,
                "ends_at": past,
                "started_at": (now - timedelta(seconds=1810)).isoformat()
            }
        }
        status = self.service._format_status_payload(state)
        self.assertTrue(status["enabled"])
        self.assertTrue(status["focus_active"])
        self.assertEqual(status["phase"], "EXPIRED_LOCKED")
        self.assertEqual(status["remaining_seconds"], 0)

    def test_idle_phase_when_disabled(self):
        """Verifies that disabled focus produces phase='IDLE' and focus_active=False."""
        state = {
            "version": 4,
            "enabled": False,
            "session": None
        }
        status = self.service._format_status_payload(state)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["focus_active"])
        self.assertEqual(status["phase"], "IDLE")
        self.assertEqual(status["remaining_seconds"], 0)
        self.assertEqual(status["total_seconds"], 0)

    def test_firmware_source_integrity(self):
        """Audits /home/ttd/Arduino/khkt/khkt.ino to enforce display-only and view integration."""
        with open("/home/ttd/Arduino/khkt/khkt.ino", "r", encoding="utf-8") as f:
            code = f.read()

        # 1. Verification of MQTT topic subscription
        self.assertIn('mqtt.subscribe("deskflow/focus/status")', code,
                      "ESP32 must subscribe to deskflow/focus/status")

        # 2. Verification of MQTT parsing
        self.assertIn('else if (topicStr == "deskflow/focus/status")', code)
        self.assertIn('doc["enabled"]', code)
        self.assertIn('doc["focus_active"]', code)
        self.assertIn('doc["phase"]', code)
        self.assertIn('doc["remaining_seconds"]', code)
        self.assertIn('doc["total_seconds"]', code)
        self.assertIn('doc["cycle_count"]', code)
        self.assertIn('doc["session_type"]', code)

        # 3. Verification of dedicated drawFocusView
        self.assertIn('void drawFocusView()', code, "drawFocusView() must be implemented")
        self.assertIn('drawFocusView();', code, "drawFocusView() must be invoked from drawPhysicalScreen()")
        self.assertIn('srvStats.activeView == "focus"', code, "activeView == 'focus' must be handled")

        # 4. Verification of cycleNextView
        self.assertIn('srvStats.activeView = "focus";', code, "cycleNextView must include 'focus'")

        # 5. Verification of all phase displays in drawFocusView
        self.assertIn('EXPIRED_LOCKED', code)
        self.assertIn('HET GIO - DA KHOA', code)
        self.assertIn('BREAK', code)
        self.assertIn('GIO GIAI LAO', code)
        self.assertIn('DANG TAP TRUNG', code)
        self.assertIn('CHUA KICH HOAT', code)

        # 6. Strict Display-Only verification: ESP32 does not mutate backend state or make phase decisions
        self.assertNotIn('mqtt.publish("deskflow/focus/set_phase"', code)
        self.assertNotIn('mqtt.publish("deskflow/focus/unlock"', code)
        self.assertNotIn('focusPhase = "WORK";', code)
        self.assertNotIn('focusPhase = "BREAK";', code)
        self.assertNotIn('focusPhase = "EXPIRED_LOCKED";', code)

if __name__ == "__main__":
    unittest.main()
