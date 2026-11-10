import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock
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


class TestAppRoutesCentralControl(AioHTTPTestCase):
    async def get_application(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_central_control_test_")
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

        station_control._data_dir = self.test_dir
        station_control._settings_file = os.path.join(self.test_dir, "station_settings.json")
        station_control.device_settings["hardware_config_version"] = 1
        station_control.device_settings["applied_by_station"] = False
        station_control.notes = []
        station_control.countdowns = []
        station_control.active_note_id = None
        station_control.active_countdown_id = None

        self.orig_mqtt = station_control._mqtt_client
        self.mock_mqtt = MagicMock()
        station_control.set_mqtt_client(self.mock_mqtt)

        app = create_app(self.service)
        return app

    async def asyncTearDown(self):
        station_control._mqtt_client = self.orig_mqtt
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

    async def test_display_view_all_9_views_routes(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}

        views = ["main", "stats", "tkb", "weather", "lyrics", "clock", "notes", "countdown", "focus"]
        for view_name in views:
            resp = await self.client.post(
                "/api/app/control/display/view",
                json={"view": view_name},
                headers=headers
            )
            self.assertEqual(resp.status, 200, f"View {view_name} failed with status {resp.status}")
            data = await resp.json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("view"), view_name)

        # Invalid view should return 400
        bad_resp = await self.client.post(
            "/api/app/control/display/view",
            json={"view": "invalid_unknown_view"},
            headers=headers
        )
        self.assertEqual(bad_resp.status, 400)

    async def test_scheduled_darkmode_route(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}

        resp = await self.client.post(
            "/api/app/control/settings",
            json={
                "darkmode_mode": "scheduled",
                "darkmode_start": "22:00",
                "darkmode_end": "06:00"
            },
            headers=headers
        )
        self.assertEqual(resp.status, 200)
        res_body = await resp.json()
        self.assertTrue(res_body.get("success"))
        settings = res_body.get("data", {})
        self.assertEqual(settings.get("darkmode_mode"), "scheduled")
        self.assertEqual(settings.get("darkmode_start"), "22:00")
        self.assertEqual(settings.get("darkmode_end"), "06:00")

    async def test_notes_crud_and_dual_schema(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}

        # 1. GET notes (initially empty)
        resp = await self.client.get("/api/app/control/notes", headers=headers)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("notes"), [])
        self.assertEqual(body.get("data"), [])

        # 2. POST note with Android 'text' key
        resp = await self.client.post(
            "/api/app/control/notes",
            json={"text": "Review Math Chapter 5", "color": "#FDE047"},
            headers=headers
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body.get("success"))
        self.assertIn("notes", body)
        self.assertIn("data", body)
        self.assertEqual(len(body["notes"]), 1)
        created = body["notes"][0]
        self.assertEqual(created["text"], "Review Math Chapter 5")
        self.assertEqual(created["content"], "Review Math Chapter 5")
        note_id = created["id"]

        # 3. Pin note
        pin_resp = await self.client.post(
            f"/api/app/control/notes/{note_id}/pin",
            headers=headers
        )
        self.assertEqual(pin_resp.status, 200)
        pin_body = await pin_resp.json()
        self.assertEqual(pin_body.get("active_station_note_id"), note_id)

        # 4. Delete note
        del_resp = await self.client.delete(
            f"/api/app/control/notes/{note_id}",
            headers=headers
        )
        self.assertEqual(del_resp.status, 200)
        del_body = await del_resp.json()
        self.assertTrue(del_body.get("success"))
        self.assertIn("notes", del_body)
        self.assertEqual(len(del_body["notes"]), 0)

    async def test_countdowns_crud_and_dual_schema(self):
        token = await self._create_token(scopes=["focus", "control"])
        headers = {"Authorization": f"Bearer {token}"}

        # 1. GET countdowns
        resp = await self.client.get("/api/app/control/countdowns", headers=headers)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("countdowns"), [])
        self.assertEqual(body.get("data"), [])

        # 2. POST countdown with Android 'name' & 'target_date' keys
        resp = await self.client.post(
            "/api/app/control/countdowns",
            json={"name": "KHKT Competition", "target_date": "2026-12-15"},
            headers=headers
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body.get("success"))
        self.assertIn("countdowns", body)
        self.assertIn("data", body)
        self.assertEqual(len(body["countdowns"]), 1)
        created = body["countdowns"][0]
        self.assertEqual(created["name"], "KHKT Competition")
        self.assertEqual(created["title"], "KHKT Competition")
        cid = created["id"]

        # 3. Pin countdown
        pin_resp = await self.client.post(
            f"/api/app/control/countdowns/{cid}/pin",
            headers=headers
        )
        self.assertEqual(pin_resp.status, 200)
        pin_body = await pin_resp.json()
        self.assertEqual(pin_body.get("active_station_countdown_id"), cid)

        # 4. Delete countdown
        del_resp = await self.client.delete(
            f"/api/app/control/countdowns/{cid}",
            headers=headers
        )
        self.assertEqual(del_resp.status, 200)
        del_body = await del_resp.json()
        self.assertTrue(del_body.get("success"))
        self.assertIn("countdowns", del_body)
        self.assertEqual(len(del_body["countdowns"]), 0)


if __name__ == "__main__":
    unittest.main()
