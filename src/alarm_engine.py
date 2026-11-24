"""
DeskFlow - Centralized Alarm Engine
Manages alarm state, monotonic epochs, UUIDv4 session tracking,
anti-brute-force rate limiting, and synchronized MQTT publishing.
"""

import time
import json
import uuid
import asyncio
import logging
import paho.mqtt.client as mqtt
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

logger = logging.getLogger("DeskFlow.AlarmEngine")


class AlarmPublishError(Exception):
    def __init__(self, message: str, rc: Optional[int] = None):
        super().__init__(message)
        self.rc = rc


@dataclass
class AlarmSession:
    session_id: str
    epoch: int
    question: str
    solution: int
    created_at: float
    failed_attempts: int = 0
    cooldown_until: float = 0.0
    is_locked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "epoch": self.epoch,
            "question": self.question,
            "solution": self.solution,
            "created_at": self.created_at,
            "failed_attempts": self.failed_attempts,
            "cooldown_until": self.cooldown_until,
            "is_locked": self.is_locked,
        }

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "active": True,
            "session_id": self.session_id,
            "epoch": self.epoch,
            "question": self.question,
            "created_at": self.created_at,
            "failed_attempts": self.failed_attempts,
            "cooldown_until": self.cooldown_until,
            "is_locked": self.is_locked,
        }


