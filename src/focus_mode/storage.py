"""
DeskFlow Focus Mode - Persistence & Storage Layer
Phase 2 implementation:
- Atomic JSON writes with fsync and directory sync
- Backup creation and automatic recovery
- Exclusive non-blocking flock for single-instance enforcement
- Asyncio lock for state mutations
- Strict file/directory permission enforcement (0700 dir, 0600 files)
- Fail-closed storage unavailability on corruption (never auto-reset to OFF)
"""

import os
import sys
import json
import copy
import fcntl
import uuid
import inspect
import asyncio
import logging
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger("DeskFlow.FocusMode.Storage")

DEFAULT_SCHEMA: Dict[str, Any] = {
    "schema_version": 2,
    "version": 0,
    "enabled": False,
    "language": "en",
    "updated_at": None,
    "devices": {},
    "pair_codes": {},
    "challenge": None,
    "policies": {
        "allow": [],
        "deny": []
    },
    "failed_attempts": 0,
    "cooldown_until": None,
    "history": [],
    "session": None,
    "current_session": None
}


class StorageUnavailableError(Exception):
    """Raised when storage is corrupted, locked by another instance, or inaccessible."""
    pass


class BackupMaintenanceWarning(UserWarning):
    """
    Warning emitted when authoritative primary commit succeeds,
    but secondary backup file maintenance fails.
    The primary file remains authoritative, intact, and consistent.
    """
    pass


@dataclass
class CommitResult:
    """
    Explicit status of a storage commit operation.
    Distinguishes authoritative primary commit from secondary backup maintenance.
    """
    primary_committed: bool
    backup_committed: bool
    backup_error: Optional[Exception] = None

    @property
    def is_authoritative_success(self) -> bool:
        """The primary file is authoritative; if primary succeeded, state is securely committed."""
        return self.primary_committed

    @property
    def has_backup_maintenance_failure(self) -> bool:
        """True if backup creation failed after successful primary commit."""
        return self.backup_error is not None


