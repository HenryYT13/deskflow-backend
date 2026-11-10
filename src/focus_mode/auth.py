"""
DeskFlow Focus Mode - Authentication & Authorization Layer
Phase 3 implementation:
- Secret management via FOCUS_SECRET or persistent 32-byte data/focus_secret.key (0600)
- 8-character cryptographically random pairing codes, 10-minute expiry, single-use, HMAC-stored
- 32-byte device tokens, 30-day sliding expiry, revocation support, HMAC-only persistence
- Constant-time comparison (hmac.compare_digest)
- Rate limiting: 5 pairing attempts/min per IP, 60 requests/min per device
- Proxy trust handling (only trust X-Forwarded-For if FOCUS_TRUST_PROXY=1)
- Strict secret and token masking (never log raw tokens, codes, or secrets)
"""

import os
import time
import hmac
import uuid
import string
import secrets
import hashlib
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List

from .storage import FocusStorage

logger = logging.getLogger("DeskFlow.FocusMode.Auth")

PAIR_CODE_TTL_SECONDS = 600  # 10 minutes
DEVICE_TOKEN_SLIDING_TTL_SECONDS = 30 * 86400  # 30 days
PAIR_RATE_LIMIT = 5  # attempts per minute
DEVICE_RATE_LIMIT = 60  # requests per minute


def get_current_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_utc(iso_str: str) -> datetime:
    # Handles trailing 'Z' or '+00:00'
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class AuthError(Exception):
    def __init__(self, message: str, code: str = "invalid_token", status: int = 401):
        super().__init__(message)
        self.code = code
        self.status = status


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int = 60):
        super().__init__(f"Rate limit exceeded. Retry after {retry_after} seconds.")
        self.code = "rate_limited"
        self.status = 429
        self.retry_after = retry_after


