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
            if not self._active_session:
                return {
                    "active": False,
                    "session_id": None,
                    "epoch": self._alarm_epoch,
                    "question": None,
                    "created_at": None,
                    "failed_attempts": 0,
                    "cooldown_until": 0.0,
                    "is_locked": False,
                }
            return self._active_session.to_public_dict()

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

            self._alarm_epoch = target_epoch
            self._active_session = None
            logger.info(f"[AlarmEngine] Math Alarm solved successfully (session={session_id}, epoch={target_epoch})")
            return {"success": True, "epoch": target_epoch, "message": "Alarm solved and silenced."}

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
