"""
Unit and Integration Tests for DeskFlow Morse Code API Endpoints:
- POST /api/app/control/morse/play (valid payload, invalid payload, malformed JSON, auth scopes)
- POST /api/app/control/morse/stop (valid request, auth scopes)
- Verification of 405 HTTPException handling and 'invalid_request' error envelope
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock
from aiohttp.test_utils import AioHTTPTestCase
from aiohttp import web

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "_server_copy", "src")))

from focus_mode.storage import FocusStorage
from focus_mode.auth import FocusAuth
from focus_mode.challenge import MathChallengeSystem
from focus_mode.classifier import WebsiteClassifier
from focus_mode.service import FocusService
from focus_mode.api import create_app
from station_control import station_control
from alarm_engine import AlarmEngine


class TestMorseApi(AioHTTPTestCase):
    async def get_application(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_morse_test_")
        self.storage = FocusStorage(data_dir=self.test_dir)
        self.storage.initialize()
        self.auth = FocusAuth(storage=self.storage)
        self.challenge = MathChallengeSystem(storage=self.storage, auth=self.auth, groq_client=None)
        self.classifier = WebsiteClassifier(storage=self.storage, groq_client=None)
        self.service = FocusService(
            storage=self.storage,
            auth=self.auth,
            challenge=self.challenge,
            classifier=self.classifier
        )

        # Mock MQTT on alarm_engine to prevent actual broker connection in tests
        self.mock_mqtt = MagicMock()
        self.mock_mqtt.is_connected.return_value = True
        msg_info = MagicMock()
        msg_info.rc = 0
        self.mock_mqtt.publish.return_value = msg_info

        from alarm_engine import alarm_engine
        self.orig_alarm_mqtt = alarm_engine.mqtt_client
        alarm_engine.mqtt_client = self.mock_mqtt

        return create_app(self.service)

    async def asyncTearDown(self):
        from alarm_engine import alarm_engine
        alarm_engine.mqtt_client = self.orig_alarm_mqtt
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)
        await super().asyncTearDown()

    async def _create_token(self, scopes=None):
        code, _ = await self.auth.generate_pairing_code()
        resp = await self.client.post(
            "/api/focus/pair",
            json={"code": code, "device_name": "Test Android", "platform": "android"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        token = data["device_token"]
        if scopes is not None:
            dev_id = data["device_id"]
            def update_scopes(s):
                if dev_id in s["devices"]:
                    s["devices"][dev_id]["scopes"] = scopes
            await self.storage.mutate(update_scopes)
        return token

    async def test_morse_play_valid_request(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "pattern": "... --- ...",
            "unit_ms": 80,
            "freq": 2400
        }
        resp = await self.client.post("/api/app/control/morse/play", json=payload, headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("pattern"), "... --- ...")
        self.assertEqual(data.get("unit_ms"), 80)
        self.assertEqual(data.get("freq"), 2400)

        # Verify MQTT publish was triggered with action 'morse_play'
        self.assertTrue(self.mock_mqtt.publish.called)
        call_args = self.mock_mqtt.publish.call_args[0]
        self.assertEqual(call_args[0], "deskflow/alarm")

    async def test_morse_play_uses_station_defaults(self):
        """Omitting unit_ms/freq must fall back to the station defaults: 60 ms / 1000 Hz."""
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self.client.post(
            "/api/app/control/morse/play",
            json={"pattern": "... --- ..."},
            headers=headers,
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data.get("unit_ms"), 60)
        self.assertEqual(data.get("freq"), 1000)

    async def test_morse_play_unauthenticated(self):
        payload = {"pattern": "... --- ..."}
        resp = await self.client.post("/api/app/control/morse/play", json=payload)
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "token_invalid")

    async def test_morse_play_missing_control_scope(self):
        token = await self._create_token(scopes=["focus"])  # Missing 'control'
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"pattern": "... --- ..."}
        resp = await self.client.post("/api/app/control/morse/play", json=payload, headers=headers)
        self.assertEqual(resp.status, 403)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "insufficient_scope")

    async def test_morse_play_empty_pattern(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"pattern": "   "}
        resp = await self.client.post("/api/app/control/morse/play", json=payload, headers=headers)
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "invalid_morse_pattern")

    async def test_morse_play_malformed_json_body(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        # Send raw invalid JSON
        resp = await self.client.post("/api/app/control/morse/play", data="{bad_json:", headers=headers)
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        # Malformed JSON raises ValueError in _parse_json, caught by middleware as invalid_request
        self.assertEqual(data["error"]["code"], "invalid_request")

    async def test_morse_stop_valid_request(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self.client.post("/api/app/control/morse/stop", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data.get("success"))

        # Verify MQTT publish was called for stop
        self.assertTrue(self.mock_mqtt.publish.called)

    async def test_morse_stop_unauthenticated(self):
        resp = await self.client.post("/api/app/control/morse/stop")
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "token_invalid")

    async def test_unregistered_endpoint_returns_405_invalid_request(self):
        """Simulates an undeployed endpoint under /api/app/ returning 405 invalid_request."""
        resp = await self.client.post("/api/app/control/nonexistent_action")
        self.assertEqual(resp.status, 405)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
