"""
DeskFlow Focus Mode - Central Service Layer
Backend-Authoritative Session Engine:
- Python backend is the single source of truth for Focus Mode
- Full support for Regular Timed Focus & Pomodoro WORK / BREAK cycles
- Enforces User Decision 1 (Option B): Timed focus expiration -> FOCUS_EXPIRED_LOCKED
- Enforces User Decision 2 (Option A): Pomodoro BREAK suspends blocking (focus_active=False)
- Enforces User Decision 3 (Option B): Early cancellation strictly requires math challenge at all times
- Synchronous downtime reconciliation across missed cycles
- Retained MQTT state publishing (deskflow/focus/status)
- Sanitized public status vs authenticated detailed status
"""

import uuid
import copy
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Callable, Tuple

from .storage import FocusStorage, StorageUnavailableError
from .auth import FocusAuth, parse_iso_utc, get_current_utc_iso
from .challenge import MathChallengeSystem
from .classifier import WebsiteClassifier

logger = logging.getLogger("DeskFlow.FocusMode.Service")


class FocusService:
    def __init__(
        self,
        storage: FocusStorage,
        auth: FocusAuth,
        challenge: MathChallengeSystem,
        classifier: WebsiteClassifier
    ):
        self.storage = storage
        self.auth = auth
        self.challenge = challenge
        self.classifier = classifier
        self._mqtt_publisher: Optional[Callable[[str, Dict[str, Any], bool, int], None]] = None
        self._deadline_callback: Optional[Callable[[Optional[datetime]], None]] = None

    def set_mqtt_publisher(self, publisher: Callable[[str, Dict[str, Any], bool, int], None]) -> None:
        """Registers a callback for retained MQTT status broadcasting."""
        self._mqtt_publisher = publisher

    def set_deadline_callback(self, callback: Callable[[Optional[datetime]], None]) -> None:
        """Registers a callback to re-arm the deadline scheduler when ends_at changes."""
        self._deadline_callback = callback

    def _notify_deadline_scheduler(self, ends_at_dt: Optional[datetime]) -> None:
        if self._deadline_callback:
            try:
                self._deadline_callback(ends_at_dt)
            except Exception as e:
                logger.warning(f"[FocusService] Error in deadline callback: {e}")

    def _publish_mqtt(self, state: Dict[str, Any]) -> None:
        if not self._mqtt_publisher:
            return
        try:
            status = self._format_status_payload(state)
            mqtt_payload = {
                "action": "focus_status",
                "version": status["version"],
                "enabled": status["enabled"],
                "focus_active": status["focus_active"],
                "session_type": status["session_type"],
                "phase": status["phase"],
                "remaining_seconds": status["remaining_seconds"],
                "total_seconds": status["total_seconds"],
                "cycle_count": status.get("cycle_count", 0),
                "ends_at": status["ends_at"],
                "started_at": status.get("started_at"),
                "server_time": status["server_time"]
            }
            self._mqtt_publisher("deskflow/focus/status", mqtt_payload, True, 1)
        except Exception as e:
            logger.warning(f"[FocusService] Failed to broadcast MQTT focus status: {e}")

    def _format_status_payload(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Calculates dynamic remaining seconds and authoritative phase from raw state."""
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        enabled = bool(state.get("enabled", False))
        session = state.get("session") or state.get("current_session")

        if not enabled or not session:
            return {
                "success": True,
                "enabled": False,
                "focus_active": False,
                "phase": "IDLE",
                "session_type": "none",
                "remaining_seconds": 0,
                "total_seconds": 0,
                "cycle_count": 0,
                "ends_at": None,
                "started_at": None,
                "version": state.get("version", 0),
                "language": state.get("language", "en"),
                "server_time": now_iso
            }

        session_type = session.get("type", "focus")
        phase = session.get("phase", "FOCUS")
        ends_at_str = session.get("ends_at")
        started_at_str = session.get("started_at") or session.get("start_time")
        cycle_count = int(session.get("cycle_count", 1)) if session_type == "pomodoro" else 0

        # Calculate remaining seconds dynamically
        remaining_seconds = 0
        total_seconds = session.get("total_seconds") or 0

        if ends_at_str:
            try:
                ends_at_dt = parse_iso_utc(ends_at_str)
                rem = int((ends_at_dt - now_dt).total_seconds())
                if rem <= 0:
                    remaining_seconds = 0
                    if phase == "FOCUS" and session_type == "focus":
                        phase = "EXPIRED_LOCKED"
                else:
                    remaining_seconds = rem
            except Exception:
                remaining_seconds = 0
        else:
            # Indefinite regular session
            remaining_seconds = None
            total_seconds = None

        # Determine website blocking policy
        # Decision 2 (Option A): During Pomodoro BREAK, website blocking is suspended (focus_active=False)
        # In all other active states (FOCUS, WORK, EXPIRED_LOCKED), website blocking is active (focus_active=True)
        focus_active = (phase != "BREAK") if enabled else False

        return {
            "success": True,
            "enabled": enabled,
            "focus_active": focus_active,
            "phase": phase,
            "session_type": session_type,
            "remaining_seconds": remaining_seconds,
            "total_seconds": total_seconds,
            "cycle_count": cycle_count,
            "ends_at": ends_at_str,
            "started_at": started_at_str,
            "version": state.get("version", 0),
            "language": state.get("language", "en"),
            "server_time": now_iso
        }

    def get_public_status(self) -> Dict[str, Any]:
        """
        Returns sanitized public status snapshot.
        Excludes personal notes, session IDs, device tokens, and challenge answers.
        """
        state = self.storage.get_state()
        status = self._format_status_payload(state)
        # Challenge indication without leaking question or answers
        active_ch = state.get("challenge")
        ch_info = None
        if active_ch:
            try:
                now_dt = datetime.now(timezone.utc)
                exp_dt = parse_iso_utc(active_ch.get("expires_at", ""))
                if now_dt < exp_dt:
                    ch_info = {
                        "active": True,
                        "challenge_id": active_ch.get("challenge_id"),
                        "expires_at": active_ch.get("expires_at")
                    }
            except Exception:
                pass
        status["challenge"] = ch_info
        return status

    def get_detailed_status(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns authenticated detailed status including session reason and challenge object."""
        state = self.storage.get_state()
        public = self.get_public_status()
        session = state.get("session") or state.get("current_session")
        if session:
            public["session"] = {
                "id": session.get("id"),
                "type": session.get("type", "focus"),
                "phase": public["phase"],
                "remaining_seconds": public["remaining_seconds"],
                "total_seconds": public["total_seconds"],
                "cycle_count": public.get("cycle_count", 0),
                "started_at": session.get("started_at") or session.get("start_time"),
                "ends_at": session.get("ends_at"),
                "reason": session.get("reason", "")
            }
        else:
            public["session"] = None
        return public

    async def enable(
        self,
        duration_minutes: Optional[int] = 25,
        break_minutes: Optional[int] = 5,
        reason: Optional[str] = None,
        language: Optional[str] = None,
        session_type: str = "focus"
    ) -> Dict[str, Any]:
        """
        Enables Focus Mode idempotently or starts a new session if idle.
        Honors duration_minutes and reason sent by Android or Extension.
        """
        return await self.start_session(
            session_type=session_type,
            duration_minutes=duration_minutes,
            break_minutes=break_minutes,
            reason=reason,
            language=language
        )

    async def start_session(
        self,
        session_type: str = "focus",
        duration_minutes: Optional[int] = 25,
        break_minutes: Optional[int] = 5,
        reason: Optional[str] = None,
        language: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Starts an authoritative Focus or Pomodoro session.
        """
        if session_type not in ("focus", "pomodoro"):
            session_type = "focus"

        target_lang = language if language in ["en", "vi"] else None
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        # Idempotency and anti-bypass check: cannot start new session if already active
        curr_state = self.storage.get_state()
        if curr_state.get("enabled"):
            if target_lang and target_lang != curr_state.get("language"):
                def _lang_mutator(st: Dict[str, Any]) -> None:
                    st["language"] = target_lang
                    st["version"] = st.get("version", 0) + 1
                    st["updated_at"] = now_iso
                committed = await self.storage.mutate(_lang_mutator)
                self._publish_mqtt(committed)
                return self.get_public_status()
            return self.get_public_status()

        clean_reason = (reason or "Focus Session").strip()[:128]
        new_session_id = str(uuid.uuid4())

        ends_at_iso = None
        ends_at_dt = None
        total_seconds = None

        if session_type == "pomodoro":
            work_m = max(1, int(duration_minutes or 25))
            break_m = max(1, int(break_minutes or 5))
            total_seconds = work_m * 60
            ends_at_dt = now_dt + timedelta(seconds=total_seconds)
            ends_at_iso = ends_at_dt.isoformat()
            new_session = {
                "id": new_session_id,
                "type": "pomodoro",
                "phase": "WORK",
                "work_duration_minutes": work_m,
                "break_duration_minutes": break_m,
                "cycle_count": 1,
                "started_at": now_iso,
                "ends_at": ends_at_iso,
                "total_seconds": total_seconds,
                "reason": clean_reason,
                "status": "active"
            }
        else:
            # Regular focus
            if duration_minutes is not None and int(duration_minutes) > 0:
                dur_m = int(duration_minutes)
                total_seconds = dur_m * 60
                ends_at_dt = now_dt + timedelta(seconds=total_seconds)
                ends_at_iso = ends_at_dt.isoformat()
            else:
                total_seconds = None
                ends_at_dt = None
                ends_at_iso = None

            new_session = {
                "id": new_session_id,
                "type": "focus",
                "phase": "FOCUS",
                "started_at": now_iso,
                "ends_at": ends_at_iso,
                "total_seconds": total_seconds,
                "reason": clean_reason,
                "status": "active"
            }

        def _start_mutator(st: Dict[str, Any]) -> None:
            st["enabled"] = True
            st["session"] = copy.deepcopy(new_session)
            st["current_session"] = copy.deepcopy(new_session)  # Backwards compatibility
            st["challenge"] = None
            st["failed_attempts"] = 0
            st["cooldown_until"] = None
            if target_lang:
                st["language"] = target_lang
            st["version"] = st.get("version", 0) + 1
            st["updated_at"] = now_iso

        committed_state = await self.storage.mutate(_start_mutator)
        logger.info(f"[FocusService] Started {session_type} session (version {committed_state['version']}, ends_at={ends_at_iso})")

        # Notify scheduler and publish MQTT
        self._notify_deadline_scheduler(ends_at_dt)
        self._publish_mqtt(committed_state)

        return self.get_public_status()

    async def advance_deadline(self) -> Dict[str, Any]:
        """
        Called when a scheduled deadline expires.
        Executes authoritative state transitions:
        - Timed Focus -> FOCUS_EXPIRED_LOCKED (Decision 1 - Option B)
        - Pomodoro WORK -> BREAK (Decision 2 - Option A)
        - Pomodoro BREAK -> WORK (cycle_count++)
        """
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        next_deadline_dt: Optional[datetime] = None

        def _deadline_mutator(st: Dict[str, Any]) -> None:
            nonlocal next_deadline_dt
            if not st.get("enabled"):
                return

            sess = st.get("session") or st.get("current_session")
            if not sess or sess.get("status") != "active":
                return

            sess_type = sess.get("type", "focus")
            current_phase = sess.get("phase", "FOCUS")

            if sess_type == "focus":
                # Timed focus reached zero -> Enter EXPIRED_LOCKED
                sess["phase"] = "EXPIRED_LOCKED"
                sess["remaining_seconds"] = 0
                st["session"] = sess
                st["current_session"] = sess
                st["version"] = st.get("version", 0) + 1
                st["updated_at"] = now_iso
                next_deadline_dt = None
                logger.info("[FocusService] Timed Focus expired -> FOCUS_EXPIRED_LOCKED (blocking remains active)")

            elif sess_type == "pomodoro":
                work_m = max(1, int(sess.get("work_duration_minutes", 25)))
                break_m = max(1, int(sess.get("break_duration_minutes", 5)))

                if current_phase == "WORK":
                    # Transition to BREAK
                    sess["phase"] = "BREAK"
                    sess["started_at"] = now_iso
                    break_secs = break_m * 60
                    deadline = now_dt + timedelta(seconds=break_secs)
                    sess["ends_at"] = deadline.isoformat()
                    sess["total_seconds"] = break_secs
                    st["session"] = sess
                    st["current_session"] = sess
                    st["version"] = st.get("version", 0) + 1
                    st["updated_at"] = now_iso
                    next_deadline_dt = deadline
                    logger.info(f"[FocusService] Pomodoro WORK ended -> Started BREAK ({break_m}m, deadline={deadline})")

                elif current_phase == "BREAK":
                    # Transition back to WORK
                    sess["phase"] = "WORK"
                    sess["cycle_count"] = int(sess.get("cycle_count", 1)) + 1
                    sess["started_at"] = now_iso
                    work_secs = work_m * 60
                    deadline = now_dt + timedelta(seconds=work_secs)
                    sess["ends_at"] = deadline.isoformat()
                    sess["total_seconds"] = work_secs
                    st["session"] = sess
                    st["current_session"] = sess
                    st["version"] = st.get("version", 0) + 1
                    st["updated_at"] = now_iso
                    next_deadline_dt = deadline
                    logger.info(f"[FocusService] Pomodoro BREAK ended -> Started WORK (Cycle {sess['cycle_count']}, deadline={deadline})")

        committed_state = await self.storage.mutate(_deadline_mutator)
        self._notify_deadline_scheduler(next_deadline_dt)
        self._publish_mqtt(committed_state)
        return self.get_public_status()

    def reconcile_downtime_sync(self, state: Dict[str, Any], now_dt: Optional[datetime] = None) -> Tuple[Dict[str, Any], bool, Optional[datetime]]:
        """
        Pure mathematical downtime projection across missed deadlines.
        Returns (reconciled_state, did_change, next_deadline_dt).
        """
        if now_dt is None:
            now_dt = datetime.now(timezone.utc)

        if not state.get("enabled"):
            return state, False, None

        sess = state.get("session") or state.get("current_session")
        if not sess or sess.get("status") != "active":
            return state, False, None

        sess_type = sess.get("type", "focus")
        ends_at_str = sess.get("ends_at")

        if not ends_at_str:
            # Indefinite regular session
            return state, False, None

        try:
            ends_at_dt = parse_iso_utc(ends_at_str)
        except Exception:
            # Corrupted timestamp -> preserve as indefinite
            sess["ends_at"] = None
            return state, True, None

        if now_dt < ends_at_dt:
            # Server restarted before active deadline elapsed
            return state, False, ends_at_dt

        # One or more deadlines elapsed during downtime!
        did_change = True
        next_deadline_dt: Optional[datetime] = None

        if sess_type == "focus":
            # Timed regular focus expired during downtime -> EXPIRED_LOCKED
            sess["phase"] = "EXPIRED_LOCKED"
            sess["remaining_seconds"] = 0
            state["session"] = sess
            state["current_session"] = sess
            next_deadline_dt = None

        elif sess_type == "pomodoro":
            Tw = max(1, int(sess.get("work_duration_minutes", 25))) * 60
            Tb = max(1, int(sess.get("break_duration_minutes", 5))) * 60
            Tcycle = Tw + Tb

            delta_t = (now_dt - ends_at_dt).total_seconds()
            current_phase = sess.get("phase", "WORK")
            cycle_count = int(sess.get("cycle_count", 1))

            if current_phase == "WORK":
                if delta_t < Tb:
                    new_phase = "BREAK"
                    phase_start = ends_at_dt
                    phase_deadline = ends_at_dt + timedelta(seconds=Tb)
                else:
                    rem = delta_t - Tb
                    completed_cycles = 1 + int(rem // Tcycle)
                    cycle_count += completed_cycles
                    r = rem % Tcycle
                    if r < Tw:
                        new_phase = "WORK"
                        phase_start = now_dt - timedelta(seconds=r)
                        phase_deadline = phase_start + timedelta(seconds=Tw)
                    else:
                        new_phase = "BREAK"
                        phase_start = now_dt - timedelta(seconds=(r - Tw))
                        phase_deadline = phase_start + timedelta(seconds=Tb)
            else:  # current_phase == "BREAK"
                completed_cycles = 1 + int(delta_t // Tcycle)
                cycle_count += completed_cycles
                r = delta_t % Tcycle
                if r < Tw:
                    new_phase = "WORK"
                    phase_start = now_dt - timedelta(seconds=r)
                    phase_deadline = phase_start + timedelta(seconds=Tw)
                else:
                    new_phase = "BREAK"
                    phase_start = now_dt - timedelta(seconds=(r - Tw))
                    phase_deadline = phase_start + timedelta(seconds=Tb)

            sess["phase"] = new_phase
            sess["cycle_count"] = cycle_count
            sess["started_at"] = phase_start.isoformat()
            sess["ends_at"] = phase_deadline.isoformat()
            sess["total_seconds"] = Tw if new_phase == "WORK" else Tb
            next_deadline_dt = phase_deadline
            state["session"] = sess
            state["current_session"] = sess

        return state, did_change, next_deadline_dt

    async def reconcile_downtime(self, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Synchronously reconciles server downtime on startup before scheduler starts.
        Increments version by exactly 1 upon commit if state changed.
        """
        if now_dt is None:
            now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        next_dt: Optional[datetime] = None

        def _reconcile_mutator(st: Dict[str, Any]) -> None:
            nonlocal next_dt
            reconciled, changed, next_deadline = self.reconcile_downtime_sync(st, now_dt)
            next_dt = next_deadline
            if changed:
                st["version"] = st.get("version", 0) + 1
                st["updated_at"] = now_iso

        committed_state = await self.storage.mutate(_reconcile_mutator)
        self._notify_deadline_scheduler(next_dt)
        self._publish_mqtt(committed_state)
        return self.get_public_status()

    async def set_language(self, language: str) -> Dict[str, Any]:
        if language not in ["en", "vi"]:
            raise ValueError("Language must be 'en' or 'vi'.")
        now_iso = get_current_utc_iso()

        def _lang_mutator(st: Dict[str, Any]) -> None:
            if st.get("language") != language:
                st["language"] = language
                st["version"] = st.get("version", 0) + 1
                st["updated_at"] = now_iso

        committed = await self.storage.mutate(_lang_mutator)
        self._publish_mqtt(committed)
        return self.get_public_status()

    async def request_challenge(self, language: Optional[str] = None, force_new: bool = False) -> Dict[str, Any]:
        state = self.storage.get_state()
        target_lang = language if language in ["en", "vi"] else state.get("language", "en")
        return await self.challenge.get_or_create_challenge(lang=target_lang, force_new=force_new)

    async def verify_challenge(self, challenge_id: str, answer: int) -> Dict[str, Any]:
        """
        Verifies math challenge. If correct, terminates active session across all devices.
        """
        res = await self.challenge.verify_challenge(challenge_id=challenge_id, submitted_answer=answer)
        if res.get("success") and res.get("correct"):
            # Session terminated -> cancel scheduler job & publish MQTT
            self._notify_deadline_scheduler(None)
            updated_state = self.storage.get_state()
            self._publish_mqtt(updated_state)
        return res

    async def classify_url(
        self,
        url: str,
        language: Optional[str] = None,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        state = self.storage.get_state()
        target_lang = language if language in ["en", "vi"] else state.get("language", "en")
        return await self.classifier.classify(raw_url=url, language=target_lang, title=title)

    async def pair_device(
        self,
        code: str,
        device_name: str,
        platform: str,
        client_ip: str
    ) -> Dict[str, Any]:
        return await self.auth.consume_pairing_code(
            code=code,
            device_name=device_name,
            platform=platform,
            client_ip=client_ip
        )

    async def register_authorized_device(
        self,
        device_name: str,
        platform: str = "desktop",
        scopes: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return await self.auth.register_authorized_device(
            device_name=device_name,
            platform=platform,
            scopes=scopes
        )

    def get_paired_devices(self, current_device_id: Optional[str] = None) -> List[Dict[str, Any]]:
        state = self.storage.get_state()
        devices = state.get("devices", {})
        result = []
        for dev_id, info in devices.items():
            if info.get("revoked", False):
                continue
            result.append({
                "device_id": dev_id,
                "device_name": info.get("device_name", "DeskFlow Device"),
                "platform": info.get("platform", "unknown"),
                "device_type": info.get("platform", "unknown"),
                "is_current": (dev_id == current_device_id) if current_device_id else False,
                "paired_at": info.get("paired_at"),
                "last_seen_at": info.get("last_seen_at")
            })
        return result

    async def unpair_device(self, target_device_id: str) -> bool:
        """Revoke a device token while retaining its revocation record for auditability."""
        return await self.auth.revoke_device(target_device_id)

    async def delete_device(self, target_device_id: str) -> bool:
        return await self.auth.delete_device(target_device_id)

    async def rename_device(self, target_device_id: str, new_name: str) -> bool:
        return await self.auth.rename_device(target_device_id, new_name)

    def get_history(self) -> Dict[str, Any]:
        state = self.storage.get_state()
        history = list(state.get("history", []))
        history.reverse()
        return {
            "success": True,
            "history": history,
            "current_session": state.get("session") or state.get("current_session")
        }