class FocusAuth:
    def __init__(self, storage: FocusStorage):
        self.storage = storage
        self._secret = self._load_or_create_secret()
        # In-memory sliding rate limit buckets:
        # { ip: [timestamp, ...] } for pairing
        self._pair_rate_limits: Dict[str, List[float]] = {}
        # { device_id: [timestamp, ...] } for authenticated requests
        self._device_rate_limits: Dict[str, List[float]] = {}

    def _load_or_create_secret(self) -> bytes:
        env_secret = os.getenv("FOCUS_SECRET")
        if env_secret:
            logger.info("[Auth] Secret loaded from FOCUS_SECRET environment variable.")
            return env_secret.encode("utf-8")

        secret_path = self.storage.secret_file
        if os.path.exists(secret_path):
            try:
                with open(secret_path, "rb") as f:
                    secret_bytes = f.read()
                if len(secret_bytes) >= 32:
                    logger.info("[Auth] Persistent secret loaded from file.")
                    return secret_bytes[:32]
            except Exception as e:
                logger.warning(f"[Auth] Could not read existing secret file: {e}")

        # Generate persistent race-safe secret
        logger.info("[Auth] Generating new persistent secret key...")
        secret_bytes = secrets.token_bytes(32)
        try:
            self.storage._ensure_data_dir()
            # Use os.O_CREAT | os.O_EXCL to ensure race-safe file creation
            try:
                fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(secret_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.chmod(secret_path, 0o600)
                except OSError:
                    pass
                logger.info("[Auth] Persistent secret written to secret file.")
                return secret_bytes
            except FileExistsError:
                # Concurrent creation: read the winner's secret
                with open(secret_path, "rb") as f:
                    existing = f.read()
                if len(existing) >= 32:
                    return existing[:32]
        except Exception as e:
            logger.error(f"[Auth] Could not write secret file: {e}")

        return secret_bytes

    def get_secret(self) -> bytes:
        return self._secret

    def compute_hmac(self, value: str) -> str:
        return hmac.new(self._secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    # --- Rate Limiting ---
    def check_pair_rate_limit(self, client_ip: str) -> None:
        now = time.time()
        timestamps = self._pair_rate_limits.setdefault(client_ip, [])
        # Evict older than 60s
        self._pair_rate_limits[client_ip] = [t for t in timestamps if now - t < 60.0]
        if len(self._pair_rate_limits[client_ip]) >= PAIR_RATE_LIMIT:
            oldest = self._pair_rate_limits[client_ip][0]
            retry_after = max(1, int(60.0 - (now - oldest)))
            raise RateLimitExceeded(retry_after=retry_after)
        self._pair_rate_limits[client_ip].append(now)

    def check_device_rate_limit(self, device_id: str) -> None:
        now = time.time()
        timestamps = self._device_rate_limits.setdefault(device_id, [])
        self._device_rate_limits[device_id] = [t for t in timestamps if now - t < 60.0]
        if len(self._device_rate_limits[device_id]) >= DEVICE_RATE_LIMIT:
            oldest = self._device_rate_limits[device_id][0]
            retry_after = max(1, int(60.0 - (now - oldest)))
            raise RateLimitExceeded(retry_after=retry_after)
        self._device_rate_limits[device_id].append(now)

    # --- Pairing Codes ---
    async def generate_pairing_code(self, scopes: Optional[List[str]] = None) -> Tuple[str, str]:
        """
        Generates an 8-character cryptographically random pairing code.
        Returns (raw_code, expires_at_iso).
        Raw code is NEVER logged or persisted directly.
        """
        alphabet = string.ascii_uppercase + string.digits
        # 8 random chars
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        code_hmac = self.compute_hmac(code)

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=PAIR_CODE_TTL_SECONDS)
        expires_at_iso = expires_at.isoformat()
        assigned_scopes = list(scopes) if scopes else ["focus"]

        def _mutator(state: Dict[str, Any]) -> None:
            if "pair_codes" not in state:
                state["pair_codes"] = {}
            # Clean expired codes
            now_dt = datetime.now(timezone.utc)
            valid_codes = {}
            for h, info in state["pair_codes"].items():
                try:
                    exp = parse_iso_utc(info.get("expires_at", ""))
                    if exp > now_dt and not info.get("used", False):
                        valid_codes[h] = info
                except Exception:
                    pass
            valid_codes[code_hmac] = {
                "expires_at": expires_at_iso,
                "used": False,
                "created_at": get_current_utc_iso(),
                "scopes": assigned_scopes
            }
            state["pair_codes"] = valid_codes

        await self.storage.mutate(_mutator)
        return code, expires_at_iso

    async def consume_pairing_code(
        self,
        code: str,
        device_name: str,
        platform: str,
        client_ip: str
    ) -> Dict[str, Any]:
        """
        Validates pairing code, generates device token, and registers device in one atomic mutation.
        Prevents concurrency race conditions where two requests submit the same code.
        """
        self.check_pair_rate_limit(client_ip)

        clean_code = code.strip().upper()
        if not clean_code or len(clean_code) < 6:
            raise AuthError("Invalid pairing code.", code="pair_code_invalid", status=403)

        candidate_hmac = self.compute_hmac(clean_code)

        # Generate 32-byte raw token and device ID
        raw_token = secrets.token_hex(32)
        token_hmac = self.compute_hmac(raw_token)
        device_id = f"dev_{uuid.uuid4().hex[:12]}"
        now_dt = datetime.now(timezone.utc)
        expires_at_dt = now_dt + timedelta(seconds=DEVICE_TOKEN_SLIDING_TTL_SECONDS)

        device_record = {
            "device_id": device_id,
            "token_hmac": token_hmac,
            "device_name": device_name.strip() or "DeskFlow Device",
            "platform": platform.strip() or "browser",
            "paired_at": now_dt.isoformat(),
            "last_seen_at": now_dt.isoformat(),
            "expires_at": expires_at_dt.isoformat(),
            "revoked": False,
            "scopes": ["focus"]
        }

        def _atomic_consume(st: Dict[str, Any]) -> None:
            pair_codes = st.get("pair_codes", {})

            matched_hmac = None
            for stored_hmac, info in pair_codes.items():
                if hmac.compare_digest(candidate_hmac, stored_hmac):
                    matched_hmac = stored_hmac
                    break

            if not matched_hmac:
                raise AuthError("Pairing code invalid or not found.", code="pair_code_invalid", status=403)

            info = pair_codes[matched_hmac]
            if info.get("used", False):
                raise AuthError("Pairing code has already been used.", code="pair_code_invalid", status=403)

            try:
                exp = parse_iso_utc(info.get("expires_at", ""))
                if datetime.now(timezone.utc) > exp:
                    raise AuthError("Pairing code has expired.", code="pair_code_invalid", status=403)
            except AuthError:
                raise
            except Exception:
                raise AuthError("Pairing code has expired.", code="pair_code_invalid", status=403)

            # Atomic consumption
            info["used"] = True

            # Assign authoritative scopes from code
            device_record["scopes"] = list(info.get("scopes", ["focus"]))

            # Atomic device registration
            if "devices" not in st:
                st["devices"] = {}
            st["devices"][device_id] = device_record

        # Single atomic storage mutation under lock
        await self.storage.mutate(_atomic_consume)

        logger.info(f"[Auth] Device {device_id} successfully paired (platform: {platform}, scopes: {device_record['scopes']}).")

        # Return the raw token ONLY ONCE
        return {
            "device_id": device_id,
            "device_token": raw_token,
            "device_name": device_record["device_name"],
            "platform": device_record["platform"],
            "scopes": device_record["scopes"],
            "expires_at": expires_at_dt.isoformat(),
            "paired_at": device_record["paired_at"]
        }

    # --- Device Token Verification ---
    async def authenticate_token(self, auth_header: Optional[str]) -> Dict[str, Any]:
        """
        Authenticates incoming Authorization: Bearer <token>.
        Applies sliding expiration and rate limiting.
        """
        if not auth_header or not auth_header.strip():
            raise AuthError("Missing Authorization header.", code="token_invalid", status=401)

        parts = auth_header.strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthError("Invalid Authorization format. Expected 'Bearer <token>'.", code="token_invalid", status=401)

        raw_token = parts[1].strip()
        if not raw_token:
            raise AuthError("Empty token provided.", code="token_invalid", status=401)

        candidate_hmac = self.compute_hmac(raw_token)

        state = self.storage.get_state()
        devices = state.get("devices", {})

        matched_device_id = None
        for dev_id, dev_info in devices.items():
            stored_hmac = dev_info.get("token_hmac", "")
            if hmac.compare_digest(candidate_hmac, stored_hmac):
                matched_device_id = dev_id
                break

        if not matched_device_id:
            raise AuthError("Invalid or unknown device token.", code="token_invalid", status=401)

        device = devices[matched_device_id]
        if device.get("revoked", False):
            raise AuthError("Device token has been revoked.", code="token_revoked", status=401)

        now_dt = datetime.now(timezone.utc)
        try:
            exp_dt = parse_iso_utc(device.get("expires_at", ""))
            if now_dt >= exp_dt:
                raise AuthError("Device token has expired.", code="token_expired", status=401)
        except AuthError:
            raise
        except Exception:
            raise AuthError("Device token has expired.", code="token_expired", status=401)

        # Check device rate limit
        self.check_device_rate_limit(matched_device_id)

        # Slide expiration (30 days from now) and update last_seen_at
        new_expires_dt = now_dt + timedelta(seconds=DEVICE_TOKEN_SLIDING_TTL_SECONDS)
        now_iso = now_dt.isoformat()
        new_exp_iso = new_expires_dt.isoformat()

        def _slide_mutator(st: Dict[str, Any]) -> None:
            if "devices" in st and matched_device_id in st["devices"]:
                st["devices"][matched_device_id]["last_seen_at"] = now_iso
                st["devices"][matched_device_id]["expires_at"] = new_exp_iso

        try:
            await self.storage.mutate(_slide_mutator)
        except Exception as e:
            logger.debug(f"[Auth] Sliding expiry update skipped: {e}")

        device_copy = dict(device)
        device_copy["last_seen_at"] = now_iso
        device_copy["expires_at"] = new_exp_iso
        device_copy["scopes"] = list(device.get("scopes", ["focus"]))
        return device_copy

    async def revoke_device(self, device_id: str) -> bool:
        """Revokes a device by marking it revoked."""
        found = False

        def _mutator(st: Dict[str, Any]) -> None:
            nonlocal found
            if "devices" in st and device_id in st["devices"]:
                st["devices"][device_id]["revoked"] = True
                found = True

        await self.storage.mutate(_mutator)
        return found

    async def rename_device(self, device_id: str, new_name: str) -> bool:
        """Renames a linked device."""
        found = False

        def _mutator(st: Dict[str, Any]) -> None:
            nonlocal found
            if "devices" in st and device_id in st["devices"]:
                st["devices"][device_id]["device_name"] = new_name
                found = True

        await self.storage.mutate(_mutator)
        return found