class FocusStorage:
    def __init__(self, data_dir: Optional[str] = None):
        if data_dir is None:
            # Check env var FOCUS_DATA_DIR first, else default to data relative to backend
            env_dir = os.getenv("FOCUS_DATA_DIR")
            if env_dir:
                data_dir = env_dir
            else:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                # If inside src, base_dir is _server_copy
                data_dir = os.path.join(base_dir, "data")

        self.data_dir = os.path.abspath(data_dir)
        self.primary_file = os.path.join(self.data_dir, "focus_mode.json")
        self.backup_file = os.path.join(self.data_dir, "focus_mode.json.bak")
        self.lock_file = os.path.join(self.data_dir, "focus_mode.lock")
        self.secret_file = os.path.join(self.data_dir, "focus_secret.key")

        self._lock_fd: Optional[int] = None
        self._async_lock = asyncio.Lock()
        self._state: Optional[Dict[str, Any]] = None
        self._is_available: bool = False
        self.last_commit_result: Optional[CommitResult] = None
        self.last_backup_error: Optional[Exception] = None

    @property
    def is_available(self) -> bool:
        return self._is_available

    @property
    def last_backup_maintenance_failed(self) -> bool:
        """Returns True if the most recent commit experienced a backup maintenance failure."""
        return self.last_commit_result is not None and self.last_commit_result.has_backup_maintenance_failure

    def _ensure_data_dir(self) -> None:
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError as e:
            logger.debug(f"Could not chmod data_dir to 0700: {e}")

    def acquire_process_lock(self) -> bool:
        """Acquires an exclusive, non-blocking process lock held for lifetime of service."""
        self._ensure_data_dir()
        try:
            # Open with mode 0600
            self._lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.chmod(self.lock_file, 0o600)
            except OSError:
                pass
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError) as e:
            logger.error(f"[Storage] Failed to acquire process lock on {self.lock_file}: {e}")
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
                self._lock_fd = None
            return False

    def release_process_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _validate_schema(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        required_keys = ["schema_version", "version", "enabled", "language", "devices"]
        for k in required_keys:
            if k not in data:
                return False
        if data.get("schema_version") not in (1, 2):
            return False
        if not isinstance(data.get("devices"), dict):
            return False
        session = data.get("session")
        if session is not None and not isinstance(session, dict):
            return False
        return True

    def migrate_schema_v1_to_v2(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Migrates schema v1 state to schema v2:
        - Creates a uniquely timestamped pre-migration backup before commit
        - Preserves legacy indefinite sessions without ends_at
        - Preserves all paired devices, tokens, pair_codes, policies, history
        """
        if data.get("schema_version") != 1:
            return data

        now_dt = datetime.now(timezone.utc)
        timestamp_str = now_dt.strftime("%Y%m%d_%H%M%S")
        pre_mig_backup = os.path.join(self.data_dir, f"focus_mode.json.pre_v2_{timestamp_str}.bak")

        # Create verified pre-migration backup of the raw file if primary exists
        if os.path.exists(self.primary_file):
            try:
                with open(self.primary_file, "r", encoding="utf-8") as src, open(pre_mig_backup, "w", encoding="utf-8") as dst:
                    content = src.read()
                    dst.write(content)
                    dst.flush()
                    os.fsync(dst.fileno())
                # Verify backup readability
                with open(pre_mig_backup, "r", encoding="utf-8") as v:
                    json.loads(v.read())
                logger.info(f"[Storage] Verified pre-v2 migration backup created: {pre_mig_backup}")
            except Exception as e:
                logger.error(f"[Storage] Pre-migration backup failed: {e}. Aborting schema migration.")
                raise RuntimeError(f"Pre-migration backup failed: {e}")

        migrated = copy.deepcopy(data)
        migrated["schema_version"] = 2

        # Migrate current_session to session
        curr_sess = migrated.get("current_session")
        if curr_sess and isinstance(curr_sess, dict) and curr_sess.get("status") == "active":
            start_time = curr_sess.get("start_time")
            migrated["session"] = {
                "id": curr_sess.get("id") or str(uuid.uuid4()),
                "type": "focus",
                "phase": "FOCUS",
                "started_at": start_time,
                "ends_at": None,  # Preserved as indefinite!
                "remaining_seconds": None,
                "total_seconds": None,
                "status": "active",
                "reason": curr_sess.get("reason", "Focus Session")
            }
        elif migrated.get("session") is None:
            migrated["session"] = None

        if not self._validate_schema(migrated):
            raise ValueError("Migrated state failed schema v2 validation.")

        return migrated

    def _read_file_safe(self, path: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                return None
            data = json.loads(content)
            if self._validate_schema(data):
                return data
            logger.warning(f"[Storage] Schema validation failed for {path}")
            return None
        except Exception as e:
            logger.warning(f"[Storage] Error reading/parsing {path}: {e}")
            return None

    def initialize(self) -> None:
        """Initializes storage, acquires lock, and performs startup recovery."""
        if not self.acquire_process_lock():
            self._is_available = False
            raise StorageUnavailableError("Process lock could not be acquired; another instance may be running.")

        primary_exists = os.path.exists(self.primary_file)
        backup_exists = os.path.exists(self.backup_file)

        primary_data = self._read_file_safe(self.primary_file) if primary_exists else None
        backup_data = self._read_file_safe(self.backup_file) if backup_exists else None

        if primary_data is not None:
            if primary_data.get("schema_version") == 1:
                logger.info("[Storage] Schema v1 detected on primary. Migrating to Schema v2...")
                primary_data = self.migrate_schema_v1_to_v2(primary_data)
                self._write_primary_sync(primary_data)
                self._write_backup_sync(primary_data)
            self._state = primary_data
            self._is_available = True
            # Keep backup synchronized if missing or outdated
            if backup_data is None:
                self._write_backup_sync(primary_data)
            logger.info("[Storage] Primary state loaded successfully.")
            return

        # Primary is invalid or missing
        if primary_exists and primary_data is None:
            # Primary exists but is CORRUPT
            logger.warning("[Storage] Primary state file is corrupt. Attempting backup recovery...")
            if backup_data is not None:
                if backup_data.get("schema_version") == 1:
                    backup_data = self.migrate_schema_v1_to_v2(backup_data)
                logger.info("[Storage] Recovered valid state from backup file.")
                self._state = backup_data
                self._write_primary_sync(backup_data)
                self._is_available = True
                return
                self._state = backup_data
                self._write_primary_sync(backup_data)
                self._is_available = True
                return
            else:
                # Both primary and backup are corrupt!
                # Phase 2.4: If both are corrupt, mark storage unavailable.
                # Never automatically reset the state to Focus OFF after corruption.
                logger.critical("[Storage] Both primary and backup files are corrupt! Marking storage unavailable.")
                self._is_available = False
                self._state = None
                raise StorageUnavailableError("Both primary and backup state files are corrupt.")

        # First run: primary does not exist
        if not primary_exists and not backup_exists:
            logger.info("[Storage] First startup detected. Creating initial default state.")
            initial_state = copy.deepcopy(DEFAULT_SCHEMA)
            self._write_primary_sync(initial_state)
            self._write_backup_sync(initial_state)
            self._state = initial_state
            self._is_available = True
            return

        # Backup exists but primary does not
        if not primary_exists and backup_data is not None:
            logger.info("[Storage] Restoring state from existing backup...")
            self._state = backup_data
            self._write_primary_sync(backup_data)
            self._is_available = True
            return

        # Corrupt backup and missing primary
        self._is_available = False
        self._state = None
        raise StorageUnavailableError("State is corrupt and unrecoverable.")

    def _sync_dir(self) -> None:
        try:
            dir_fd = os.open(self.data_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def _atomic_write_file(self, target_path: str, data: Dict[str, Any]) -> None:
        self._ensure_data_dir()
        tmp_name = f".tmp_{os.getpid()}_{uuid.uuid4().hex}"
        tmp_path = os.path.join(self.data_dir, tmp_name)

        payload = json.dumps(data, indent=2, ensure_ascii=False)
        fd = os.open(tmp_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass

        os.replace(tmp_path, target_path)
        self._sync_dir()

    def _write_primary_sync(self, data: Dict[str, Any]) -> None:
        self._atomic_write_file(self.primary_file, data)

    def _write_backup_sync(self, data: Dict[str, Any]) -> None:
        self._atomic_write_file(self.backup_file, data)

    def get_state(self) -> Dict[str, Any]:
        if not self._is_available or self._state is None:
            raise StorageUnavailableError("Focus Mode storage is unavailable.")
        st = copy.deepcopy(self._state)
        st.setdefault("history", [])
        st.setdefault("current_session", None)
        return st

    def _commit_both_sync(self, new_state: Dict[str, Any]) -> CommitResult:
        """
        Safe write consistency strategy:
        1. Write primary file atomically (temp file -> fsync -> os.replace -> dir fsync).
           The primary file is authoritative. If this fails, an exception is raised immediately;
           runtime state is untouched, and caller is notified of commit failure.
        2. Once primary succeeds, immediately update self._state to new_state so that
           runtime memory is NEVER left stale relative to the persisted primary file.
        3. Write backup file atomically (secondary maintenance).
           If backup write fails:
           - The primary file remains authoritative and fully committed on disk.
           - In-memory state remains synchronized with the committed primary.
           - We do NOT raise an exception that causes callers to pretend the mutation was rolled
             back or blindly retry a committed non-idempotent operation.
           - We record the backup maintenance failure on `self.last_commit_result` and `self.last_backup_error`,
             log a warning, and emit a `BackupMaintenanceWarning`.
        """
        # Step 1: Write primary atomically (authoritative commit)
        self._write_primary_sync(new_state)

        # Step 2: Primary committed on disk; synchronize runtime memory immediately
        self._state = new_state
        self.last_backup_error = None

        # Step 3: Write backup atomically (secondary maintenance)
        try:
            self._write_backup_sync(new_state)
            result = CommitResult(primary_committed=True, backup_committed=True, backup_error=None)
            self.last_commit_result = result
            return result
        except Exception as e:
            self.last_backup_error = e
            result = CommitResult(primary_committed=True, backup_committed=False, backup_error=e)
            self.last_commit_result = result
            logger.warning(
                f"[Storage] Primary state committed successfully (authoritative), "
                f"but backup maintenance failed: {e}"
            )
            warnings.warn(
                f"Primary state committed, but backup maintenance failed: {e}",
                BackupMaintenanceWarning,
                stacklevel=2
            )
            return result

    async def mutate(self, mutator_fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """
        Executes a state mutation under asyncio lock.
        The state is NOT published to in-memory consumers until persistent write succeeds.
        Returns the committed state.

        If primary write fails, an exception is raised and state is NOT updated.
        If primary write succeeds but backup maintenance fails, the mutation is committed,
        in-memory state is synchronized, and the committed state is returned (with
        backup maintenance failure recorded on self.last_commit_result).
        """
        if not self._is_available or self._state is None:
            raise StorageUnavailableError("Focus Mode storage is unavailable.")

        async with self._async_lock:
            if not self._is_available or self._state is None:
                raise StorageUnavailableError("Focus Mode storage is unavailable.")

            new_state = copy.deepcopy(self._state)
            if inspect.iscoroutinefunction(mutator_fn):
                await mutator_fn(new_state)
            else:
                res = mutator_fn(new_state)
                if asyncio.iscoroutine(res):
                    await res

            if not self._validate_schema(new_state):
                raise ValueError("Mutation resulted in an invalid schema.")

            # Commit to disk in thread pool to not block asyncio event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._commit_both_sync, new_state)

            return copy.deepcopy(self._state)
