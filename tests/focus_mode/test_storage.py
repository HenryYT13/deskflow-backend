"""
Unit tests for FocusStorage:
- First-run initialization
- Atomic writes
- Backup creation
- Backup recovery when primary corrupt
- Both files corrupt -> StorageUnavailableError (never reset to OFF)
- Permission behavior (0700 dir, 0600 files)
- Concurrent mutations under asyncio lock
- Process lock exclusion
- Restart persistence
"""

import os
import stat
import json
import shutil
import tempfile
import asyncio
import unittest
from pathlib import Path

import sys
import types
if sys.version_info >= (3, 13) and "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../_server_copy/src")))

from focus_mode.storage import (
    FocusStorage,
    StorageUnavailableError,
    BackupMaintenanceWarning,
    CommitResult,
    DEFAULT_SCHEMA,
)
from focus_mode.auth import FocusAuth, AuthError
from focus_mode.service import FocusService
from focus_mode.challenge import MathChallengeSystem
from focus_mode.classifier import WebsiteClassifier


class TestFocusStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="deskflow_storage_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_first_run_initialization(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()
        self.assertTrue(storage.is_available)

        state = storage.get_state()
        self.assertIn(state["schema_version"], (1, 2))
        self.assertEqual(state["version"], 0)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["language"], "en")
        self.assertTrue(os.path.exists(storage.primary_file))
        self.assertTrue(os.path.exists(storage.backup_file))
        storage.release_process_lock()

    def test_file_and_dir_permissions(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()

        dir_stat = os.stat(storage.data_dir)
        self.assertEqual(oct(stat.S_IMODE(dir_stat.st_mode)), oct(0o700))

        primary_stat = os.stat(storage.primary_file)
        self.assertEqual(oct(stat.S_IMODE(primary_stat.st_mode)), oct(0o600))

        backup_stat = os.stat(storage.backup_file)
        self.assertEqual(oct(stat.S_IMODE(backup_stat.st_mode)), oct(0o600))
        storage.release_process_lock()

    async def test_atomic_writes_and_persistence(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()

        async def _mutate(state):
            state["enabled"] = True
            state["version"] = 1

        await storage.mutate(_mutate)
        state = storage.get_state()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["version"], 1)

        # Inspect on-disk primary
        with open(storage.primary_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertTrue(disk_data["enabled"])
        self.assertEqual(disk_data["version"], 1)

        # Inspect on-disk backup
        with open(storage.backup_file, "r", encoding="utf-8") as f:
            backup_data = json.load(f)
        self.assertTrue(backup_data["enabled"])
        self.assertEqual(backup_data["version"], 1)

        storage.release_process_lock()

        # Re-open in a new storage instance (simulating restart)
        storage2 = FocusStorage(data_dir=self.test_dir)
        storage2.initialize()
        state2 = storage2.get_state()
        self.assertTrue(state2["enabled"])
        self.assertEqual(state2["version"], 1)
        storage2.release_process_lock()

    def test_backup_recovery_when_primary_corrupt(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()

        # Update backup with enabled=True
        valid_backup = storage.get_state()
        valid_backup["enabled"] = True
        valid_backup["version"] = 42
        with open(storage.backup_file, "w", encoding="utf-8") as f:
            json.dump(valid_backup, f)

        # Corrupt primary
        with open(storage.primary_file, "w", encoding="utf-8") as f:
            f.write("{ corrupt json truncated ...")

        storage.release_process_lock()

        # New instance startup should recover from backup
        recovery_storage = FocusStorage(data_dir=self.test_dir)
        recovery_storage.initialize()
        self.assertTrue(recovery_storage.is_available)
        st = recovery_storage.get_state()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["version"], 42)
        recovery_storage.release_process_lock()

    def test_both_files_corrupt_fails_closed(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()

        # Corrupt both files
        with open(storage.primary_file, "w", encoding="utf-8") as f:
            f.write("corrupted garbage")
        with open(storage.backup_file, "w", encoding="utf-8") as f:
            f.write("corrupted backup garbage")

        storage.release_process_lock()

        storage_corrupt = FocusStorage(data_dir=self.test_dir)
        with self.assertRaises(StorageUnavailableError):
            storage_corrupt.initialize()

        self.assertFalse(storage_corrupt.is_available)
        with self.assertRaises(StorageUnavailableError):
            storage_corrupt.get_state()

        storage_corrupt.release_process_lock()

    def test_process_lock_exclusion(self):
        storage1 = FocusStorage(data_dir=self.test_dir)
        storage1.initialize()
        self.assertTrue(storage1.is_available)

        # Attempt to open second instance with same lock file
        storage2 = FocusStorage(data_dir=self.test_dir)
        with self.assertRaises(StorageUnavailableError):
            storage2.initialize()

        self.assertFalse(storage2.is_available)

        # Release first lock, second should succeed
        storage1.release_process_lock()
        storage2.initialize()
        self.assertTrue(storage2.is_available)
        storage2.release_process_lock()

    async def test_concurrent_mutations(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()

        async def worker(idx):
            for _ in range(10):
                def _inc(st):
                    st["version"] += 1
                await storage.mutate(_inc)

        tasks = [worker(i) for i in range(5)]
        await asyncio.gather(*tasks)

        final_state = storage.get_state()
        self.assertEqual(final_state["version"], 50)
        storage.release_process_lock()

    async def test_fault_injection_primary_write_failure(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()
        initial_state = storage.get_state()
        self.assertEqual(initial_state["version"], 0)

        # Inject failure into primary write
        def broken_primary(data):
            raise IOError("Simulated disk error on primary write")

        original_primary = storage._write_primary_sync
        storage._write_primary_sync = broken_primary

        try:
            with self.assertRaises(IOError):
                await storage.mutate(lambda st: st.update({"version": 99, "enabled": True}))

            # Runtime state must remain old_state
            self.assertEqual(storage.get_state()["version"], 0)
            self.assertFalse(storage.get_state()["enabled"])

            # Disk primary must remain old_state
            with open(storage.primary_file, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            self.assertEqual(disk_data["version"], 0)
            self.assertFalse(disk_data["enabled"])
        finally:
            storage._write_primary_sync = original_primary
            storage.release_process_lock()

    async def test_fault_injection_backup_write_failure(self):
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()
        initial_state = storage.get_state()
        self.assertEqual(initial_state["version"], 0)

        # Inject failure into backup write AFTER primary write succeeds
        def broken_backup(data):
            raise IOError("Simulated disk error on backup write")

        original_backup = storage._write_backup_sync
        storage._write_backup_sync = broken_backup

        try:
            import warnings
            with warnings.catch_warnings(record=True) as captured_warnings:
                warnings.simplefilter("always")
                # Mutate MUST NOT raise an error; primary write succeeded and is authoritative
                res = await storage.mutate(lambda st: st.update({"version": 99, "enabled": True}))

            self.assertEqual(res["version"], 99)
            self.assertTrue(res["enabled"])

            # Verify explicit distinction: primary success vs backup maintenance failure
            self.assertTrue(storage.last_backup_maintenance_failed)
            self.assertIsNotNone(storage.last_commit_result)
            self.assertTrue(storage.last_commit_result.is_authoritative_success)
            self.assertTrue(storage.last_commit_result.has_backup_maintenance_failure)
            self.assertIsInstance(storage.last_backup_error, IOError)

            # Warning emitted
            self.assertTrue(any(issubclass(w.category, BackupMaintenanceWarning) for w in captured_warnings))

            # Runtime state MUST NOT be stale! It must reflect the committed primary state
            current_state = storage.get_state()
            self.assertEqual(current_state["version"], 99)
            self.assertTrue(current_state["enabled"])

            # Disk primary must have new_state
            with open(storage.primary_file, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            self.assertEqual(disk_data["version"], 99)
            self.assertTrue(disk_data["enabled"])

            # Runtime state and persisted primary are 100% consistent
            self.assertEqual(current_state, disk_data)
        finally:
            storage._write_backup_sync = original_backup
            storage.release_process_lock()

    async def test_fault_injection_backup_failure_on_pairing(self):
        """
        Caller test for device pairing:
        When backup write fails, pairing still succeeds, device is registered,
        code is consumed, and non-idempotent operation is NOT rolled back.
        """
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()
        auth = FocusAuth(storage=storage)

        code, _ = await auth.generate_pairing_code()

        def broken_backup(data):
            raise IOError("Simulated disk error on backup write during pairing")

        storage._write_backup_sync = broken_backup

        try:
            # Caller operation MUST SUCCEED because primary commit is authoritative
            pair_res = await auth.consume_pairing_code(
                code=code,
                device_name="Chrome PC",
                platform="chromium",
                client_ip="127.0.0.1"
            )
            self.assertIn("device_token", pair_res)
            self.assertIn("device_id", pair_res)

            # In-memory and primary disk state show device registered and code consumed
            state = storage.get_state()
            self.assertIn(pair_res["device_id"], state["devices"])
            code_hmac = auth.compute_hmac(code)
            self.assertTrue(state["pair_codes"][code_hmac]["used"])

            with open(storage.primary_file, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            self.assertIn(pair_res["device_id"], disk_data["devices"])
            self.assertTrue(disk_data["pair_codes"][code_hmac]["used"])

            self.assertTrue(storage.last_backup_maintenance_failed)

            # Code was consumed; caller does not blindly retry a committed non-idempotent operation
            with self.assertRaises(AuthError) as ctx:
                await auth.consume_pairing_code(
                    code=code,
                    device_name="Second Attempt",
                    platform="chromium",
                    client_ip="127.0.0.1"
                )
            self.assertEqual(ctx.exception.code, "pair_code_invalid")
        finally:
            storage.release_process_lock()

    async def test_fault_injection_backup_failure_on_state_transition(self):
        """
        Caller test for state transitions:
        When backup write fails, FocusService.enable() succeeds and state is ON.
        Never reset Focus Mode to OFF.
        """
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()
        auth = FocusAuth(storage=storage)
        challenge_sys = MathChallengeSystem(storage=storage, auth=auth, groq_client=None)
        classifier = WebsiteClassifier(storage=storage, groq_client=None)
        service = FocusService(
            storage=storage,
            auth=auth,
            challenge=challenge_sys,
            classifier=classifier
        )

        self.assertFalse(storage.get_state()["enabled"])

        def broken_backup(data):
            raise IOError("Simulated disk error on backup write during state transition")

        storage._write_backup_sync = broken_backup

        try:
            res = await service.enable()
            self.assertTrue(res["enabled"])
            self.assertTrue(storage.get_state()["enabled"])

            with open(storage.primary_file, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            self.assertTrue(disk_data["enabled"])

            self.assertTrue(storage.last_backup_maintenance_failed)
        finally:
            storage.release_process_lock()

    async def test_fault_injection_backup_failure_on_challenge_creation(self):
        """
        Caller test for challenge creation:
        When backup write fails, get_or_create_challenge() succeeds and stores challenge in primary.
        """
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()
        auth = FocusAuth(storage=storage)
        challenge_sys = MathChallengeSystem(storage=storage, auth=auth, groq_client=None)
        classifier = WebsiteClassifier(storage=storage, groq_client=None)
        service = FocusService(
            storage=storage,
            auth=auth,
            challenge=challenge_sys,
            classifier=classifier
        )
        await service.enable()

        def broken_backup(data):
            raise IOError("Simulated disk error on backup write during challenge creation")

        storage._write_backup_sync = broken_backup

        try:
            pub_ch = await challenge_sys.get_or_create_challenge("en")
            self.assertIsNotNone(pub_ch)
            self.assertIn("challenge_id", pub_ch)
            self.assertIn("question", pub_ch)

            st = storage.get_state()
            self.assertIsNotNone(st["challenge"])
            self.assertEqual(st["challenge"]["challenge_id"], pub_ch["challenge_id"])

            with open(storage.primary_file, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            self.assertEqual(disk_data["challenge"]["challenge_id"], pub_ch["challenge_id"])

            self.assertTrue(storage.last_backup_maintenance_failed)
        finally:
            storage.release_process_lock()

    async def test_fault_injection_backup_failure_on_challenge_verification(self):
        """
        Caller test for challenge verification:
        When backup write fails, verify_challenge() succeeds, verifies correct answer,
        and disables Focus Mode without throwing or rolling back.
        """
        storage = FocusStorage(data_dir=self.test_dir)
        storage.initialize()
        auth = FocusAuth(storage=storage)
        challenge_sys = MathChallengeSystem(storage=storage, auth=auth, groq_client=None)
        classifier = WebsiteClassifier(storage=storage, groq_client=None)
        service = FocusService(
            storage=storage,
            auth=auth,
            challenge=challenge_sys,
            classifier=classifier
        )
        await service.enable()

        pub_ch = await challenge_sys.get_or_create_challenge("en")
        challenge_id = pub_ch["challenge_id"]

        state = storage.get_state()
        stored_hmac = state["challenge"]["answer_hmac"]
        correct_ans = None
        for cand in range(-1000, 1001):
            if auth.compute_hmac(f"{challenge_id}:{cand}") == stored_hmac:
                correct_ans = cand
                break
        self.assertIsNotNone(correct_ans)

        def broken_backup(data):
            raise IOError("Simulated disk error on backup write during challenge verification")

        storage._write_backup_sync = broken_backup

        try:
            res = await challenge_sys.verify_challenge(challenge_id, correct_ans)
            self.assertTrue(res["correct"])
            self.assertFalse(res["enabled"])

            self.assertFalse(storage.get_state()["enabled"])
            self.assertIsNone(storage.get_state()["challenge"])

            with open(storage.primary_file, "r", encoding="utf-8") as f:
                disk_data = json.load(f)
            self.assertFalse(disk_data["enabled"])
            self.assertIsNone(disk_data["challenge"])

            self.assertTrue(storage.last_backup_maintenance_failed)
        finally:
            storage.release_process_lock()


if __name__ == "__main__":
    unittest.main()
