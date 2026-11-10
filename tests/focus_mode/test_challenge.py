"""
Unit tests for MathChallengeSystem:
- Every supported challenge type (arith, linear, percent, quad)
- Server-side question text formatting and templates (en and vi)
- Schema and bounds validation (magnitude <= 1000, integer answers)
- Groq failure fallback and invalid response fallback to random.SystemRandom()
- Correct answer unlocks Focus Mode (OFF), resets failed attempts, increments version
- Incorrect answer keeps Focus Mode ON, increments failed attempts, installs replacement challenge
- Expired challenge handling
- Replay / unknown challenge prevention (409 challenge_not_active)
- Cooldown logic: 5 consecutive failures triggers 60s cooldown (429)
- Concurrent verification serialization
"""

import os
import shutil
import tempfile
import asyncio
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

from focus_mode.storage import FocusStorage
from focus_mode.auth import FocusAuth
from focus_mode.challenge import (
    MathChallengeSystem,
    ChallengeError,
    CooldownActiveError,
    ChallengeNotActiveError,
    MAX_ANSWER_MAGNITUDE
)


class TestMathChallengeSystem(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_challenge_test_")
        self.storage = FocusStorage(data_dir=self.test_dir)
        self.storage.initialize()
        self.auth = FocusAuth(storage=self.storage)
        self.challenge = MathChallengeSystem(storage=self.storage, auth=self.auth, groq_client=None)

    def tearDown(self):
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def _enable_focus(self):
        def _en(st):
            st["enabled"] = True
            st["version"] = 1
        await self.storage.mutate(_en)

    def test_all_supported_question_types(self):
        # 1. arith (+)
        q_en, ans = self.challenge._format_question("arith", {"op": "+", "a": 12, "b": 34}, "en")
        self.assertEqual(ans, 46)
        self.assertIn("12 + 34", q_en)

        # arith (-)
        q_vi, ans = self.challenge._format_question("arith", {"op": "-", "a": 50, "b": 20}, "vi")
        self.assertEqual(ans, 30)
        self.assertIn("50 - 20", q_vi)

        # arith (*)
        q_en, ans = self.challenge._format_question("arith", {"op": "*", "a": 7, "b": 8}, "en")
        self.assertEqual(ans, 56)
        self.assertIn("7 × 8", q_en)

        # 2. linear: a*x + b = c -> 3x - 5 = 10 -> 3x = 15 -> x = 5
        q_en, ans = self.challenge._format_question("linear", {"a": 3, "b": -5, "c": 10}, "en")
        self.assertEqual(ans, 5)
        self.assertIn("3x", q_en)

        # 3. percent: 25% of 80 -> 20
        q_en, ans = self.challenge._format_question("percent", {"p": 25, "n": 80}, "en")
        self.assertEqual(ans, 20)
        self.assertIn("25% of 80", q_en)

        # 4. quad: roots -3 and 4 -> (x+3)(x-4) = x^2 - x - 12 = 0. larger root = 4
        q_en, ans = self.challenge._format_question("quad", {"r1": -3, "r2": 4}, "en")
        self.assertEqual(ans, 4)
        self.assertIn("x²", q_en)

    def test_parameter_bounds_and_validations(self):
        # Non-integer linear
        with self.assertRaises(ValueError):
            self.challenge._format_question("linear", {"a": 3, "b": 1, "c": 3}, "en")

        # Non-integer percent
        with self.assertRaises(ValueError):
            self.challenge._format_question("percent", {"p": 15, "n": 33}, "en")

        # Non-distinct quad roots
        with self.assertRaises(ValueError):
            self.challenge._format_question("quad", {"r1": 5, "r2": 5}, "en")

    async def test_groq_failure_and_offline_fallback(self):
        # Groq client raises exception
        mock_groq = MagicMock()
        mock_groq.chat.completions.create.side_effect = RuntimeError("API unreachable")

        sys_ch = MathChallengeSystem(storage=self.storage, auth=self.auth, groq_client=mock_groq)
        ch_id, q_text, ans_hmac, exp_iso = await sys_ch.generate_challenge_data("en")
        self.assertTrue(ch_id.startswith("ch_"))
        self.assertTrue(len(q_text) > 0)
        self.assertTrue(len(ans_hmac) > 0)

    async def test_groq_invalid_response_fallback(self):
        # Groq client returns malformed JSON
        mock_groq = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "I am an AI, I cannot output JSON."
        mock_groq.chat.completions.create.return_value.choices = [mock_choice]

        sys_ch = MathChallengeSystem(storage=self.storage, auth=self.auth, groq_client=mock_groq)
        ch_id, q_text, ans_hmac, exp_iso = await sys_ch.generate_challenge_data("vi")
        self.assertTrue(ch_id.startswith("ch_"))
        self.assertTrue(len(q_text) > 0)

    async def test_correct_answer_disables_focus(self):
        await self._enable_focus()

        # Create challenge
        pub_ch = await self.challenge.get_or_create_challenge("en")
        ch_id = pub_ch["challenge_id"]

        # Find the correct answer by brute force in [-1000, 1000]
        # (Since we compute HMAC(secret, ch_id + ":" + ans))
        state = self.storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        correct_ans = None
        for cand in range(-1000, 1001):
            if self.auth.compute_hmac(f"{ch_id}:{cand}") == stored_hmac:
                correct_ans = cand
                break
        self.assertIsNotNone(correct_ans)

        # Verify correct answer
        res = await self.challenge.verify_challenge(ch_id, correct_ans)
        self.assertTrue(res["correct"])
        self.assertFalse(res["enabled"])
        self.assertFalse(res["focus_active"])

        # Check state
        st = self.storage.get_state()
        self.assertFalse(st["enabled"])
        self.assertIsNone(st["challenge"])
        self.assertEqual(st["failed_attempts"], 0)

    async def test_incorrect_answer_increments_failures_and_replaces_challenge(self):
        await self._enable_focus()
        pub_ch = await self.challenge.get_or_create_challenge("en")
        ch_id = pub_ch["challenge_id"]

        # Submit wrong answer
        res = await self.challenge.verify_challenge(ch_id, -99999)
        self.assertFalse(res["correct"])
        self.assertTrue(res["enabled"])
        self.assertIn("new_challenge", res)
        self.assertNotEqual(res["new_challenge"]["challenge_id"], ch_id)

        st = self.storage.get_state()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["failed_attempts"], 1)
        self.assertEqual(st["challenge"]["challenge_id"], res["new_challenge"]["challenge_id"])

    async def test_cooldown_after_five_failed_attempts(self):
        await self._enable_focus()

        # Submit 5 incorrect answers consecutively
        for _ in range(5):
            pub_ch = await self.challenge.get_or_create_challenge("en")
            ch_id = pub_ch["challenge_id"]
            await self.challenge.verify_challenge(ch_id, -99999)

        # 6th attempt must trigger CooldownActiveError (429)
        st = self.storage.get_state()
        self.assertIsNotNone(st["cooldown_until"])

        with self.assertRaises(CooldownActiveError) as ctx:
            await self.challenge.get_or_create_challenge("en")
        self.assertEqual(ctx.exception.status, 429)
        self.assertGreater(ctx.exception.retry_after, 0)

    async def test_replayed_or_unknown_challenge_rejected(self):
        await self._enable_focus()
        with self.assertRaises(ChallengeNotActiveError):
            await self.challenge.verify_challenge("ch_unknown_12345", 42)

    async def test_expired_challenge_never_disables_focus(self):
        await self._enable_focus()
        pub_ch = await self.challenge.get_or_create_challenge("en")
        ch_id = pub_ch["challenge_id"]

        # Expire challenge in state
        past_iso = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        def _exp(st):
            st["challenge"]["expires_at"] = past_iso
        await self.storage.mutate(_exp)

        # Find what would have been the correct answer
        state = self.storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        correct_ans = None
        for cand in range(-1000, 1001):
            if self.auth.compute_hmac(f"{ch_id}:{cand}") == stored_hmac:
                correct_ans = cand
                break

        # Even with the "correct" answer, expired challenge MUST FAIL and NOT disable focus
        res = await self.challenge.verify_challenge(ch_id, correct_ans)
        self.assertFalse(res["correct"])
        self.assertTrue(res["enabled"])
        self.assertEqual(res["error"], "challenge_expired")

        st = self.storage.get_state()
        self.assertTrue(st["enabled"])

    async def test_concurrent_challenge_creation_single_invariant(self):
        await self._enable_focus()

        # Launch 5 concurrent calls to get_or_create_challenge simultaneously
        results = await asyncio.gather(*[
            self.challenge.get_or_create_challenge("en") for _ in range(5)
        ])

        # All 5 calls must receive the EXACT same challenge ID
        first_id = results[0]["challenge_id"]
        for res in results:
            self.assertEqual(res["challenge_id"], first_id)

        # State must only have this one active challenge
        st = self.storage.get_state()
        self.assertEqual(st["challenge"]["challenge_id"], first_id)

    async def test_concurrent_answer_verification_single_winner(self):
        await self._enable_focus()
        pub_ch = await self.challenge.get_or_create_challenge("en")
        ch_id = pub_ch["challenge_id"]

        # Find the correct answer
        state = self.storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        correct_ans = None
        for cand in range(-1000, 1001):
            if self.auth.compute_hmac(f"{ch_id}:{cand}") == stored_hmac:
                correct_ans = cand
                break

        successes = []
        conflicts = []

        async def submit(idx):
            try:
                res = await self.challenge.verify_challenge(ch_id, correct_ans)
                successes.append(res)
            except ChallengeError as e:
                conflicts.append(e)

        # Launch 3 concurrent verification requests simultaneously with the same challenge ID
        await asyncio.gather(*[submit(i) for i in range(3)])

        # Exactly 1 request succeeds in solving the challenge
        self.assertEqual(len(successes), 1)
        self.assertTrue(successes[0]["correct"])
        self.assertFalse(successes[0]["enabled"])

        # The other 2 requests receive 409 errors (challenge_not_active or focus_not_active)
        self.assertEqual(len(conflicts), 2)
        for c in conflicts:
            self.assertIn(c.code, ["challenge_not_active", "focus_not_active"])
            self.assertEqual(c.status, 409)


if __name__ == "__main__":
    unittest.main()
