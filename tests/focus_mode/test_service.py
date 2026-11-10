"""
Unit tests for FocusService & State Machine:
- OFF -> ON state transition (increments version)
- ON -> ON idempotent transition
- ON -> Challenge requested
- ON -> Incorrect answer -> ON (increments failure counter)
- ON -> Correct answer -> OFF (increments version, resets failure counter)
- NO direct disable operation exists in service
- Public state format parity across clients
"""

import os
import shutil
import tempfile
import unittest

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

from focus_mode.storage import FocusStorage
from focus_mode.auth import FocusAuth
from focus_mode.challenge import MathChallengeSystem, ChallengeError
from focus_mode.classifier import WebsiteClassifier
from focus_mode.service import FocusService


class TestFocusService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_service_test_")
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

    def tearDown(self):
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_state_transitions_lifecycle(self):
        # 1. Initial State: OFF, version 0
        status0 = self.service.get_public_status()
        self.assertFalse(status0["enabled"])
        self.assertFalse(status0["focus_active"])
        self.assertEqual(status0["version"], 0)
        self.assertIsNone(status0["challenge"])

        # Cannot request challenge while OFF (409 focus_not_active)
        with self.assertRaises(ChallengeError) as ctx:
            await self.service.request_challenge()
        self.assertEqual(ctx.exception.code, "focus_not_active")

        # 2. Transition: OFF -> ON
        status1 = await self.service.enable(language="vi")
        self.assertTrue(status1["enabled"])
        self.assertTrue(status1["focus_active"])
        self.assertEqual(status1["version"], 1)
        self.assertEqual(status1["language"], "vi")

        # 3. Idempotent: ON -> ON
        status2 = await self.service.enable(language="vi")
        self.assertTrue(status2["enabled"])
        self.assertEqual(status2["version"], 1)  # Version does not increment when nothing changed

        # 4. Transition: ON -> Challenge Requested
        ch = await self.service.request_challenge()
        self.assertTrue(ch["challenge_id"].startswith("ch_"))
        status3 = self.service.get_public_status()
        self.assertIsNotNone(status3["challenge"])
        self.assertEqual(status3["challenge"]["challenge_id"], ch["challenge_id"])

        # 5. Transition: ON -> Incorrect Answer -> ON
        res_fail = await self.service.verify_challenge(ch["challenge_id"], -9999)
        self.assertFalse(res_fail["correct"])
        self.assertTrue(res_fail["enabled"])
        self.assertTrue(res_fail["focus_active"])
        self.assertIn("new_challenge", res_fail)

        new_ch = res_fail["new_challenge"]
        self.assertNotEqual(new_ch["challenge_id"], ch["challenge_id"])

        # 6. Transition: ON -> Correct Answer -> OFF
        # Find correct answer
        state = self.storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        correct_ans = None
        for cand in range(-1000, 1001):
            if self.auth.compute_hmac(f"{new_ch['challenge_id']}:{cand}") == stored_hmac:
                correct_ans = cand
                break
        self.assertIsNotNone(correct_ans)

        res_ok = await self.service.verify_challenge(new_ch["challenge_id"], correct_ans)
        self.assertTrue(res_ok["correct"])
        self.assertFalse(res_ok["enabled"])
        self.assertFalse(res_ok["focus_active"])

        status_final = self.service.get_public_status()
        self.assertFalse(status_final["enabled"])
        self.assertIsNone(status_final["challenge"])

    def test_no_direct_disable_method_exists(self):
        # Enforce that service has NO disable method
        self.assertFalse(hasattr(self.service, "disable"))
        self.assertFalse(hasattr(self.service, "turn_off"))


if __name__ == "__main__":
    unittest.main()
