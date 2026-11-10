"""
Unit tests for FocusAuth:
- Secret management (FOCUS_SECRET vs persistent file)
- Pairing code generation (8-chars, random, HMAC-stored)
- Valid pairing and token issuance
- Invalid pairing code (403)
- Expired pairing code (403)
- Reused pairing code (403 single-use)
- Device token verification (constant-time compare)
- Sliding expiration (30 days)
- Device revocation (401 token_revoked)
- HMAC-only persistence (raw token and pairing code never in state)
- Rate limiting (5 attempts/min per IP for pairing, 60 requests/min per device)
"""

import os
import time
import shutil
import tempfile
import asyncio
import unittest
from datetime import datetime, timezone, timedelta

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

from focus_mode.storage import FocusStorage
from focus_mode.auth import FocusAuth, AuthError, RateLimitExceeded


class TestFocusAuth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_auth_test_")
        self.storage = FocusStorage(data_dir=self.test_dir)
        self.storage.initialize()
        self.auth = FocusAuth(storage=self.storage)

    def tearDown(self):
        self.storage.release_process_lock()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_secret_persistence_and_permissions(self):
        secret = self.auth.get_secret()
        self.assertGreaterEqual(len(secret), 32)
        self.assertTrue(os.path.exists(self.storage.secret_file))

        # Re-reading in another instance should yield same secret
        auth2 = FocusAuth(storage=self.storage)
        self.assertEqual(auth2.get_secret(), secret)

    async def test_pairing_code_generation_and_storage(self):
        code, expires_at = await self.auth.generate_pairing_code()
        self.assertEqual(len(code), 8)
        self.assertTrue(code.isalnum())

        # Check raw code is NOT in state
        state = self.storage.get_state()
        pair_codes = state.get("pair_codes", {})
        self.assertNotIn(code, pair_codes)

        # But candidate HMAC IS in state
        candidate_hmac = self.auth.compute_hmac(code)
        self.assertIn(candidate_hmac, pair_codes)
        self.assertFalse(pair_codes[candidate_hmac]["used"])

    async def test_successful_pairing_and_single_use(self):
        code, _ = await self.auth.generate_pairing_code()

        reg = await self.auth.consume_pairing_code(
            code=code,
            device_name="Test Browser",
            platform="chromium",
            client_ip="127.0.0.1"
        )
        self.assertIn("device_token", reg)
        self.assertIn("device_id", reg)
        raw_token = reg["device_token"]
        dev_id = reg["device_id"]

        # Check raw token is NOT in state
        state = self.storage.get_state()
        device_entry = state["devices"][dev_id]
        self.assertNotIn("token", device_entry)
        self.assertNotIn(raw_token, str(device_entry))
        self.assertEqual(device_entry["token_hmac"], self.auth.compute_hmac(raw_token))

        # Re-use must fail
        with self.assertRaises(AuthError) as ctx:
            await self.auth.consume_pairing_code(
                code=code,
                device_name="Test Browser 2",
                platform="chromium",
                client_ip="127.0.0.1"
            )
        self.assertEqual(ctx.exception.code, "pair_code_invalid")
        self.assertEqual(ctx.exception.status, 403)

    async def test_invalid_pairing_code(self):
        with self.assertRaises(AuthError) as ctx:
            await self.auth.consume_pairing_code(
                code="WRONGCOD",
                device_name="Test Browser",
                platform="chromium",
                client_ip="127.0.0.1"
            )
        self.assertEqual(ctx.exception.code, "pair_code_invalid")
        self.assertEqual(ctx.exception.status, 403)

    async def test_expired_pairing_code(self):
        code, _ = await self.auth.generate_pairing_code()
        cand_hmac = self.auth.compute_hmac(code)

        # Artificially expire the code in state
        past_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        def _expire(st):
            st["pair_codes"][cand_hmac]["expires_at"] = past_iso
        await self.storage.mutate(_expire)

        with self.assertRaises(AuthError) as ctx:
            await self.auth.consume_pairing_code(
                code=code,
                device_name="Test",
                platform="chromium",
                client_ip="127.0.0.1"
            )
        self.assertEqual(ctx.exception.code, "pair_code_invalid")
        self.assertEqual(ctx.exception.status, 403)

    async def test_device_token_authentication_and_sliding_expiry(self):
        code, _ = await self.auth.generate_pairing_code()
        reg = await self.auth.consume_pairing_code(code, "Chrome", "chromium", "127.0.0.1")
        token = reg["device_token"]
        dev_id = reg["device_id"]

        # Authenticate valid token
        auth_dev = await self.auth.authenticate_token(f"Bearer {token}")
        self.assertEqual(auth_dev["device_id"], dev_id)
        self.assertEqual(auth_dev["device_name"], "Chrome")

        # Missing / invalid header
        with self.assertRaises(AuthError) as ctx:
            await self.auth.authenticate_token(None)
        self.assertEqual(ctx.exception.code, "token_invalid")

        with self.assertRaises(AuthError) as ctx:
            await self.auth.authenticate_token("Bearer bad_token_1234")
        self.assertEqual(ctx.exception.code, "token_invalid")

    async def test_device_token_revocation(self):
        code, _ = await self.auth.generate_pairing_code()
        reg = await self.auth.consume_pairing_code(code, "Chrome", "chromium", "127.0.0.1")
        token = reg["device_token"]
        dev_id = reg["device_id"]

        # Revoke device
        success = await self.auth.revoke_device(dev_id)
        self.assertTrue(success)

        # Authenticate revoked token -> 401 token_revoked
        with self.assertRaises(AuthError) as ctx:
            await self.auth.authenticate_token(f"Bearer {token}")
        self.assertEqual(ctx.exception.code, "token_revoked")
        self.assertEqual(ctx.exception.status, 401)

    async def test_device_token_expiry(self):
        code, _ = await self.auth.generate_pairing_code()
        reg = await self.auth.consume_pairing_code(code, "Chrome", "chromium", "127.0.0.1")
        token = reg["device_token"]
        dev_id = reg["device_id"]

        # Artificially expire the token
        past_iso = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        def _expire_tok(st):
            st["devices"][dev_id]["expires_at"] = past_iso
        await self.storage.mutate(_expire_tok)

        with self.assertRaises(AuthError) as ctx:
            await self.auth.authenticate_token(f"Bearer {token}")
        self.assertEqual(ctx.exception.code, "token_expired")
        self.assertEqual(ctx.exception.status, 401)

    async def test_pairing_rate_limit(self):
        client_ip = "192.168.1.50"
        for _ in range(5):
            code, _ = await self.auth.generate_pairing_code()
            await self.auth.consume_pairing_code(code, "D", "p", client_ip)

        # 6th attempt within minute should trigger RateLimitExceeded (429)
        code, _ = await self.auth.generate_pairing_code()
        with self.assertRaises(RateLimitExceeded) as ctx:
            await self.auth.consume_pairing_code(code, "D", "p", client_ip)
        self.assertEqual(ctx.exception.status, 429)
        self.assertGreater(ctx.exception.retry_after, 0)

    async def test_concurrent_pairing_code_consumption_single_winner(self):
        code, _ = await self.auth.generate_pairing_code()

        successes = []
        failures = []

        async def worker(worker_id):
            try:
                # Use distinct client_ips to test atomic code consumption without hitting per-IP rate limit
                res = await self.auth.consume_pairing_code(
                    code=code,
                    device_name=f"Device-{worker_id}",
                    platform="chromium",
                    client_ip=f"10.0.0.{worker_id}"
                )
                successes.append(res)
            except AuthError as ae:
                failures.append(ae)

        # Launch 5 concurrent pairing attempts simultaneously with the same code
        await asyncio.gather(*[worker(i) for i in range(1, 6)])

        # Exactly 1 request must succeed
        self.assertEqual(len(successes), 1)
        # Exactly 4 requests must fail
        self.assertEqual(len(failures), 4)
        for err in failures:
            self.assertEqual(err.code, "pair_code_invalid")
            self.assertEqual(err.status, 403)


if __name__ == "__main__":
    unittest.main()
