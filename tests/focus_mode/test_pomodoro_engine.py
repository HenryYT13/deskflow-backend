"""
Comprehensive Unit Tests for Focus & Pomodoro Engine Lifecycle
Covers:
- Timed Focus expiration -> EXPIRED_LOCKED (Decision 1 - Option B)
- Pomodoro WORK -> BREAK transition (Decision 2 - Option A: enabled=True, focus_active=False)
- Pomodoro BREAK -> WORK transition (cycle_count++)
- Math challenge early cancellation during WORK, BREAK, EXPIRED_LOCKED (Decision 3 - Option B)
- Downtime reconciliation across restarts and multi-cycle bounds
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from focus_mode.storage import FocusStorage
from focus_mode.auth import FocusAuth
from focus_mode.challenge import MathChallengeSystem
from focus_mode.classifier import WebsiteClassifier
from focus_mode.service import FocusService


class TestPomodoroEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_pomodoro_test_")
        self.storage = FocusStorage(data_dir=self.test_dir)
        self.storage.initialize()
        self.auth = FocusAuth(storage=self.storage)
        self.challenge = MathChallengeSystem(storage=self.storage, auth=self.auth, groq_client=None)
        self.classifier = WebsiteClassifier(storage=self.storage, groq_client=None)
        self.mock_mqtt = MagicMock()
        self.service = FocusService(
            storage=self.storage,
            auth=self.auth,
            challenge=self.challenge,
            classifier=self.classifier
        )
        self.service.set_mqtt_publisher(self.mock_mqtt)

    def tearDown(self):
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _solve_challenge(self, ch_id: str) -> int:
        state = self.storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        for cand in range(-2000, 2001):
            if self.auth.compute_hmac(f"{ch_id}:{cand}") == stored_hmac:
                return cand
        raise ValueError(f"Could not solve challenge {ch_id}")

    async def test_timed_focus_expiration_to_expired_locked(self):
        """Decision 1 (Option B): Timed focus expiration transitions to EXPIRED_LOCKED."""
        # Start timed focus (25 minutes)
        res = await self.service.start_session(session_type="focus", duration_minutes=25)
        self.assertTrue(res["enabled"])
        self.assertTrue(res["focus_active"])
        self.assertEqual(res["phase"], "FOCUS")

        # Simulate time passing past deadline
        past_deadline = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        def _set_past(st):
            st["session"]["ends_at"] = past_deadline
        await self.storage.mutate(_set_past)

        # Advance deadline
        updated = await self.service.advance_deadline()
        self.assertTrue(updated["enabled"], "Focus Mode MUST remain enabled in EXPIRED_LOCKED")
        self.assertTrue(updated["focus_active"], "focus_active MUST be True (blocking websites)")
        self.assertEqual(updated["phase"], "EXPIRED_LOCKED")
        self.assertEqual(updated["remaining_seconds"], 0)

        # Confirm MQTT publish
        self.assertTrue(self.mock_mqtt.called)
        last_payload = self.mock_mqtt.call_args[0][1]
        self.assertEqual(last_payload["phase"], "EXPIRED_LOCKED")

    async def test_pomodoro_work_to_break_and_break_to_work(self):
        """Decision 2 (Option A): Pomodoro WORK transitions to BREAK, then BREAK transitions to WORK."""
        # Start Pomodoro (25m work, 5m break)
        res = await self.service.start_session(session_type="pomodoro", duration_minutes=25, break_minutes=5)
        self.assertTrue(res["enabled"])
        self.assertTrue(res["focus_active"])
        self.assertEqual(res["phase"], "WORK")
        self.assertEqual(res["cycle_count"], 1)

        # Simulate work deadline passing
        past_work = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await self.storage.mutate(lambda st: st["session"].__setitem__("ends_at", past_work))

        # Advance deadline -> WORK to BREAK
        break_state = await self.service.advance_deadline()
        self.assertTrue(break_state["enabled"], "Session must remain enabled during BREAK")
        self.assertFalse(break_state["focus_active"], "focus_active must be False during BREAK (allow browsing)")
        self.assertEqual(break_state["phase"], "BREAK")
        self.assertGreater(break_state["remaining_seconds"], 0)

        # Simulate break deadline passing
        past_break = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await self.storage.mutate(lambda st: st["session"].__setitem__("ends_at", past_break))

        # Advance deadline -> BREAK to WORK (cycle 2)
        work2_state = await self.service.advance_deadline()
        self.assertTrue(work2_state["enabled"])
        self.assertTrue(work2_state["focus_active"])
        self.assertEqual(work2_state["phase"], "WORK")
        self.assertEqual(work2_state["cycle_count"], 2)

    async def test_math_challenge_required_in_all_phases(self):
        """Decision 3 (Option B): Early cancellation strictly requires math challenge during WORK, BREAK, and EXPIRED_LOCKED."""
        # 1. During WORK
        await self.service.start_session(session_type="pomodoro", duration_minutes=25, break_minutes=5)
        ch_work = await self.service.request_challenge()
        self.assertTrue(ch_work["challenge_id"].startswith("ch_"))

        # Verify correct answer ends session
        correct_ans = self._solve_challenge(ch_work["challenge_id"])
        v_res = await self.service.verify_challenge(ch_work["challenge_id"], correct_ans)
        self.assertTrue(v_res["correct"])
        self.assertFalse(self.service.get_public_status()["enabled"])

        # 2. During BREAK
        await self.service.start_session(session_type="pomodoro", duration_minutes=25, break_minutes=5)
        # Advance to break
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await self.storage.mutate(lambda s: s["session"].__setitem__("ends_at", past))
        await self.service.advance_deadline()
        self.assertEqual(self.service.get_public_status()["phase"], "BREAK")

        # Request challenge during BREAK
        ch_break = await self.service.request_challenge()
        correct_ans = self._solve_challenge(ch_break["challenge_id"])
        v_res = await self.service.verify_challenge(ch_break["challenge_id"], correct_ans)
        self.assertTrue(v_res["correct"])
        self.assertFalse(self.service.get_public_status()["enabled"])

        # 3. During EXPIRED_LOCKED
        await self.service.start_session(session_type="focus", duration_minutes=25)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await self.storage.mutate(lambda s: s["session"].__setitem__("ends_at", past))
        await self.service.advance_deadline()
        self.assertEqual(self.service.get_public_status()["phase"], "EXPIRED_LOCKED")

        # Request challenge during EXPIRED_LOCKED
        ch_locked = await self.service.request_challenge()
        correct_ans = self._solve_challenge(ch_locked["challenge_id"])
        v_res = await self.service.verify_challenge(ch_locked["challenge_id"], correct_ans)
        self.assertTrue(v_res["correct"])
        self.assertFalse(self.service.get_public_status()["enabled"])

    async def test_downtime_reconciliation_sync(self):
        """Test restart recovery reconciliation under downtime cases."""
        now_dt = datetime.now(timezone.utc)

        # Case A: Timed focus expired during downtime
        def _setup_timed_expired(st):
            st["enabled"] = True
            st["session"] = {
                "id": "sess_1",
                "type": "focus",
                "phase": "FOCUS",
                "started_at": (now_dt - timedelta(minutes=60)).isoformat(),
                "ends_at": (now_dt - timedelta(minutes=10)).isoformat(),
                "total_seconds": 1500,
                "status": "active"
            }
        await self.storage.mutate(_setup_timed_expired)
        st = self.storage.get_state()
        new_st, changed, next_dt = self.service.reconcile_downtime_sync(st)
        self.assertTrue(changed)
        self.assertIsNone(next_dt)
        self.assertEqual(new_st["session"]["phase"], "EXPIRED_LOCKED")
        self.assertEqual(new_st["session"]["remaining_seconds"], 0)

        # Case B: Pomodoro downtime spanning into BREAK
        # started 27 mins ago, work=25m, break=5m -> currently in BREAK with 3 mins remaining
        def _setup_pomo_in_break(st):
            st["enabled"] = True
            st["session"] = {
                "id": "sess_2",
                "type": "pomodoro",
                "phase": "WORK",
                "work_duration_minutes": 25,
                "break_duration_minutes": 5,
                "cycle_count": 1,
                "started_at": (now_dt - timedelta(minutes=27)).isoformat(),
                "ends_at": (now_dt - timedelta(minutes=2)).isoformat(),
                "status": "active"
            }
        await self.storage.mutate(_setup_pomo_in_break)
        st = self.storage.get_state()
        new_st, changed, next_dt = self.service.reconcile_downtime_sync(st)
        self.assertTrue(changed)
        self.assertIsNotNone(next_dt)
        self.assertEqual(new_st["session"]["phase"], "BREAK")
        ends_dt = datetime.fromisoformat(new_st["session"]["ends_at"])
        rem = (ends_dt - now_dt).total_seconds()
        self.assertGreater(rem, 0)
        self.assertLessEqual(rem, 185)


if __name__ == "__main__":
    unittest.main()