class AlarmEngine:
    def __init__(self, mqtt_client: Optional[mqtt.Client] = None):
        self.mqtt_client = mqtt_client
        self._lock = asyncio.Lock()
        self._alarm_epoch: int = 0
        self._active_session: Optional[AlarmSession] = None
        # Anti-oversleep (Wake-up verification) state
        self.anti_oversleep_enabled: bool = True
        self.anti_oversleep_delay_sec: int = 300
        self._wakeup_state: str = "idle"  # idle | waiting | prompt
        self._wakeup_check_at: float = 0.0
        self._wakeup_deadline: float = 0.0
        self._wakeup_question: str = ""
        self._wakeup_solution: int = 0
        self._notify_hook = None

    def set_notify_hook(self, hook) -> None:
        self._notify_hook = hook

    def set_anti_oversleep(self, enabled: Optional[bool] = None, delay_sec: Optional[int] = None) -> None:
        if enabled is not None:
            self.anti_oversleep_enabled = bool(enabled)
        if delay_sec is not None:
            self.anti_oversleep_delay_sec = max(30, min(1800, int(delay_sec)))

    def _emit_notify(self, kind: str, title: str, body: str, priority: str = "high") -> None:
        if self._notify_hook:
            try:
                self._notify_hook(kind, title, body, priority)
            except Exception as e:
                logger.warning(f"[AlarmEngine] Notify hook failed: {e}")

    def set_mqtt_client(self, client: mqtt.Client) -> None:
        self.mqtt_client = client

    def _publish_alarm_mqtt(self, payload: dict) -> None:
        """
        Publishes an alarm command to MQTT with connection and return code validation.
        MUST be called while holding self._lock.
        """
        if not self.mqtt_client:
            logger.warning("[AlarmEngine] MQTT client is not configured; simulated publish.")
            return

        if not self.mqtt_client.is_connected():
            logger.error("[AlarmEngine] MQTT client is NOT connected to broker. Hardware publish dropped!")
            raise AlarmPublishError("MQTT client disconnected from broker", rc=mqtt.MQTT_ERR_NO_CONN)

        payload_str = json.dumps(payload, ensure_ascii=False)
        msg_info = self.mqtt_client.publish("deskflow/alarm", payload_str, qos=1)

        if msg_info.rc != mqtt.MQTT_ERR_SUCCESS:
            err_str = mqtt.error_string(msg_info.rc)
            logger.error(f"[AlarmEngine] MQTT publish failed with rc={msg_info.rc} ({err_str})")
            raise AlarmPublishError(f"MQTT publish failed: {err_str}", rc=msg_info.rc)

        logger.info(f"[AlarmEngine] MQTT command '{payload.get('action')}' successfully enqueued (epoch={self._alarm_epoch}).")

    async def get_active_session_public(self) -> Dict[str, Any]:
        async with self._lock:
            base = {
                "anti_oversleep_enabled": self.anti_oversleep_enabled,
                "anti_oversleep_delay_sec": self.anti_oversleep_delay_sec,
                "wakeup_state": self._wakeup_state,
                "wakeup_pending": self._wakeup_state == "prompt",
                "wakeup_waiting": self._wakeup_state == "waiting",
                "wakeup_check_at": self._wakeup_check_at if self._wakeup_state != "idle" else None,
                "wakeup_deadline": self._wakeup_deadline if self._wakeup_state == "prompt" else None,
            }
            if not self._active_session:
                return {
                    **base,
                    "active": False,
                    "session_id": None,
                    "epoch": self._alarm_epoch,
                    "question": None,
                    "created_at": None,
                    "failed_attempts": 0,
                    "cooldown_until": 0.0,
                    "is_locked": False,
                }
            res = self._active_session.to_public_dict()
            res.update(base)
            return res

    async def trigger_alarm(self, question: str, solution: int, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Triggers or replaces an active math alarm."""
        async with self._lock:
            sid = session_id or str(uuid.uuid4())
            target_epoch = self._alarm_epoch + 1

            new_session = AlarmSession(
                session_id=sid,
                epoch=target_epoch,
                question=question,
                solution=int(solution),
                created_at=time.time(),
            )

            payload = {
                "action": "math_alarm_trigger",
                "math_q": question,
                "session_id": sid,
                "epoch": target_epoch,
            }

            try:
                self._publish_alarm_mqtt(payload)
            except AlarmPublishError:
                logger.error("[AlarmEngine] Rollback trigger state due to MQTT publish failure.")
                raise

            self._alarm_epoch = target_epoch
            self._active_session = new_session
            self._wakeup_state = "idle"
            self._emit_notify(
                "alarm_ringing",
                "DeskFlow Báo thức đang reo!",
                f"Giải toán để tắt còi: {question}",
                "urgent",
            )
            logger.info(f"[AlarmEngine] Math Alarm triggered (session={sid}, epoch={target_epoch}, q='{question}')")
            return {"success": True, "session_id": sid, "epoch": target_epoch}

    async def solve_alarm(self, session_id: str, answer: int) -> Dict[str, Any]:
        """Validates solution and silences hardware if correct."""
        async with self._lock:
            if not self._active_session or self._active_session.session_id != session_id:
                raise ValueError("stale_session")

            sess = self._active_session
            if sess.is_locked:
                raise PermissionError("alarm_locked")
            if time.time() < sess.cooldown_until:
                raise TimeoutError("cooldown_active")

            if answer != sess.solution:
                sess.failed_attempts += 1
                if sess.failed_attempts >= 8:
                    sess.is_locked = True
                    raise PermissionError("alarm_locked")
                elif sess.failed_attempts >= 5:
                    sess.cooldown_until = time.time() + 60.0
                    raise TimeoutError("cooldown_active")
                raise ValueError("incorrect_answer")

            target_epoch = self._alarm_epoch + 1
            payload = {
                "action": "stop_alarm",
                "session_id": session_id,
                "epoch": target_epoch,
            }

            try:
                self._publish_alarm_mqtt(payload)
            except AlarmPublishError:
                logger.error("[AlarmEngine] Rollback solve state due to MQTT publish failure.")
                raise

            solved_q = sess.question
            solved_ans = sess.solution
            self._alarm_epoch = target_epoch
            self._active_session = None
            if self.anti_oversleep_enabled:
                self._wakeup_state = "waiting"
                self._wakeup_check_at = time.time() + self.anti_oversleep_delay_sec
                self._wakeup_question = solved_q
                self._wakeup_solution = solved_ans
            logger.info(f"[AlarmEngine] Math Alarm solved successfully (session={session_id}, epoch={target_epoch})")
            return {
                "success": True,
                "epoch": target_epoch,
                "wakeup_scheduled": self.anti_oversleep_enabled,
                "wakeup_check_in_sec": self.anti_oversleep_delay_sec if self.anti_oversleep_enabled else 0,
                "message": "Alarm solved and silenced.",
            }

    async def confirm_awake(self) -> Dict[str, Any]:
        """Confirms the user is actually awake, cancelling pending anti-oversleep re-trigger."""
        async with self._lock:
            was_state = self._wakeup_state
            self._wakeup_state = "idle"
            self._wakeup_check_at = 0.0
            self._wakeup_deadline = 0.0
            try:
                self._publish_alarm_mqtt({"action": "wakeup_confirmed"})
            except AlarmPublishError:
                pass
            logger.info(f"[AlarmEngine] Wake-up confirmed by user (previous state={was_state}).")
            return {"success": True, "previous_state": was_state}

    async def check_wakeup_tick(self) -> None:
        """Periodic check (~5s) driving anti-oversleep prompt & automatic alarm re-trigger."""
        now = time.time()
        should_retrigger = False
        retrigger_q = ""
        retrigger_sol = 0

        async with self._lock:
            if self._active_session is not None or self._wakeup_state == "idle":
                return
            if self._wakeup_state == "waiting" and now >= self._wakeup_check_at:
                self._wakeup_state = "prompt"
                self._wakeup_deadline = now + 45.0
                try:
                    self._publish_alarm_mqtt({
                        "action": "math_alarm_wakeup_check",
                        "timeout_sec": 45,
                    })
                except AlarmPublishError:
                    pass
                self._emit_notify(
                    "wakeup_check",
                    "Bạn còn thức chứ?",
                    "Bấm xác nhận trên ESP32, Web hoặc ĐT trong 45s nếu không báo thức sẽ reo lại!",
                    "urgent",
                )
                logger.info("[AlarmEngine] Anti-oversleep prompt sent (45s window).")
            elif self._wakeup_state == "prompt" and now >= self._wakeup_deadline:
                import random
                a = random.randint(12, 48)
                b = random.randint(13, 47)
                retrigger_q = f"{a} + {b} = ?"
                retrigger_sol = a + b
                self._wakeup_state = "idle"
                should_retrigger = True

        if should_retrigger:
            logger.warning("[AlarmEngine] User missed anti-oversleep confirmation! Re-triggering alarm.")
            try:
                await self.trigger_alarm(question=retrigger_q, solution=retrigger_sol)
            except Exception as e:
                logger.error(f"[AlarmEngine] Failed anti-oversleep re-trigger: {e}")

    async def cancel_alarm(self, session_id: str) -> Dict[str, Any]:
        """Cancels an active alarm session by ID."""
        async with self._lock:
            if not self._active_session or self._active_session.session_id != session_id:
                raise ValueError("stale_session")

            target_epoch = self._alarm_epoch + 1
            payload = {
                "action": "stop_alarm",
                "session_id": session_id,
                "epoch": target_epoch,
            }

            try:
                self._publish_alarm_mqtt(payload)
            except AlarmPublishError:
                logger.error("[AlarmEngine] Rollback cancel state due to MQTT publish failure.")
                raise

            self._alarm_epoch = target_epoch
            self._active_session = None
            logger.info(f"[AlarmEngine] Math Alarm cancelled (session={session_id}, epoch={target_epoch})")
            return {"success": True, "epoch": target_epoch, "message": "Alarm cancelled."}

    async def emergency_stop(self) -> Dict[str, Any]:
        """Unconditionally silences hardware and clears any active session."""
        async with self._lock:
            target_epoch = self._alarm_epoch + 1
            payload = {
                "action": "stop_alarm",
                "epoch": target_epoch,
                "emergency": True,
            }

            try:
                self._publish_alarm_mqtt(payload)
            except AlarmPublishError:
                logger.error("[AlarmEngine] Rollback emergency stop state due to MQTT publish failure.")
                raise

            self._alarm_epoch = target_epoch
            self._active_session = None
            self._wakeup_state = "idle"
            self._wakeup_check_at = 0.0
            self._wakeup_deadline = 0.0
            logger.info(f"[AlarmEngine] Emergency Stop executed (epoch={target_epoch})")
            return {"success": True, "epoch": target_epoch, "message": "Emergency stop signal sent."}

    async def trigger_buzz(self, duration: int = 250) -> Dict[str, Any]:
        """Triggers a short test buzz on the hardware buzzer."""
        async with self._lock:
            payload = {
                "action": "buzz",
                "duration": max(50, min(5000, int(duration))),
            }
            self._publish_alarm_mqtt(payload)
            return {"success": True, "duration": payload["duration"]}

    async def play_morse(self, pattern: str, unit_ms: int = 60, freq: int = 1000) -> Dict[str, Any]:
        """Sends a Morse code playback command to the ESP32 station."""
        async with self._lock:
            cleaned_pattern = pattern.strip()[:500]
            unit = max(20, min(500, int(unit_ms)))
            frequency = max(400, min(4000, int(freq)))
            payload = {
                "action": "morse_play",
                "pattern": cleaned_pattern,
                "unit_ms": unit,
                "freq": frequency,
            }
            self._publish_alarm_mqtt(payload)
            logger.info(f"[AlarmEngine] Morse playback dispatched: {len(cleaned_pattern)} symbols, unit={unit}ms")
            return {"success": True, "pattern": cleaned_pattern, "unit_ms": unit, "freq": frequency}

    async def play_melody(self, notes: Optional[List[List[int]]] = None, title: str = "Melody") -> Dict[str, Any]:
        """Asks the ESP32 to play a MIDI melody on the buzzer."""
        async with self._lock:
            payload: Dict[str, Any] = {"action": "melody_play", "title": title}
            if notes:
                payload["notes"] = notes
            self._publish_alarm_mqtt(payload)
            logger.info(f"[AlarmEngine] Melody play dispatched: '{title}' ({len(notes) if notes else 'builtin'} notes)")
            return {"success": True, "title": title, "note_count": len(notes) if notes else 0}

    async def stop_melody(self) -> Dict[str, Any]:
        """Stops active buzzer melody playback immediately."""
        async with self._lock:
            self._publish_alarm_mqtt({"action": "melody_stop"})
            logger.info("[AlarmEngine] Melody stop dispatched.")
            return {"success": True, "message": "Melody playback stopped."}

    async def stop_morse(self) -> Dict[str, Any]:
        """Stops any active Morse code playback immediately."""
        async with self._lock:
            payload = {
                "action": "morse_stop"
            }
            self._publish_alarm_mqtt(payload)
            logger.info("[AlarmEngine] Morse stop dispatched.")
            return {"success": True, "message": "Morse playback stopped."}


# Global singleton instance
alarm_engine = AlarmEngine()
