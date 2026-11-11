"""
DeskFlow Unified App - Control and Spotify REST API Endpoints
Provides endpoints for:
- Active alarm status, solution, cancellation, and emergency stop
- Timetable synchronization
- Spotify status, playback control, and volume
Strictly enforces server-side scopes ('control', 'spotify').
"""

import re
import asyncio
import logging
from aiohttp import web
from typing import Dict, Any, Optional

import config
import spotify_service
import timetable
from alarm_engine import alarm_engine, AlarmPublishError
from station_control import station_control
from .auth import FocusAuth, AuthError
from .api import error_response

logger = logging.getLogger("DeskFlow.AppAPI")

UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def require_scope(device: Dict[str, Any], required_scope: str) -> None:
    scopes = device.get("scopes", ["focus"])
    if required_scope not in scopes:
        logger.warning(f"[Auth] Scope rejection: device '{device.get('device_id')}' possesses {scopes}, but '{required_scope}' is required.")
        raise AuthError(f"Forbidden: scope '{required_scope}' required.", code="insufficient_scope", status=403)


class AppApiHandler:
    def __init__(self, auth: FocusAuth):
        self.auth = auth

    async def _authenticate(self, request: web.Request, required_scope: Optional[str] = None) -> Dict[str, Any]:
        auth_header = request.headers.get("Authorization")
        device = await self.auth.authenticate_token(auth_header)
        if required_scope:
            require_scope(device, required_scope)
        return device

    async def _parse_json(self, request: web.Request) -> Dict[str, Any]:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Body must be a JSON object.")
            return body
        except web.HTTPException:
            raise
        except Exception:
            raise ValueError("Malformed or invalid JSON body.")

    # --- Health ---
    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "healthy",
            "service": "deskflow-unified-backend",
            "version": "1.0.0"
        })

    # --- Control / Alarm ---
    async def handle_alarm_active(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        active_info = await alarm_engine.get_active_session_public()
        return web.json_response(active_info)

    async def handle_alarm_solve(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)

        session_id = data.get("session_id")
        answer = data.get("answer")

        if not session_id or not isinstance(session_id, str):
            return error_response("invalid_session_id", status=400)

        # Strict integer check: in Python, isinstance(True, int) is True, so explicitly disallow bool!
        if answer is None or isinstance(answer, bool) or not isinstance(answer, int):
            return error_response("answer_must_be_integer", status=400)

        if not (-1000000 <= answer <= 1000000):
            return error_response("answer_out_of_range", status=400)

        try:
            res = await alarm_engine.solve_alarm(session_id=session_id.strip(), answer=answer)
            return web.json_response(res)
        except ValueError as ve:
            err_code = str(ve)
            if err_code == "stale_session":
                return error_response("stale_session", status=409)
            elif err_code == "incorrect_answer":
                return error_response("incorrect_answer", status=400)
            return error_response("invalid_request", status=400)
        except TimeoutError:
            return error_response("cooldown_active", status=429, retry_after=60)
        except PermissionError:
            return error_response("alarm_locked", status=403)
        except AlarmPublishError as ape:
            logger.error(f"[AppAPI] Alarm solve MQTT publish error: {ape}")
            return error_response("mqtt_publish_failed", status=503)

    async def handle_alarm_cancel(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        session_id = data.get("session_id")
        if not session_id or not isinstance(session_id, str):
            return error_response("invalid_session_id", status=400)

        try:
            res = await alarm_engine.cancel_alarm(session_id=session_id.strip())
            return web.json_response(res)
        except ValueError:
            return error_response("stale_session", status=409)
        except AlarmPublishError as ape:
            logger.error(f"[AppAPI] Alarm cancel MQTT publish error: {ape}")
            return error_response("mqtt_publish_failed", status=503)

    async def handle_alarm_emergency_stop(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            res = await alarm_engine.emergency_stop()
            return web.json_response(res)
        except AlarmPublishError as ape:
            logger.error(f"[AppAPI] Emergency stop MQTT publish error: {ape}")
            return error_response("mqtt_publish_failed", status=503)

    async def handle_timetable_today(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            # Fetch dual day timetable from PocketBase
            dual_tkb = await timetable.get_dual_day_tkb()
            return web.json_response({
                "success": True,
                "data": dual_tkb
            })
        except Exception as e:
            logger.error(f"[AppAPI] Failed to fetch timetable: {e}", exc_info=True)
            return web.json_response({
                "success": False,
                "error": "timetable_unavailable",
                "data": None
            }, status=503)

    # --- Station Status & Settings ---
    async def handle_control_status(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        status_data = station_control.get_status()
        return web.json_response({
            "success": True,
            "data": status_data
        })

    async def handle_control_settings_get(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        settings_data = station_control.get_settings()
        return web.json_response({
            "success": True,
            "data": settings_data
        })

    async def handle_control_settings_post(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = await self._parse_json(request)
        try:
            updated = station_control.update_settings(data)
            return web.json_response({
                "success": True,
                "data": updated
            })
        except ValueError as ve:
            return error_response(str(ve), status=400)
        except IOError as ie:
            return error_response(f"Persistence failure: {ie}", status=500)

    async def handle_control_theme_post(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = await self._parse_json(request)
        theme = data.get("theme") or data.get("mode") or data.get("darkmode_mode")
        if not theme:
            return error_response("missing_theme", status=400)
        try:
            updated = station_control.update_settings({
                "darkmode_mode": str(theme),
                "darkmode_start": data.get("start", "22:00"),
                "darkmode_end": data.get("end", "06:00")
            })
            return web.json_response({
                "success": True,
                "theme": theme,
                "data": updated
            })
        except ValueError as ve:
            return error_response(str(ve), status=400)

    # --- Display & View ---
    async def handle_display_view(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = await self._parse_json(request)
        view = data.get("view")
        if not view or not isinstance(view, str):
            return error_response("invalid_view", status=400)
        try:
            res = station_control.switch_view(view.strip().lower())
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    async def handle_display_close_tkb(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        res = station_control.close_tkb()
        return web.json_response(res)

    # --- Lighting & LEDs ---
    async def handle_lamp_color(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        hex_color = data.get("hex")
        if not hex_color or not isinstance(hex_color, str):
            return error_response("invalid_hex_color", status=400)
        try:
            res = station_control.set_lamp_color(hex_color)
            return web.json_response(res)
        except ValueError:
            return error_response("invalid_hex_color", status=400)

    async def handle_lamp_dim(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        led = data.get("led")
        screen = data.get("screen")
        if led is not None and (not isinstance(led, int) or isinstance(led, bool) or not (0 <= led <= 100)):
            return error_response("led_brightness_out_of_range", status=400)
        if screen is not None and (not isinstance(screen, int) or isinstance(screen, bool) or not (0 <= screen <= 100)):
            return error_response("screen_brightness_out_of_range", status=400)
        res = station_control.set_lamp_dim(led=led, screen=screen)
        return web.json_response(res)

    async def handle_lamp_off(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        res = station_control.set_lamp_off()
        return web.json_response(res)

    async def handle_led(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        act = data.get("action")
        if act not in ("toggle", "blink"):
            return error_response("invalid_led_action", status=400)
        res = station_control.set_led(act)
        return web.json_response(res)

    async def handle_clock_style_get(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        cfg = station_control.get_clock_config()
        return web.json_response({
            "success": True,
            "data": cfg
        })

    async def handle_clock_style(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = await self._parse_json(request)
        try:
            res = station_control.set_clock_config(data)
            return web.json_response({
                "success": True,
                "data": res
            })
        except ValueError as e:
            return error_response("invalid_clock_config", details=str(e), status=400)

    # --- Buzzer & Alarm Schedule ---
    async def handle_alarm_buzz(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except Exception:
                data = {}
        duration = data.get("duration", 250)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 10 or duration > 5000:
            return error_response("invalid_buzz_duration", status=400)
        res = await station_control.trigger_buzz(duration_ms=duration)
        return web.json_response(res)

    async def handle_morse_play(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        pattern = data.get("pattern", "")
        if not isinstance(pattern, str) or not pattern.strip():
            return error_response("invalid_morse_pattern", status=400)
        unit_ms = data.get("unit_ms", 80)
        if not isinstance(unit_ms, int) or isinstance(unit_ms, bool) or unit_ms < 20 or unit_ms > 500:
            unit_ms = 80
        freq = data.get("freq", 2400)
        if not isinstance(freq, int) or isinstance(freq, bool) or freq < 400 or freq > 4000:
            freq = 2400
        res = await station_control.play_morse(pattern=pattern.strip(), unit_ms=unit_ms, freq=freq)
        return web.json_response(res)

    async def handle_morse_stop(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        res = await station_control.stop_morse()
        return web.json_response(res)

    async def handle_melody_presets(self, request: web.Request) -> web.Response:
        from midi_parser import get_preset_list
        return web.json_response({"success": True, "presets": get_preset_list()})

    async def handle_melody_play(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        title = data.get("title", "Custom Melody")
        notes = data.get("notes")
        preset_id = data.get("preset_id")
        midi_b64 = data.get("midi_base64")

        if preset_id:
            from midi_parser import get_preset
            p = get_preset(str(preset_id))
            if not p:
                return error_response("preset_not_found", status=404)
            title = p["title"]
            notes = p["notes"]
        elif midi_b64:
            import base64
            from midi_parser import parse_midi_bytes
            try:
                midi_bytes = base64.b64decode(midi_b64)
                notes = parse_midi_bytes(midi_bytes)
                if not title or title == "Custom Melody":
                    title = data.get("filename", "MIDI Melody")
            except Exception as e:
                return error_response(f"invalid_midi: {str(e)}", status=400)
        elif not notes or not isinstance(notes, list):
            return error_response("missing_notes_or_preset", status=400)

        res = await station_control.play_melody(notes=notes, title=title)
        return web.json_response(res)

    async def handle_melody_stop(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        res = await station_control.stop_melody()
        return web.json_response(res)

    async def handle_melody_upload(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from midi_parser import parse_midi_bytes
        try:
            if request.content_type.startswith("multipart/"):
                reader = await request.multipart()
                field = await reader.next()
                if not field:
                    return error_response("no_file_uploaded", status=400)
                filename = field.filename or "Uploaded.mid"
                content = await field.read()
            else:
                content = await request.read()
                filename = request.headers.get("X-Filename", "Uploaded.mid")

            notes = parse_midi_bytes(content)
            title = filename.rsplit(".", 1)[0]
            res = await station_control.play_melody(notes=notes, title=title)
            res["filename"] = filename
            res["note_count"] = len(notes)
            return web.json_response(res)
        except Exception as e:
            return error_response(f"midi_parse_error: {str(e)}", status=400)

    async def handle_alarm_schedule(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        time_str = data.get("time")
        if not time_str or not isinstance(time_str, str) or not re.match(r"^\d{1,2}:\d{2}$", time_str.strip()):
            return error_response("invalid_time_format_hh_mm", status=400)
        try:
            res = await station_control.schedule_alarm(time_str.strip())
            return web.json_response(res)
        except Exception as e:
            logger.error(f"[AppAPI] Schedule alarm error: {e}")
            return error_response("failed_to_schedule_alarm", status=500)

    # --- Persistent Alarms Management ---
    async def handle_alarms_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        alarms = station_control.get_alarms()
        return web.json_response({
            "success": True,
            "alarms": alarms
        })

    async def handle_alarms_post(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        alarm = station_control.save_alarm(data)
        return web.json_response({
            "success": True,
            "alarm": alarm
        })

    async def handle_alarms_put(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        alarm_id = request.match_info.get("id") or data.get("id")
        data["id"] = alarm_id
        alarm = station_control.save_alarm(data)
        return web.json_response({
            "success": True,
            "alarm": alarm
        })

    async def handle_alarms_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        alarm_id = request.match_info.get("id")
        if not alarm_id:
            return error_response("missing_alarm_id", status=400)
        deleted = station_control.delete_alarm(alarm_id)
        return web.json_response({
            "success": True,
            "deleted": deleted
        })

    async def handle_alarms_toggle(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        alarm_id = request.match_info.get("id")
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except Exception:
                data = {}
        enabled = data.get("enabled")
        alarm = station_control.toggle_alarm(alarm_id, enabled)
        if not alarm:
            return error_response("alarm_not_found", status=404)
        return web.json_response({
            "success": True,
            "alarm": alarm
        })

    # --- Display Device Selection ---
    async def handle_get_selected_device(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        return web.json_response(station_control.get_selected_display_device())

    async def handle_set_selected_device(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = await self._parse_json(request)
        device_id = data.get("device_id")
        return web.json_response(station_control.set_selected_display_device(device_id))

    # --- Device Custom Renaming ---
    async def handle_control_device_rename(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = await self._parse_json(request)
        target_id = request.match_info.get("device_id") or data.get("device_id")
        new_name = str(data.get("name") or data.get("device_name") or "").strip()
        if not target_id or not new_name:
            return error_response("missing_target_or_name", status=400)
        station_control.rename_device(target_id, new_name)
        try:
            from focus_mode.service import focus_service
            await focus_service.rename_device(target_id, new_name)
        except Exception:
            pass
        return web.json_response({
            "success": True,
            "device_id": target_id,
            "device_name": new_name
        })

    # --- Strict 1-Second Telemetry Ingestion & In-Memory Status ---
    async def handle_telemetry_post(self, request: web.Request) -> web.Response:
        device = None
        try:
            device = await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = await self._parse_json(request)
        if device:
            device_id = device.get("device_id", "unknown")
            device_name = device.get("device_name", "Unknown Device")
        else:
            device_id = data.get("device_id") or "unpaired_computer"
            device_name = data.get("device_name") or "Desktop Computer"
        res = station_control.report_device_telemetry(device_id, device_name, data)
        return web.json_response(res)

    async def handle_telemetry_get(self, request: web.Request) -> web.Response:
        return web.json_response(station_control.get_active_telemetry())

    # --- Persistent Notes CRUD & Station Pinning ---
    async def handle_notes_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        notes = station_control.get_notes()
        return web.json_response({
            "success": True,
            "data": notes,
            "notes": notes,
            "active_station_note_id": station_control.active_note_id
        })

    async def handle_notes_post(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        content = data.get("content") or data.get("text") or data.get("note")
        title = data.get("title")
        if not content or not isinstance(content, str):
            return error_response("content_required", status=400)
        try:
            note = station_control.add_note(content=content, title=title)
            # If pin_to_station requested, pin it immediately
            if data.get("pin_to_station"):
                note = station_control.pin_note_to_station(note["id"])
            notes = station_control.get_notes()
            return web.json_response({
                "success": True,
                "data": note,
                "note": note,
                "notes": notes,
                "active_station_note_id": station_control.active_note_id
            })
        except ValueError as e:
            return error_response(str(e), status=400)

    async def handle_notes_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        note_id = request.match_info.get("id")
        if not note_id:
            return error_response("id_required", status=400)
        ok = station_control.delete_note(note_id)
        if ok:
            notes = station_control.get_notes()
            return web.json_response({
                "success": True,
                "message": "Note deleted",
                "notes": notes,
                "data": notes,
                "active_station_note_id": station_control.active_note_id
            })
        return error_response("note_not_found", status=404)

    async def handle_notes_pin(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        note_id = request.match_info.get("id")
        if not note_id:
            return error_response("id_required", status=400)
        try:
            note = station_control.pin_note_to_station(note_id)
            notes = station_control.get_notes()
            return web.json_response({
                "success": True,
                "data": note,
                "note": note,
                "notes": notes,
                "active_station_note_id": station_control.active_note_id
            })
        except ValueError as e:
            return error_response(str(e), status=404)

    async def handle_notes_unpin(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        station_control.unpin_sticky_note()
        notes = station_control.get_notes()
        return web.json_response({
            "success": True,
            "message": "Sticky note unpinned",
            "notes": notes,
            "data": notes,
            "active_station_note_id": None
        })

    # --- Persistent Countdowns CRUD & Station Pinning ---
    async def handle_countdowns_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        cds = station_control.get_countdowns()
        return web.json_response({
            "success": True,
            "data": cds,
            "countdowns": cds,
            "active_station_countdown_id": station_control.active_countdown_id
        })

    async def handle_countdowns_post(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        title = data.get("title") or data.get("name")
        target_date = data.get("target_date") or data.get("date")
        if not title or not isinstance(title, str):
            return error_response("title_required", status=400)
        if not target_date or not isinstance(target_date, str):
            return error_response("target_date_required", status=400)
        try:
            cd = station_control.add_countdown(title=title, target_date_str=target_date)
            if data.get("pin_to_station"):
                cd = station_control.set_active_countdown_on_station(cd["id"])
            cds = station_control.get_countdowns()
            return web.json_response({
                "success": True,
                "data": cd,
                "countdown": cd,
                "countdowns": cds,
                "active_station_countdown_id": station_control.active_countdown_id
            })
        except ValueError as e:
            return error_response(str(e), status=400)

    async def handle_countdowns_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        cid = request.match_info.get("id")
        if not cid:
            return error_response("id_required", status=400)
        ok = station_control.delete_countdown(cid)
        if ok:
            cds = station_control.get_countdowns()
            return web.json_response({
                "success": True,
                "message": "Countdown deleted",
                "countdowns": cds,
                "data": cds,
                "active_station_countdown_id": station_control.active_countdown_id
            })
        return error_response("countdown_not_found", status=404)

    async def handle_countdowns_pin(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        cid = request.match_info.get("id")
        if not cid:
            return error_response("id_required", status=400)
        try:
            cd = station_control.set_active_countdown_on_station(cid)
            cds = station_control.get_countdowns()
            return web.json_response({
                "success": True,
                "data": cd,
                "countdown": cd,
                "countdowns": cds,
                "active_station_countdown_id": station_control.active_countdown_id
            })
        except ValueError as e:
            return error_response(str(e), status=404)

    # --- Weather Forecast Details ---
    async def handle_weather_forecast(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        city = request.query.get("city")
        try:
            import scheduler
            res = await scheduler.get_weather_forecast(city=city)
            return web.json_response({
                "success": True,
                "city": res.get("city", ""),
                "available": res.get("available", False),
                "intervals": res.get("intervals", []),
                "data": res
            })
        except Exception as e:
            return error_response(f"forecast_error: {e}", status=500)

    # --- Backward-compatible endpoints ---
    async def handle_sticky_note(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        note = data.get("note")
        if not note or not isinstance(note, str):
            return error_response("invalid_note", status=400)
        res = station_control.set_sticky_note(note.strip())
        return web.json_response(res)

    async def handle_exam_countdown(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        name = data.get("name")
        date_str = data.get("date")
        if not name or not isinstance(name, str) or not date_str or not isinstance(date_str, str):
            return error_response("invalid_exam_data", status=400)
        try:
            res = station_control.set_exam_countdown(name=name.strip(), date_str=date_str.strip())
            return web.json_response(res)
        except Exception as e:
            return error_response(f"exam_countdown_error: {e}", status=400)

    # --- Groq Quote & Pomodoro Timer ---
    async def handle_quote_generate(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except Exception:
                data = {}
        topic = data.get("topic", "học tập và kiên trì")
        limit = data.get("limit", 75)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 10 or limit > 200:
            limit = 75
        res = await asyncio.to_thread(station_control.generate_groq_quote, topic=topic, max_chars=limit)
        return web.json_response(res)

    async def handle_timer_start(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        duration = data.get("duration", "25m")
        note = data.get("note", "Tập trung học")
        if not duration or not isinstance(duration, str):
            return error_response("invalid_duration", status=400)
        try:
            res = await asyncio.to_thread(station_control.start_pomodoro_timer, duration=duration, note=note)
            return web.json_response(res)
        except Exception as e:
            return error_response(f"timer_error: {e}", status=400)

    # --- Spotify ---
    async def handle_spotify_status(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        try:
            # Offload synchronous spotipy call to worker thread
            status = await asyncio.to_thread(spotify_service.get_current_playing, False)
            if status is None:
                return web.json_response({
                    "is_playing": False,
                    "connected": False,
                    "track": "Spotify Offline",
                    "artist": "N/A"
                })
            res = dict(status)
            res["connected"] = True
            return web.json_response(res)
        except Exception as e:
            logger.error(f"[AppAPI] Error fetching Spotify status: {e}")
            return error_response("spotify_unavailable", status=503)

    async def handle_spotify_playback(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        data = await self._parse_json(request)
        command = data.get("command")
        if not command or not isinstance(command, str):
            return error_response("invalid_command", status=400)

        cmd = command.strip().lower()
        try:
            if cmd in ("play", "pause", "toggle"):
                await asyncio.to_thread(spotify_service.play_pause_toggle)
            elif cmd == "next":
                await asyncio.to_thread(spotify_service.next_track)
            elif cmd in ("prev", "previous"):
                await asyncio.to_thread(spotify_service.previous_track)
            else:
                return error_response("unsupported_playback_command", status=400)

            # Return updated status immediately
            updated_status = await asyncio.to_thread(spotify_service.get_current_playing, True)
            return web.json_response({
                "success": True,
                "command": cmd,
                "status": updated_status
            })
        except Exception as e:
            logger.error(f"[AppAPI] Spotify playback command failed: {e}")
            return error_response("playback_command_failed", status=500)

    async def handle_spotify_volume(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        data = await self._parse_json(request)
        vol = data.get("volume")

        # Strict integer check
        if vol is None or isinstance(vol, bool) or not isinstance(vol, int):
            return error_response("volume_must_be_integer_0_100", status=400)

        if not (0 <= vol <= 100):
            return error_response("volume_must_be_integer_0_100", status=400)

        try:
            await asyncio.to_thread(spotify_service.set_playback_volume, vol)
            return web.json_response({
                "success": True,
                "volume": vol
            })
        except Exception as e:
            logger.error(f"[AppAPI] Failed to set volume: {e}")
            return error_response("volume_change_failed", status=500)

    # --- Local Admin (Loopback Only) ---
    async def handle_admin_generate_pairing_code(self, request: web.Request) -> web.Response:
        # Strictly loopback only (reject if routed via Caddy proxy)
        if request.headers.get("X-Real-IP") is not None or request.headers.get("X-Forwarded-For") is not None:
            return error_response("forbidden", status=403)
        if request.remote not in ("127.0.0.1", "::1"):
            return error_response("forbidden", status=403)

        # Gated with server token
        auth_header = request.headers.get("Authorization", "")
        expected = f"Bearer {getattr(config, 'DISCORD_TOKEN', '')}"
        if not auth_header or auth_header != expected:
            logger.warning(f"[AppAPI Admin] Auth mismatch: got {auth_header[:20]!r} expected {expected[:20]!r}")
            return error_response("unauthorized", status=401)

        try:
            data = await self._parse_json(request)
        except Exception:
            data = {}
        scopes = data.get("scopes", ["focus", "control", "spotify"])
        raw_code, expires_at = await self.auth.generate_pairing_code(scopes=scopes)
        return web.json_response({
            "success": True,
            "pairing_code": raw_code,
            "expires_at": expires_at,
            "scopes": scopes
        })


def register_app_routes(app: web.Application, auth: FocusAuth) -> None:
    handler = AppApiHandler(auth=auth)

    # Health
    app.router.add_get("/api/app/health", handler.handle_health)

    # Local Admin (Loopback only)
    app.router.add_post("/api/app/admin/pairing/generate", handler.handle_admin_generate_pairing_code)

    # Control / Alarm & Buzz
    app.router.add_get("/api/app/control/alarm/active", handler.handle_alarm_active)
    app.router.add_post("/api/app/control/alarm/solve", handler.handle_alarm_solve)
    app.router.add_post("/api/app/control/alarm/cancel", handler.handle_alarm_cancel)
    app.router.add_post("/api/app/control/alarm/emergency-stop", handler.handle_alarm_emergency_stop)
    app.router.add_post("/api/app/control/alarm/buzz", handler.handle_alarm_buzz)
    app.router.add_post("/api/app/control/alarm/schedule", handler.handle_alarm_schedule)
    app.router.add_post("/api/app/control/morse/play", handler.handle_morse_play)
    app.router.add_post("/api/app/control/morse/stop", handler.handle_morse_stop)
    app.router.add_get("/api/app/control/melody/presets", handler.handle_melody_presets)
    app.router.add_post("/api/app/control/melody/play", handler.handle_melody_play)
    app.router.add_post("/api/app/control/melody/upload", handler.handle_melody_upload)
    app.router.add_post("/api/app/control/melody/stop", handler.handle_melody_stop)

    # Persistent Alarms
    app.router.add_get("/api/app/control/alarms", handler.handle_alarms_get)
    app.router.add_post("/api/app/control/alarms", handler.handle_alarms_post)
    app.router.add_put("/api/app/control/alarms/{id}", handler.handle_alarms_put)
    app.router.add_delete("/api/app/control/alarms/{id}", handler.handle_alarms_delete)
    app.router.add_post("/api/app/control/alarms/{id}/toggle", handler.handle_alarms_toggle)

    # Dynamic Display Device Selection & 1-Second Telemetry
    app.router.add_get("/api/app/control/device/selected", handler.handle_get_selected_device)
    app.router.add_post("/api/app/control/device/selected", handler.handle_set_selected_device)
    app.router.add_post("/api/app/control/device/rename", handler.handle_control_device_rename)
    app.router.add_post("/api/app/control/device/{device_id}/rename", handler.handle_control_device_rename)
    app.router.add_post("/api/app/control/telemetry", handler.handle_telemetry_post)
    app.router.add_get("/api/app/control/telemetry", handler.handle_telemetry_get)

    # Control / Station Status & Settings
    app.router.add_get("/api/app/control/status", handler.handle_control_status)
    app.router.add_get("/api/app/control/settings", handler.handle_control_settings_get)
    app.router.add_post("/api/app/control/settings", handler.handle_control_settings_post)
    app.router.add_post("/api/app/control/theme", handler.handle_control_theme_post)

    # Control / Display & Screen
    app.router.add_post("/api/app/control/display/view", handler.handle_display_view)
    app.router.add_post("/api/app/control/display/close-tkb", handler.handle_display_close_tkb)

    # Control / Lighting & LEDs
    app.router.add_post("/api/app/control/lamp/color", handler.handle_lamp_color)
    app.router.add_post("/api/app/control/lamp/dim", handler.handle_lamp_dim)
    app.router.add_post("/api/app/control/lamp/off", handler.handle_lamp_off)
    app.router.add_post("/api/app/control/led", handler.handle_led)
    app.router.add_get("/api/app/control/clock-style", handler.handle_clock_style_get)
    app.router.add_post("/api/app/control/clock-style", handler.handle_clock_style)

    # Control / Tools (Sticky note, Exam, Quote, Timer, Weather Forecast)
    app.router.add_get("/api/app/control/notes", handler.handle_notes_get)
    app.router.add_post("/api/app/control/notes", handler.handle_notes_post)
    app.router.add_delete("/api/app/control/notes/{id}", handler.handle_notes_delete)
    app.router.add_post("/api/app/control/notes/{id}/pin", handler.handle_notes_pin)
    app.router.add_post("/api/app/control/notes/unpin", handler.handle_notes_unpin)

    app.router.add_get("/api/app/control/countdowns", handler.handle_countdowns_get)
    app.router.add_post("/api/app/control/countdowns", handler.handle_countdowns_post)
    app.router.add_delete("/api/app/control/countdowns/{id}", handler.handle_countdowns_delete)
    app.router.add_post("/api/app/control/countdowns/{id}/pin", handler.handle_countdowns_pin)

    app.router.add_get("/api/app/control/weather/forecast", handler.handle_weather_forecast)
    app.router.add_post("/api/app/control/sticky-note", handler.handle_sticky_note)
    app.router.add_post("/api/app/control/exam-countdown", handler.handle_exam_countdown)
    app.router.add_post("/api/app/control/quote/generate", handler.handle_quote_generate)
    app.router.add_post("/api/app/control/quote", handler.handle_quote_generate)
    app.router.add_post("/api/app/control/timer/start", handler.handle_timer_start)

    # Timetable
    app.router.add_get("/api/app/control/timetable/today", handler.handle_timetable_today)

    # Spotify
    app.router.add_get("/api/app/spotify/status", handler.handle_spotify_status)
    app.router.add_post("/api/app/spotify/playback", handler.handle_spotify_playback)
    app.router.add_post("/api/app/spotify/volume", handler.handle_spotify_volume)

    # Also register CORS options preflight for /api/app/*
    async def options_app_handler(request: web.Request) -> web.Response:
        return web.Response(status=204)
    app.router.add_route("OPTIONS", "/api/app/{tail:.*}", options_app_handler)

    logger.info("[AppAPI] Unified App routes registered on aiohttp web app.")
