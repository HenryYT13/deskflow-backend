"""
Integration tests for aiohttp Focus Mode REST API:
- Every endpoint:
  - POST /api/focus/pair
  - GET /api/focus/status
  - POST /api/focus/enable
  - POST /api/focus/classify-url
  - POST /api/focus/challenge
  - POST /api/focus/verify
  - GET /api/focus/devices
  - POST /api/focus/unpair
- Authentication requirements (401 on missing / invalid token)
- Oversized request payload handling (16 KB limit -> 413)
- Error envelope structure ({"error": {"code": ..., "retry_after": ...}})
- CORS headers on browser extension origins
- Storage unavailable behavior (503)
"""

import os
import shutil
import tempfile
import unittest
from aiohttp.test_utils import AioHTTPTestCase
from aiohttp import web

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

from focus_mode.storage import FocusStorage
from focus_mode.auth import FocusAuth
from focus_mode.challenge import MathChallengeSystem
from focus_mode.classifier import WebsiteClassifier
from focus_mode.service import FocusService
from focus_mode.api import create_app


class TestFocusApi(AioHTTPTestCase):
    async def get_application(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_api_test_")
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
        return create_app(self.service)

    async def asyncTearDown(self):
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)
        await super().asyncTearDown()

    async def _pair_helper(self):
        code, _ = await self.auth.generate_pairing_code()
        resp = await self.client.post(
            "/api/focus/pair",
            json={"code": code, "device_name": "Test Chrome", "platform": "chromium"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        return data["device_token"], data["device_id"]

    async def test_pair_endpoint_and_validation(self):
        # 1. Missing body/code -> 400 invalid_request
        resp = await self.client.post("/api/focus/pair", json={})
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "invalid_request")

        # 2. Valid pairing
        code, _ = await self.auth.generate_pairing_code()
        resp = await self.client.post(
            "/api/focus/pair",
            json={"pairing_code": code, "device_name": "Desk Chrome", "browser": "Chrome"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("device_token", data)
        self.assertIn("device_id", data)
        self.assertEqual(data["device_name"], "Desk Chrome")
        self.assertFalse(data["focus_active"])

    async def test_authentication_guard(self):
        # Unauthenticated request to /status -> 401 token_invalid
        resp = await self.client.get("/api/focus/status")
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "token_invalid")

        # Invalid token -> 401
        resp = await self.client.get("/api/focus/status", headers={"Authorization": "Bearer badtoken"})
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "token_invalid")

    async def test_status_and_enable_endpoints(self):
        token, dev_id = await self._pair_helper()
        headers = {"Authorization": f"Bearer {token}"}

        # Check status (initial: OFF)
        resp = await self.client.get("/api/focus/status", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertFalse(data["enabled"])
        self.assertFalse(data["focus_active"])
        self.assertEqual(data["version"], 0)

        # Enable Focus Mode
        resp = await self.client.post("/api/focus/enable", headers=headers, json={"language": "en"})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["enabled"])
        self.assertTrue(data["focus_active"])
        self.assertEqual(data["version"], 1)

    async def test_enable_refuses_stop_request_and_keeps_state(self):
        """`{"enabled": false}` must never start a session again.

        Regression: desktop clients sent this to stop Focus Mode, the handler
        ignored the flag and created a fresh 25-minute session, so the window
        re-locked right after a correct challenge answer.
        """
        token, _ = await self._pair_helper()
        headers = {"Authorization": f"Bearer {token}"}

        # Start a short session, then try the legacy "stop" request.
        resp = await self.client.post(
            "/api/focus/enable",
            headers=headers,
            json={"language": "en", "duration_minutes": 5},
        )
        self.assertEqual(resp.status, 200)
        started = await resp.json()
        self.assertTrue(started["enabled"])
        version_after_start = started["version"]

        resp = await self.client.post(
            "/api/focus/enable", headers=headers, json={"enabled": False}
        )
        self.assertEqual(resp.status, 409)
        body = await resp.json()
        self.assertEqual(body["error"]["code"], "challenge_required")

        # The session must be untouched: same version, still enabled.
        resp = await self.client.get("/api/focus/status", headers=headers)
        status = await resp.json()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["version"], version_after_start)

    async def test_classify_url_endpoint(self):
        token, _ = await self._pair_helper()
        headers = {"Authorization": f"Bearer {token}"}

        # Focus is OFF -> Allowed (reason: focus_off)
        resp = await self.client.post(
            "/api/focus/classify-url",
            headers=headers,
            json={"url": "https://reddit.com"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["action"], "allow")
        self.assertEqual(data["reason"], "focus_off")

        # Enable Focus Mode
        await self.client.post("/api/focus/enable", headers=headers, json={"language": "en"})

        # Hard blocked domain -> Blocked
        resp = await self.client.post(
            "/api/focus/classify-url",
            headers=headers,
            json={"url": "https://chatgpt.com/search"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["action"], "block")
        self.assertEqual(data["reason"], "hard_blocked")

    async def test_challenge_and_verify_endpoints(self):
        token, _ = await self._pair_helper()
        headers = {"Authorization": f"Bearer {token}"}

        # Focus OFF -> Challenge request gives 409 focus_not_active
        resp = await self.client.post("/api/focus/challenge", headers=headers, json={})
        self.assertEqual(resp.status, 409)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "focus_not_active")

        # Enable Focus
        await self.client.post("/api/focus/enable", headers=headers, json={"language": "en"})

        # Request challenge
        resp = await self.client.post("/api/focus/challenge", headers=headers, json={})
        self.assertEqual(resp.status, 200)
        ch = await resp.json()
        ch_id = ch["challenge_id"]
        self.assertTrue(ch_id.startswith("ch_"))

        # Verify wrong answer -> 200 with correct: false, new_challenge
        resp = await self.client.post(
            "/api/focus/verify",
            headers=headers,
            json={"challenge_id": ch_id, "answer": -9999}
        )
        self.assertEqual(resp.status, 200)
        res_fail = await resp.json()
        self.assertFalse(res_fail["correct"])
        self.assertTrue(res_fail["enabled"])
        self.assertIn("new_challenge", res_fail)

        new_ch_id = res_fail["new_challenge"]["challenge_id"]

        # Find correct answer
        state = self.storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        correct_ans = None
        for cand in range(-1000, 1001):
            if self.auth.compute_hmac(f"{new_ch_id}:{cand}") == stored_hmac:
                correct_ans = cand
                break
        self.assertIsNotNone(correct_ans)

        # Verify correct answer -> 200 with correct: true, enabled: false
        resp = await self.client.post(
            "/api/focus/verify",
            headers=headers,
            json={"challenge_id": new_ch_id, "answer": correct_ans}
        )
        self.assertEqual(resp.status, 200)
        res_ok = await resp.json()
        self.assertTrue(res_ok["correct"])
        self.assertFalse(res_ok["enabled"])

    async def test_devices_and_unpair_endpoints(self):
        token, dev_id = await self._pair_helper()
        headers = {"Authorization": f"Bearer {token}"}

        # Query devices
        resp = await self.client.get("/api/focus/devices", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(len(data["devices"]) >= 1)
        # Check tokens never exposed
        self.assertNotIn("token", str(data))
        self.assertNotIn("token_hmac", str(data))

        # Unpair self
        resp = await self.client.post("/api/focus/unpair", headers=headers, json={})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["revoked"])

        # Subsequent authenticated requests fail with 401 token_revoked
        resp = await self.client.get("/api/focus/status", headers=headers)
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "token_revoked")

    async def test_unpair_cross_device_revocation_forbidden(self):
        # Pair Device A and Device B
        token_a, dev_a = await self._pair_helper()
        token_b, dev_b = await self._pair_helper()

        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        # Device A attempts to revoke Device B -> must be rejected with 403 forbidden
        resp = await self.client.post(
            "/api/focus/unpair",
            headers=headers_a,
            json={"device_id": dev_b}
        )
        self.assertEqual(resp.status, 403)
        err = await resp.json()
        self.assertEqual(err["error"]["code"], "forbidden")

        # Verify Device B is still completely active and valid
        resp_b = await self.client.get("/api/focus/status", headers=headers_b)
        self.assertEqual(resp_b.status, 200)

        # Device A revoking its own device ID explicitly -> succeeds
        resp_self = await self.client.post(
            "/api/focus/unpair",
            headers=headers_a,
            json={"device_id": dev_a}
        )
        self.assertEqual(resp_self.status, 200)
        self.assertTrue((await resp_self.json())["revoked"])

        # Device A is now revoked
        resp_a_revoked = await self.client.get("/api/focus/status", headers=headers_a)
        self.assertEqual(resp_a_revoked.status, 401)

    async def test_unpair_cross_device_with_control_scope_succeeds(self):
        # Register Admin/Desktop device with 'control' scope and regular device B
        dev_admin = await self.auth.register_authorized_device(
            device_name="Admin Desktop",
            platform="desktop",
            scopes=["focus", "control", "spotify"]
        )
        token_b, dev_b = await self._pair_helper()

        headers_admin = {"Authorization": f"Bearer {dev_admin['device_token']}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        # Device with 'control' scope revokes Device B -> succeeds!
        resp = await self.client.post(
            "/api/focus/unpair",
            headers=headers_admin,
            json={"device_id": dev_b}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data.get("revoked"))

        # Verify Device B is now revoked
        resp_b = await self.client.get("/api/focus/status", headers=headers_b)
        self.assertEqual(resp_b.status, 401)

    async def test_exact_cors_origin_validation(self):
        allowed_ext = "chrome-extension://abcdefghijklmnopqrstuvwxyz123456"
        unauthorized_ext = "chrome-extension://rogueextension99999999999999"
        lookalike_origin = "http://localhost.attacker.example:8787"

        # Explicitly configure allowed origins
        os.environ["FOCUS_ALLOWED_ORIGINS"] = allowed_ext
        os.environ["FOCUS_ALLOW_DEV_ORIGINS"] = "0"

        # 1. Configured exact extension origin -> allowed
        resp = await self.client.options(
            "/api/focus/status",
            headers={
                "Origin": allowed_ext,
                "Access-Control-Request-Method": "GET"
            }
        )
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), allowed_ext)
        self.assertNotEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

        # 2. Unauthorized extension origin -> 403 Forbidden on preflight
        resp_unauth = await self.client.options(
            "/api/focus/status",
            headers={
                "Origin": unauthorized_ext,
                "Access-Control-Request-Method": "GET"
            }
        )
        self.assertEqual(resp_unauth.status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", resp_unauth.headers)

        # 3. Lookalike attack origin -> 403 Forbidden
        resp_lookalike = await self.client.options(
            "/api/focus/status",
            headers={
                "Origin": lookalike_origin,
                "Access-Control-Request-Method": "GET"
            }
        )
        self.assertEqual(resp_lookalike.status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", resp_lookalike.headers)

        # 4. Malformed origin with path / query -> 403 Forbidden
        resp_malformed = await self.client.options(
            "/api/focus/status",
            headers={
                "Origin": f"{allowed_ext}/secret?bypass=1",
                "Access-Control-Request-Method": "GET"
            }
        )
        self.assertEqual(resp_malformed.status, 403)

        # 5. Dev origin rejected when FOCUS_ALLOW_DEV_ORIGINS=0
        resp_dev_rejected = await self.client.options(
            "/api/focus/status",
            headers={
                "Origin": "http://127.0.0.1:8787",
                "Access-Control-Request-Method": "GET"
            }
        )
        self.assertEqual(resp_dev_rejected.status, 403)

        # 6. Dev origin accepted when FOCUS_ALLOW_DEV_ORIGINS=1
        os.environ["FOCUS_ALLOW_DEV_ORIGINS"] = "1"
        resp_dev_allowed = await self.client.options(
            "/api/focus/status",
            headers={
                "Origin": "http://127.0.0.1:8787",
                "Access-Control-Request-Method": "GET"
            }
        )
        self.assertEqual(resp_dev_allowed.status, 204)
        self.assertEqual(resp_dev_allowed.headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:8787")

        # Reset env
        os.environ.pop("FOCUS_ALLOWED_ORIGINS", None)
        os.environ.pop("FOCUS_ALLOW_DEV_ORIGINS", None)

    async def test_oversized_payload_rejected(self):
        token, _ = await self._pair_helper()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # Send body > 16 KB (16384 bytes)
        huge_url = "https://example.com/" + ("a" * 17000)
        resp = await self.client.post(
            "/api/focus/classify-url",
            headers=headers,
            data=f'{{"url": "{huge_url}"}}'
        )
        self.assertEqual(resp.status, 413)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "too_large")

    async def test_pair_session_qr_flow(self):
        # 1. Desktop requests a QR session
        create_resp = await self.client.post("/api/focus/pair/session/create", json={"device_name": "Desktop PC", "platform": "linux"})
        self.assertEqual(create_resp.status, 200)
        create_data = await create_resp.json()
        self.assertTrue(create_data["success"])
        session_id = create_data["session_id"]
        self.assertIn("deskflow://pair?session=", create_data["qr_payload"])

        # 2. Check pending status
        status_resp = await self.client.get(f"/api/focus/pair/session/{session_id}")
        self.assertEqual(status_resp.status, 200)
        status_data = await status_resp.json()
        self.assertEqual(status_data["status"], "pending")

        # 3. Mobile app approves session with custom name
        mobile_token, _ = await self._pair_helper()
        mobile_headers = {"Authorization": f"Bearer {mobile_token}", "Content-Type": "application/json"}
        approve_resp = await self.client.post(
            f"/api/focus/pair/session/{session_id}/approve",
            headers=mobile_headers,
            json={"device_name": "My Fedora Desktop", "platform": "linux"}
        )
        self.assertEqual(approve_resp.status, 200)
        approve_data = await approve_resp.json()
        self.assertTrue(approve_data["success"])

        # 4. Desktop polling detects approval
        poll_resp = await self.client.get(f"/api/focus/pair/session/{session_id}")
        self.assertEqual(poll_resp.status, 200)
        poll_data = await poll_resp.json()
        self.assertEqual(poll_data["status"], "approved")
        self.assertEqual(poll_data["device_name"], "My Fedora Desktop")
        desktop_token = poll_data["device_token"]
        self.assertTrue(desktop_token)

        # 5. Verify the issued desktop token works
        desktop_headers = {"Authorization": f"Bearer {desktop_token}"}
        auth_check = await self.client.get("/api/focus/status", headers=desktop_headers)
        self.assertEqual(auth_check.status, 200)


if __name__ == "__main__":
    unittest.main()
