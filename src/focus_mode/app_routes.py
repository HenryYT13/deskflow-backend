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
from calendar_service import calendar_service, render_oauth_result_html
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
            "version": "2.4.1"
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
            dual_tkb = await timetable.get_dual_day_tkb()
            sched = await timetable.fetch_current_weekly_schedule()
            live = timetable.get_live_period_status((dual_tkb or {}).get("today"))
            stats = timetable.get_weekly_subject_stats((sched or {}).get("schedule_data") or {})
            return web.json_response({
                "success": True,
                "data": dual_tkb,
                "live": live,
                "stats": stats,
                "source": (sched or {}).get("source", "pocketbase"),
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
            view_key = view.strip().lower()
            tkb_payload = None
            if view_key == "tkb":
                # Fetch the schedule first so the station never shows an empty
                # TKB after switching (Windows "sync to ESP32" button).
                try:
                    dual_tkb = await timetable.get_dual_day_tkb()
                    if dual_tkb and dual_tkb.get("today"):
                        tkb_payload = timetable.build_station_payload(dual_tkb)
                except Exception as exc:
                    logger.warning(f"[AppAPI] TKB payload unavailable: {exc}")
            res = station_control.switch_view(view_key, tkb_payload=tkb_payload)
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    async def handle_display_close_tkb(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        res = station_control.close_tkb()
        return web.json_response(res)

    # --- Lighting & LEDs ---
    async def handle_lamp_color(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        hex_color = data.get("hex") or data.get("color")
        if not hex_color or not isinstance(hex_color, str):
            return error_response("invalid_hex_color", status=400)
        hex_color2 = data.get("hex2") or data.get("color2")
        gradient = data.get("gradient")
        try:
            res = station_control.set_lamp_color(hex_color, hex_color2=hex_color2, gradient=gradient)
            return web.json_response(res)
        except ValueError:
            return error_response("invalid_hex_color", status=400)

    async def handle_lamp_dim(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        raw_led = (
            data.get("led")
            if data.get("led") is not None
            else (
                data.get("led_bright")
                if data.get("led_bright") is not None
                else (
                    data.get("led_brightness")
                    if data.get("led_brightness") is not None
                    else data.get("brightness")
                )
            )
        )
        raw_screen = data.get("screen") if data.get("screen") is not None else (data.get("screen_bright") if data.get("screen_bright") is not None else data.get("screen_brightness"))

        led = None
        if raw_led is not None:
            try:
                led = int(round(float(raw_led)))
                if not (0 <= led <= 100):
                    return error_response("led_brightness_out_of_range", status=400)
            except (ValueError, TypeError):
                return error_response("invalid_led_brightness", status=400)

        screen = None
        if raw_screen is not None:
            try:
                screen = int(round(float(raw_screen)))
                if not (0 <= screen <= 100):
                    return error_response("screen_brightness_out_of_range", status=400)
            except (ValueError, TypeError):
                return error_response("invalid_screen_brightness", status=400)

        res = station_control.set_lamp_dim(led=led, screen=screen)
        return web.json_response(res)

    async def handle_lamp_off(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        res = station_control.set_lamp_off()
        return web.json_response(res)

    async def handle_led(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        act = data.get("action")
        if act not in ("on", "off", "toggle", "blink"):
            return error_response("invalid_led_action", status=400)
        res = station_control.set_led(act)
        return web.json_response(res)

    async def handle_lamp_effect(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        eff = str(data.get("effect") or "solid").strip().lower()
        speed = data.get("speed")
        color = data.get("color")
        rgb = data.get("rgb")
        color2 = data.get("color2") or data.get("hex2")
        gradient = data.get("gradient")
        try:
            res = station_control.set_lamp_effect(
                eff, speed=speed, color=color, rgb=rgb, color2=color2, gradient=gradient
            )
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    async def handle_lamp_ambient(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        enabled = bool(data.get("enabled", True))
        res = station_control.set_ambient_sync(enabled)
        return web.json_response(res)

    async def handle_alarm_confirm_awake(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except Exception:
                data = {}
        try:
            res = await alarm_engine.confirm_awake(captcha=data.get("captcha"))
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    async def handle_alarm_shake_dismiss(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except Exception:
                data = {}
        try:
            shakes = int(data.get("shakes", 20))
            res = await alarm_engine.shake_dismiss_active_alarm(shakes=shakes)
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    async def handle_alarm_anti_oversleep_config(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        alarm_engine.set_anti_oversleep(
            enabled=data.get("enabled"),
            delay_sec=data.get("delay_sec"),
            level=data.get("level"),
        )
        return web.json_response({
            "success": True,
            "enabled": alarm_engine.anti_oversleep_enabled,
            "delay_sec": alarm_engine.anti_oversleep_delay_sec,
            "level": alarm_engine.anti_oversleep_level,
        })

    async def handle_notifications_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            since = float(request.query.get("since", "0") or 0)
        except ValueError:
            since = 0.0
        items = station_control.get_notifications(since=since)
        return web.json_response({"success": True, "notifications": items})

    async def handle_notify_test(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        title = str(data.get("title") or "DeskFlow Test Alert")
        body = str(data.get("body") or "Thông báo đẩy về điện thoại hoạt động bình thường!")
        ev = station_control.push_notification("test", title, body, "high")
        return web.json_response({"success": True, "notification": ev})

    async def handle_ota_status(self, request: web.Request) -> web.Response:
        try:
            await self._authenticate(request, required_scope="control")
        except Exception:
            pass
        return web.json_response({"success": True, "data": station_control.get_ota_status()})

    async def handle_ota_upload(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        version = request.query.get("version") or "2.1.0"
        auto_flash = request.query.get("flash", "false").lower() in ("1", "true", "yes")
        filename = "firmware.bin"
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            content = b""
            async for field in reader:
                if field.name in ("file", "firmware"):
                    filename = field.filename or filename
                    content = await field.read()
                elif field.name == "version":
                    version = (await field.text()).strip() or version
                elif field.name == "flash":
                    auto_flash = (await field.text()).strip().lower() in ("1", "true", "yes")
        else:
            content = await request.read()
            filename = request.headers.get("X-Filename", filename)
        try:
            meta = station_control.save_ota_firmware(content, version=version, filename=filename)
            flash_res = None
            if auto_flash:
                flash_res = station_control.trigger_ota_flash()
            return web.json_response({"success": True, "ota": meta, "flash": flash_res})
        except ValueError as ve:
            return error_response(str(ve), status=400)

    async def handle_ota_firmware_download(self, request: web.Request) -> web.Response:
        bin_path = station_control.get_ota_firmware_path()
        if not bin_path:
            return web.Response(status=404, text="No OTA firmware uploaded")
        return web.FileResponse(bin_path, headers={"Content-Disposition": "attachment; filename=deskflow-esp32.bin"})

    async def handle_ota_flash(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            res = station_control.trigger_ota_flash()
            return web.json_response(res)
        except FileNotFoundError as fnf:
            return error_response(str(fnf), status=404)

    async def handle_station_ota_trigger(self, request: web.Request) -> web.Response:
        """Allows ESP32 hardware Settings menu to pull latest GitHub Release .bin and get its URL."""
        try:
            res = await station_control.check_and_pull_latest_ota(auto_flash=False)
            ver = str(res.get("latest_version") or res.get("version") or "2.4.1").lstrip("v")
            return web.json_response({
                "success": True,
                "version": ver,
                "bin_url": "https://df.api.thetechdude.io.vn/api/app/control/ota/firmware.bin",
            })
        except Exception as e:
            return error_response(f"ota_trigger_failed: {e}", status=502)

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
            # `language` changes the station UI language too (menus, view list,
            # status screens), not only the clock face.
            if isinstance(data, dict) and str(data.get("language", "")).strip().lower() in ("vi", "en"):
                station_control.set_station_language(str(data["language"]).strip().lower())
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
        # Station defaults: 60 ms dot unit, 1000 Hz tone.
        unit_ms = data.get("unit_ms", 60)
        if not isinstance(unit_ms, int) or isinstance(unit_ms, bool) or unit_ms < 20 or unit_ms > 500:
            unit_ms = 60
        freq = data.get("freq", 1000)
        if not isinstance(freq, int) or isinstance(freq, bool) or freq < 400 or freq > 4000:
            freq = 1000
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
        preset_id = data.get("preset_id") or data.get("song") or data.get("preset")
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
                raw_tracks = data.get("selected_tracks")
                sel_tracks = [int(t) for t in raw_tracks] if isinstance(raw_tracks, list) else None
                notes = parse_midi_bytes(midi_bytes, selected_tracks=sel_tracks)
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

    async def _read_midi_request(self, request: web.Request):
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            field = await reader.next()
            if not field:
                raise ValueError("no_file_uploaded")
            filename = field.filename or "Uploaded.mid"
            content = await field.read()
        else:
            content = await request.read()
            filename = request.headers.get("X-Filename", "Uploaded.mid")
        return filename, content

    async def handle_melody_inspect(self, request: web.Request) -> web.Response:
        """(#41) Inspects SMF MIDI chunks/tracks without truncating so UI can pick lead track."""
        await self._authenticate(request, required_scope="control")
        from midi_parser import inspect_midi_tracks
        try:
            filename, content = await self._read_midi_request(request)
            info = inspect_midi_tracks(content)
            return web.json_response({"success": True, "filename": filename, **info})
        except Exception as e:
            return error_response(f"midi_inspect_error: {str(e)}", status=400)

    async def handle_melody_upload(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from midi_parser import parse_midi_bytes
        try:
            filename, content = await self._read_midi_request(request)
            tracks_param = request.query.get("tracks") or request.headers.get("X-Midi-Tracks", "")
            sel_tracks = [int(t.strip()) for t in tracks_param.split(",") if t.strip().isdigit()] if tracks_param else None
            notes = parse_midi_bytes(content, selected_tracks=sel_tracks)
            title = filename.rsplit(".", 1)[0]
            res = await station_control.play_melody(notes=notes, title=title)
            res["filename"] = filename
            res["note_count"] = len(notes)
            res["notes"] = notes[:240]
            return web.json_response(res)
        except Exception as e:
            return error_response(f"midi_parse_error: {str(e)}", status=400)

    async def handle_melody_library_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        return web.json_response({"success": True, "items": station_control.list_saved_melodies()})

    async def handle_melody_library_post(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        try:
            entry = station_control.save_custom_melody(
                name=str(data.get("name") or "My Melody"),
                notes=data.get("notes") or [],
                source=str(data.get("source") or "custom"),
            )
            return web.json_response({"success": True, "item": entry, "items": station_control.list_saved_melodies()})
        except ValueError as e:
            return error_response(str(e), status=400)

    async def handle_melody_library_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        mid = request.match_info.get("id", "")
        if station_control.delete_saved_melody(mid):
            return web.json_response({"success": True, "items": station_control.list_saved_melodies()})
        return error_response("saved_melody_not_found", status=404)

    async def handle_melody_library_play(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        mid = request.match_info.get("id", "")
        try:
            res = await station_control.play_saved_melody(mid)
            return web.json_response(res)
        except FileNotFoundError:
            return error_response("saved_melody_not_found", status=404)

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
            aqi_uv = await scheduler.get_weather_aqi_uv(city=city)
            return web.json_response({
                "success": True,
                "city": res.get("city", ""),
                "available": res.get("available", False),
                "intervals": res.get("intervals", []),
                "aqi_uv": aqi_uv,
                "data": {**res, "aqi_uv": aqi_uv},
            })
        except Exception as e:
            return error_response(f"forecast_error: {e}", status=500)

    async def handle_weather_aqi(self, request: web.Request) -> web.Response:
        city = request.query.get("city")
        try:
            import scheduler
            aqi_uv = await scheduler.get_weather_aqi_uv(city=city)
            return web.json_response({"success": True, **aqi_uv})
        except Exception as e:
            return error_response(f"aqi_error: {e}", status=500)

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

        # Optionally pin the generated quote to the station as a sticky note, so
        # "generate then pin" works from every client in one call.
        if data.get("pin") is True and res.get("success") and res.get("quote"):
            try:
                note = station_control.add_note(
                    content=str(res["quote"]), title="Danh ngôn"
                )
                note = station_control.pin_note_to_station(note["id"])
                res["note"] = note
                res["pinned"] = True
            except Exception as exc:
                logger.warning(f"[AppAPI] Could not pin generated quote: {exc}")
                res["pinned"] = False

        res.setdefault("author", "Groq AI")
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
        auth_header = request.headers.get("Authorization")
        if auth_header:
            try:
                await self._authenticate(request)
            except Exception:
                pass
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
            tid = res.get("track_id") or res.get("id") or ""
            if tid:
                res["audio_features"] = await asyncio.to_thread(spotify_service.get_track_audio_features, str(tid))
            return web.json_response(res)
        except Exception as e:
            logger.error(f"[AppAPI] Error fetching Spotify status: {e}")
            return error_response("spotify_unavailable", status=503)

    async def handle_spotify_playback(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
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
        await self._authenticate(request)
        data = await self._parse_json(request)
        raw_vol = data.get("volume")

        # Flexible numeric parsing (support int & float sliders)
        if raw_vol is None or isinstance(raw_vol, bool):
            return error_response("volume_must_be_integer_0_100", status=400)
        try:
            vol = int(round(float(raw_vol)))
            if not (0 <= vol <= 100):
                return error_response("volume_must_be_integer_0_100", status=400)
        except (ValueError, TypeError):
            return error_response("volume_must_be_integer_0_100", status=400)

        try:
            await asyncio.to_thread(spotify_service.set_playback_volume, vol)
            # Read back so we can tell the caller whether Spotify really changed.
            applied = await asyncio.to_thread(spotify_service.get_current_volume)
            effective = vol if applied is None else int(applied)
            # Mirror the AUTHORITATIVE value to the ESP32 and every app.
            station_control.set_spotify_volume(effective)
            return web.json_response({
                "success": True,
                "volume": effective,
                "requested": vol,
                "applied": applied is None or abs(effective - vol) <= 1,
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

    # --- Google Calendar Integration (OAuth 2.0 + Private iCal) ---
    async def handle_calendar_status(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        status = calendar_service.get_public_status()
        return web.json_response({"success": True, "data": status, **status})

    async def handle_calendar_events(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        refresh = str(request.query.get("refresh", "")).lower() in ("1", "true", "yes")
        days_raw = request.query.get("days", "30")
        try:
            days = max(1, min(90, int(days_raw)))
        except ValueError:
            days = 30

        status = calendar_service.get_public_status()
        if refresh:
            status = await calendar_service.sync_events(days_ahead=days, push_to_station=True)
        return web.json_response({
            "success": True,
            "events": status.get("events", []),
            "data": status,
            "connected": status.get("connected", False),
            "oauth_connected": status.get("oauth_connected", False),
            "ical_connected": status.get("ical_connected", False),
            "last_sync_at": status.get("last_sync_at"),
            "last_error": status.get("last_error"),
        })

    async def handle_calendar_config(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        try:
            calendar_service.update_config(
                ical_url=data.get("ical_url"),
                google_client_id=data.get("google_client_id"),
                google_client_secret=data.get("google_client_secret"),
                google_redirect_uri=data.get("google_redirect_uri"),
                selected_calendar_id=data.get("selected_calendar_id"),
                selected_calendar_ids=data.get("selected_calendar_ids"),
            )
            status = await calendar_service.sync_events(push_to_station=True)
            return web.json_response({"success": True, "data": status, **status})
        except ValueError as e:
            return error_response(str(e), status=400)
        except Exception as e:
            logger.error(f"[AppAPI] Calendar config update failed: {e}")
            return error_response("calendar_config_failed", status=500)

    async def handle_calendar_sync(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            status = await calendar_service.sync_events(push_to_station=True)
            return web.json_response({"success": True, "data": status, **status})
        except Exception as e:
            logger.error(f"[AppAPI] Calendar sync error: {e}")
            return error_response(f"calendar_sync_failed: {e}", status=500)

    async def handle_calendar_create_event(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        try:
            res = await calendar_service.create_event(
                title=str(data.get("title") or ""),
                date_str=str(data.get("date") or ""),
                start_time=str(data.get("start_time") or ""),
                end_time=str(data.get("end_time") or ""),
                all_day=bool(data.get("all_day", False)),
                location=str(data.get("location") or ""),
                description=str(data.get("description") or ""),
                calendar_id=data.get("calendar_id"),
            )
            status = res["status"]
            return web.json_response({
                "success": True,
                "created_id": res.get("created_id", ""),
                "data": status,
                **status,
            })
        except PermissionError as e:
            return error_response(str(e), status=400)
        except ValueError as e:
            return error_response(str(e), status=400)
        except Exception as e:
            logger.error(f"[AppAPI] Create Google Calendar event failed: {e}")
            return error_response(f"calendar_create_failed: {e}", status=500)

    async def handle_calendar_delete_event(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        event_id = request.match_info.get("id", "")
        cal_id = request.query.get("calendar_id")
        try:
            status = await calendar_service.delete_event(event_id=event_id, calendar_id=cal_id)
            return web.json_response({"success": True, "data": status, **status})
        except PermissionError as e:
            return error_response(str(e), status=400)
        except ValueError as e:
            return error_response(str(e), status=400)
        except Exception as e:
            logger.error(f"[AppAPI] Delete Google Calendar event failed: {e}")
            return error_response(f"calendar_delete_failed: {e}", status=500)

    async def handle_calendar_oauth_start(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            data = calendar_service.build_oauth_start_url()
            return web.json_response({"success": True, **data})
        except ValueError as e:
            return error_response(str(e), status=400)

    async def handle_calendar_oauth_callback(self, request: web.Request) -> web.Response:
        err = request.query.get("error")
        if err:
            return web.Response(
                text=render_oauth_result_html(False, error=err),
                content_type="text/html",
                status=400,
            )
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        if not code or not state:
            return web.Response(
                text=render_oauth_result_html(False, error="missing_code_or_state"),
                content_type="text/html",
                status=400,
            )
        try:
            status = await calendar_service.complete_oauth_callback(code=code, state=state)
            return web.Response(
                text=render_oauth_result_html(True, email=status.get("connected_email", "")),
                content_type="text/html",
            )
        except Exception as e:
            logger.error(f"[AppAPI] Google Calendar OAuth callback error: {e}")
            return web.Response(
                text=render_oauth_result_html(False, error=str(e)),
                content_type="text/html",
                status=400,
            )

    async def handle_calendar_oauth_disconnect(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        status = calendar_service.disconnect_oauth()
        calendar_service.push_to_station()
        return web.json_response({"success": True, "data": status, **status})

    async def handle_calendar_ai_sync_tkb(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            data = await self._parse_json(request)
        except Exception:
            data = {}
        week_offset = int(data.get("week_offset", 0) or 0)
        use_ai = bool(data.get("use_ai", True))
        try:
            res = await calendar_service.ai_sync_tkb_to_calendar(week_offset=week_offset, use_ai=use_ai)
            return web.json_response(res)
        except PermissionError as pe:
            return error_response(str(pe), status=403)
        except ValueError as ve:
            return error_response(str(ve), status=400)
        except Exception as e:
            logger.error(f"[AppAPI] AI TKB sync error: {e}", exc_info=True)
            return error_response(f"ai_sync_failed: {e}", status=500)

    # --- #11 AI Natural Language Calendar Quick Add ---
    async def handle_calendar_quick_add(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        text = str(data.get("text") or "").strip()
        if not text:
            return error_response("text_required", status=400)
        try:
            res = await calendar_service.ai_quick_add(text)
            status = res.get("status") or {}
            return web.json_response({
                "success": True,
                "created_id": res.get("created_id", ""),
                "parsed": res.get("parsed", {}),
                "data": status,
                **status,
            })
        except (PermissionError, ValueError) as e:
            return error_response(str(e), status=400)
        except Exception as e:
            logger.error(f"[AppAPI] Calendar Quick Add error: {e}")
            return error_response(f"quick_add_failed: {e}", status=500)

    # --- #28 Custom Boot Logo, #23 Game Mode, #54 BPM Sync ---
    async def handle_boot_logo(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        try:
            res = station_control.set_boot_logo(
                title=str(data.get("title") or "DESKFLOW"),
                sub=str(data.get("sub") or ""),
                bitmap=str(data.get("bitmap") or ""),
            )
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    async def handle_game_mode(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        try:
            mode = int(data.get("mode", 0))
            res = station_control.set_game_mode(mode)
            return web.json_response(res)
        except (ValueError, TypeError):
            return error_response("invalid_game_mode", status=400)

    async def handle_lamp_bpm_sync(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        enabled = bool(data.get("enabled", True))
        return web.json_response(station_control.set_bpm_sync(enabled))

    # --- #58 Spotify Listening Stats Digest ---
    async def handle_spotify_stats(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        stats = await asyncio.to_thread(spotify_service.get_listening_stats)
        return web.json_response({"success": True, "data": stats, **stats})

    # --- #45 Voice TTS Morning Briefing ---
    async def handle_control_briefing(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        briefing = await asyncio.to_thread(station_control.build_morning_briefing)
        return web.json_response({"success": True, "data": briefing, **briefing})

    # --- #71 Real-Time SSE Live Stream ---
    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        auth_header = request.headers.get("Authorization")
        if not auth_header and request.query.get("token"):
            auth_header = f"Bearer {request.query.get('token')}"
        device = await self.auth.authenticate_token(auth_header)
        require_scope(device, "control")

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        import json
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        setattr(q, "_deskflow_loop", asyncio.get_running_loop())
        station_control.register_sse_queue(q)
        try:
            init_payload = {
                "status": station_control.get_station_snapshot(),
                "settings": station_control.get_settings(),
            }
            await resp.write(f"event: snapshot\ndata: {json.dumps(init_payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    await resp.write(
                        f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                except asyncio.TimeoutError:
                    snap = station_control.get_station_snapshot()
                    await resp.write(
                        f"event: heartbeat\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
        except (asyncio.CancelledError, ConnectionResetError, Exception):
            pass
        finally:
            station_control.unregister_sse_queue(q)
        return resp

    # --- #73 AI DeskFlow Copilot Chat & Action Dispatcher ---
    async def _execute_copilot_action(self, action_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if action_name == "lamp_color":
            return station_control.set_lamp_color(
                str(args.get("hex") or "#FFFFFF"),
                hex_color2=args.get("hex2"),
                gradient=args.get("gradient"),
            )
        if action_name == "lamp_effect":
            return station_control.set_lamp_effect(
                str(args.get("effect") or "breathing"),
                speed=args.get("speed"),
                color=args.get("color"),
                rgb=args.get("rgb"),
            )
        if action_name == "lamp_off":
            return station_control.set_lamp_off()
        if action_name == "switch_view":
            return station_control.switch_view(str(args.get("view") or "main"))
        if action_name == "start_focus":
            return station_control.start_timer(int(args.get("minutes") or 25))
        if action_name == "create_note":
            return station_control.add_note(
                title=str(args.get("title") or "AI Note"),
                content=str(args.get("content") or ""),
            )
        if action_name == "create_alarm":
            return station_control.save_alarm({
                "time": str(args.get("time") or "07:00"),
                "label": str(args.get("label") or "DeskFlow AI Alarm"),
                "enabled": True,
            })
        if action_name == "quick_add_event":
            return await calendar_service.ai_quick_add(str(args.get("text") or ""))
        if action_name == "game_mode":
            return station_control.set_game_mode(int(args.get("mode", 0)))
        return {"skipped": True}

    async def handle_ai_chat(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        message = str(data.get("message") or "").strip()
        if not message:
            return error_response("message_required", status=400)

        import json
        snap = station_control.get_station_snapshot()
        ctx_summary = (
            f"Weather={snap.get('weather')}, Track={snap.get('spotify_track')}, "
            f"Mode={snap.get('current_screen_mode')}, LED={station_control.device_settings.get('led_effect')}"
        )

        reply = "Mình đã ghi nhận yêu cầu của bạn."
        action_obj: Dict[str, Any] = {"action": "none", "args": {}}
        action_result: Optional[Dict[str, Any]] = None

        groq_client = getattr(station_control, "_groq_client", None)
        if groq_client:
            sys_prompt = (
                "You are DeskFlow AI Copilot. Respond concisely in Vietnamese. "
                "Output STRICT JSON with keys: {\"reply\": string, \"action\": string, \"args\": object}. "
                "Supported actions: "
                "\"lamp_color\" ({hex, hex2?, gradient?}), "
                "\"lamp_effect\" ({effect: solid|rainbow|breathing|chase|police|sparkle|fire|strobe|ambient, speed?, rgb?}), "
                "\"lamp_off\" ({}), "
                "\"switch_view\" ({view: main|tkb|calendar|note|exam|quote|timer|game|alarm}), "
                "\"start_focus\" ({minutes}), "
                "\"create_note\" ({title, content}), "
                "\"create_alarm\" ({time: HH:MM, label}), "
                "\"quick_add_event\" ({text}), "
                "\"game_mode\" ({mode: 0|1|2}), "
                "\"none\" ({}). Only pick an action if user clearly requests a device/app command."
            )
            try:
                def _call_groq():
                    comp = groq_client.chat.completions.create(
                        model="openai/gpt-oss-20b",
                        messages=[
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": f"Context: {ctx_summary}\nUser: {message}"},
                        ],
                        temperature=0.2,
                        max_completion_tokens=1024,
                        reasoning_effort="low",
                    )
                    return comp.choices[0].message.content or ""

                raw = await asyncio.to_thread(_call_groq)
                m = re.search(r"\{[\s\S]*\}", raw)
                if m:
                    parsed = json.loads(m.group(0))
                    reply = str(parsed.get("reply") or reply)
                    action_obj = {
                        "action": str(parsed.get("action") or "none"),
                        "args": parsed.get("args") if isinstance(parsed.get("args"), dict) else {},
                    }
            except Exception as e:
                logger.warning(f"[AI Copilot] Groq fallback: {e}")
                reply = f"Trạm hiện tại: {ctx_summary}. (AI đang dùng chế độ nhanh)"

        act_name = action_obj.get("action", "none")
        if act_name and act_name != "none":
            try:
                action_result = await self._execute_copilot_action(act_name, action_obj.get("args") or {})
            except Exception as e:
                action_result = {"error": str(e)}

        return web.json_response({
            "success": True,
            "reply": reply,
            "action": act_name,
            "action_result": action_result,
        })

    # --- #78 Shareable Public Desk Status & Live Badge SVG (No private data) ---
    def _build_public_status(self) -> Dict[str, Any]:
        snap = station_control.get_station_snapshot()
        return {
            "online": bool(snap.get("wifi_rssi") is not None and snap.get("station_online", True)),
            "version": "2.2.0",
            "mode": snap.get("current_screen_mode", "main"),
            "weather": snap.get("weather", "N/A"),
            "spotify_track": snap.get("spotify_track", "Idle"),
            "spotify_artist": snap.get("spotify_artist", ""),
            "led_effect": station_control.device_settings.get("led_effect", "solid"),
        }

    async def handle_public_status(self, request: web.Request) -> web.Response:
        return web.json_response({"success": True, **self._build_public_status()})

    async def handle_public_badge_svg(self, request: web.Request) -> web.Response:
        import html
        info = self._build_public_status()
        track = html.escape(str(info.get("spotify_track") or "Idle")[:36])
        weather = html.escape(str(info.get("weather") or "DeskFlow")[:20])
        label = "DeskFlow Live"
        status_txt = f"{weather} • {track}"
        width = max(210, min(460, 115 + len(status_txt) * 7))
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="24" role="img" aria-label="DeskFlow Live">'
            f'<rect width="{width}" height="24" rx="4" fill="#111114"/>'
            f'<rect x="1" y="1" width="{width-2}" height="22" rx="3" fill="none" stroke="#27272A"/>'
            f'<circle cx="12" cy="12" r="4" fill="#22C55E"/>'
            f'<text x="22" y="16" fill="#FFFFFF" font-family="ui-monospace,SFMono-Regular,monospace" font-size="11" font-weight="600">{label}</text>'
            f'<text x="110" y="16" fill="#A1A1AA" font-family="ui-sans-serif,system-ui,sans-serif" font-size="11">{status_txt}</text>'
            f'</svg>'
        )
        return web.Response(
            text=svg,
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-cache, max-age=30"},
        )

    # --- #80 1-Click Backup & Restore JSON ---
    async def handle_ota_check_update(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        try:
            data = await self._parse_json(request)
        except Exception:
            data = {}
        auto_pull = bool(data.get("auto_pull", True))
        auto_flash = bool(data.get("auto_flash", True))
        force = bool(data.get("force", False))
        scheme = "https" if request.headers.get("X-Forwarded-Proto", "") == "https" or "thetechdude.io.vn" in request.host else request.scheme
        base_url = f"{scheme}://{request.host}" if request.host else "https://df.api.thetechdude.io.vn"
        try:
            res = await station_control.check_and_pull_latest_ota(
                auto_pull=auto_pull,
                auto_flash=auto_flash,
                force=force,
                public_base_url=base_url,
            )
            return web.json_response({"success": True, "data": res, **res})
        except FileNotFoundError as fe:
            return error_response(str(fe), status=404)
        except Exception as e:
            logger.error(f"[AppAPI OTA Check] Failed: {e}")
            return error_response(f"ota_check_failed: {e}", status=502)

    async def handle_backup_export(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        backup = await asyncio.to_thread(station_control.export_backup)
        return web.json_response({"success": True, "backup": backup, **backup})

    async def handle_backup_restore(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        payload = data.get("backup") if isinstance(data.get("backup"), dict) else data
        try:
            res = await asyncio.to_thread(station_control.restore_backup, payload)
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    # --- (#16) Excel Timetable Upload & Revert, (#17) Stats, (#11) Formulas ---
    async def handle_timetable_upload_excel(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        raw_bytes = b""
        week_num = request.query.get("week_number")
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            async for field in reader:
                if field.name == "file":
                    raw_bytes = await field.read()
                elif field.name == "week_number":
                    week_num = (await field.text()).strip() or week_num
        else:
            raw_bytes = await request.read()
        if not raw_bytes:
            return error_response("missing_excel_file", status=400)
        try:
            saved = await asyncio.to_thread(timetable.parse_excel_timetable, raw_bytes, week_num)
            dual_tkb = await timetable.get_dual_day_tkb()
            stats = timetable.get_weekly_subject_stats(saved.get("schedule_data") or {})
            try:
                station_control.mqtt_publish("deskflow/schedule", json.dumps(timetable.build_station_payload(dual_tkb), ensure_ascii=False))
            except Exception:
                pass
            return web.json_response({
                "success": True,
                "source": "excel_custom",
                "week_number": saved.get("week_number"),
                "data": dual_tkb,
                "stats": stats,
            })
        except ValueError as ve:
            return error_response(str(ve), status=400)
        except Exception as e:
            return error_response(f"excel_parse_failed: {e}", status=400)

    async def handle_timetable_custom_clear(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        removed = timetable.clear_custom_timetable()
        dual_tkb = await timetable.get_dual_day_tkb()
        return web.json_response({"success": True, "removed": removed, "data": dual_tkb})

    async def handle_timetable_stats(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        sched = await timetable.fetch_current_weekly_schedule()
        dual_tkb = await timetable.get_dual_day_tkb()
        stats = timetable.get_weekly_subject_stats((sched or {}).get("schedule_data") or {})
        live = timetable.get_live_period_status((dual_tkb or {}).get("today"))
        return web.json_response({"success": True, "stats": stats, "live": live, "source": (sched or {}).get("source", "pocketbase")})

    async def handle_formulas_get(self, request: web.Request) -> web.Response:
        from src import v24_features
        custom = v24_features.get_custom_formulas()
        return web.json_response({
            "success": True,
            "formulas": custom + timetable.QUICK_FORMULAS,
            "custom_formulas": custom,
            "elements": timetable.PERIODIC_ELEMENTS,
        })

    async def handle_formulas_custom_post(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from src import v24_features
        data = await self._parse_json(request)
        title = str(data.get("title") or "").strip()
        formula = str(data.get("formula") or "").strip()
        if not title or not formula:
            return error_response("missing_title_or_formula", status=400)
        item = v24_features.add_custom_formula(
            subject=str(data.get("subject") or "Khác"),
            title=title,
            formula=formula,
            note=str(data.get("note") or ""),
        )
        return web.json_response({"success": True, "item": item, "custom_formulas": v24_features.get_custom_formulas()})

    async def handle_formulas_custom_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from src import v24_features
        fid = request.match_info.get("id", "")
        ok = v24_features.delete_custom_formula(fid)
        return web.json_response({"success": ok, "custom_formulas": v24_features.get_custom_formulas()})

    async def handle_teachers_get(self, request: web.Request) -> web.Response:
        from src import v24_features
        return web.json_response({"success": True, "teachers": v24_features.get_teachers()})

    async def handle_teachers_post(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from src import v24_features
        data = await self._parse_json(request)
        name = str(data.get("name") or "").strip()
        subject = str(data.get("subject") or "").strip()
        if not name or not subject:
            return error_response("missing_name_or_subject", status=400)
        item = v24_features.add_teacher(
            name=name,
            subject=subject,
            phone=str(data.get("phone") or ""),
            zalo=str(data.get("zalo") or ""),
            email=str(data.get("email") or ""),
            note=str(data.get("note") or ""),
        )
        return web.json_response({"success": True, "teacher": item, "teachers": v24_features.get_teachers()})

    async def handle_teachers_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from src import v24_features
        tid = request.match_info.get("id", "")
        ok = v24_features.delete_teacher(tid)
        return web.json_response({"success": ok, "teachers": v24_features.get_teachers()})

    async def handle_notes_export(self, request: web.Request) -> web.Response:
        notes = station_control.get_notes()
        lines = ["# DeskFlow Notes Export\n"]
        for n in notes:
            title = n.get("title") or n.get("id") or "Ghi chú"
            content = n.get("content") or n.get("text") or ""
            lines.append(f"## {title}\n\n{content}\n")
        md = "\n---\n\n".join(lines)
        return web.Response(
            text=md,
            content_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="deskflow-notes.md"'},
        )

    async def handle_alarm_check_tomorrow(self, request: web.Request) -> web.Response:
        from datetime import datetime, timedelta
        from src import alarm_engine
        tomorrow = (datetime.now() + timedelta(days=1)).weekday()
        has_early = tomorrow < 6
        alarms = alarm_engine.list_alarms() if hasattr(alarm_engine, "list_alarms") else []
        enabled_early = any(a.get("enabled", True) and int(a.get("hour", 9)) <= 7 for a in alarms)
        return web.json_response({
            "success": True,
            "has_early_period": has_early,
            "has_early_alarm": enabled_early,
            "warn": has_early and not enabled_early,
            "message": "Ngày mai có tiết 1 lúc 7h15 nhưng bạn chưa bật báo thức!" if (has_early and not enabled_early) else "",
        })

    async def handle_device_battery_post(self, request: web.Request) -> web.Response:
        from src import v24_features
        data = await self._parse_json(request)
        res = v24_features.update_phone_battery(int(data.get("level", 100)), bool(data.get("charging", False)))
        station_control.publish_mqtt("deskflow/device/battery", json.dumps(res))
        return web.json_response({"success": True, **res})

    async def handle_device_battery_get(self, request: web.Request) -> web.Response:
        from src import v24_features
        return web.json_response({"success": True, **v24_features.get_phone_battery()})

    async def handle_device_call_post(self, request: web.Request) -> web.Response:
        from src import v24_features
        data = await self._parse_json(request)
        res = v24_features.record_incoming_call(str(data.get("caller") or "Cuộc gọi đến"), str(data.get("number") or ""))
        station_control.publish_mqtt("deskflow/device/call", json.dumps(res, ensure_ascii=False))
        return web.json_response({"success": True, **res})

    async def handle_github_repos_get(self, request: web.Request) -> web.Response:
        from src import v24_features
        return web.json_response({"success": True, "repos": v24_features.get_github_repos(), "runs": v24_features.fetch_github_ci_summary()})

    async def handle_github_repos_post(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from src import v24_features
        data = await self._parse_json(request)
        repos = v24_features.add_github_repo(str(data.get("repo") or ""))
        return web.json_response({"success": True, "repos": repos})

    async def handle_github_repos_delete(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        from src import v24_features
        data = await self._parse_json(request)
        repos = v24_features.delete_github_repo(str(data.get("repo") or ""))
        return web.json_response({"success": True, "repos": repos})

    async def handle_serial_logs_get(self, request: web.Request) -> web.Response:
        from src import v24_features
        return web.json_response({"success": True, "logs": v24_features.get_serial_logs()})

    async def handle_changelog_get(self, request: web.Request) -> web.Response:
        from src import v24_features
        return web.json_response({"success": True, **v24_features.CHANGELOG_V240})

    # --- (#18) Calendar Holiday Toggle ---
    async def handle_calendar_holiday(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = await self._parse_json(request)
        date_str = str(data.get("date") or "")
        is_hol = data.get("is_holiday")
        try:
            res = calendar_service.toggle_holiday_date(date_str, is_hol)
            return web.json_response(res)
        except ValueError as ve:
            return error_response(str(ve), status=400)

    # --- (#75) ESP32 Screenshot Capture & Image Feed ---
    async def handle_screenshot_capture(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        prev_ts = int(station_control.device_settings.get("last_screenshot_at") or 0)
        res = station_control.request_screenshot()
        for _ in range(20):
            await asyncio.sleep(0.2)
            if int(station_control.device_settings.get("last_screenshot_at") or 0) > prev_ts:
                break
        return web.json_response({
            **res,
            "ready": int(station_control.device_settings.get("last_screenshot_at") or 0) > prev_ts,
            "last_screenshot_at": station_control.device_settings.get("last_screenshot_at"),
        })

    async def handle_station_screenshot_upload(self, request: web.Request) -> web.Response:
        raw = await request.read()
        try:
            w = int(request.query.get("w", "240"))
            h = int(request.query.get("h", "320"))
            res = await asyncio.to_thread(station_control.save_screenshot_rgb565_rle, w, h, raw)
            return web.json_response(res)
        except Exception as e:
            return error_response(f"screenshot_save_failed: {e}", status=400)

    async def handle_screenshot_latest(self, request: web.Request) -> web.Response:
        raw = station_control.get_latest_screenshot_bytes()
        if not raw:
            return error_response("no_screenshot", status=404)
        return web.Response(body=raw, content_type="image/png", headers={"Cache-Control": "no-store"})

    # --- (#57) GitHub Stats & ESP32 HTTP Pair-Code Fallback ---
    async def handle_github_sync(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="control")
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except Exception:
                data = {}
        res = await station_control.sync_github_stats(username=data.get("username"))
        return web.json_response(res)

    async def handle_station_pair_code_http(self, request: web.Request) -> web.Response:
        raw_code, expires_at = await self.auth.generate_pairing_code(scopes=["focus", "control", "spotify"])
        qr_payload = f"deskflow://pair?code={raw_code}"
        qr_size = 0
        qr_bits_hex = ""
        try:
            import qrcode
            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=1)
            qr.add_data(qr_payload)
            qr.make(fit=True)
            matrix = qr.get_matrix()
            qr_size = len(matrix)
            buf = bytearray((qr_size * qr_size + 7) // 8)
            bit_idx = 0
            for row in matrix:
                for val in row:
                    if val:
                        buf[bit_idx >> 3] |= (1 << (7 - (bit_idx & 7)))
                    bit_idx += 1
            qr_bits_hex = buf.hex().upper()
        except Exception:
            pass
        return web.json_response({
            "success": True,
            "code": raw_code,
            "qr_payload": qr_payload,
            "qr_size": qr_size,
            "qr_bits": qr_bits_hex,
            "expires_at": expires_at,
            "expires_in": 600,
            "full_access": True,
        })

    # --- (#132, #140) Spotify Queue & Playback Devices ---
    async def handle_spotify_queue(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        q = await asyncio.to_thread(spotify_service.get_playback_queue)
        return web.json_response({"success": True, **q})

    async def handle_spotify_history(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        items = await asyncio.to_thread(spotify_service.get_recently_played, 20)
        return web.json_response({"success": True, "history": items})

    async def handle_spotify_devices(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        devs = await asyncio.to_thread(spotify_service.get_available_devices)
        return web.json_response({"success": True, "devices": devs})

    async def handle_spotify_transfer(self, request: web.Request) -> web.Response:
        await self._authenticate(request, required_scope="spotify")
        data = await self._parse_json(request)
        dev_id = str(data.get("device_id") or "").strip()
        if not dev_id:
            return error_response("device_id_required", status=400)
        try:
            res = await asyncio.to_thread(spotify_service.transfer_playback_device, dev_id, True)
            return web.json_response(res)
        except Exception as e:
            return error_response(f"transfer_failed: {e}", status=500)

    # --- (#157) GitHub & Vercel Webhooks -> LED Flash + Push ---
    async def handle_webhook_github(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        repo = str((data.get("repository") or {}).get("name") or "deskflow")
        if "workflow_run" in data:
            wr = data["workflow_run"] or {}
            status = str(wr.get("conclusion") or wr.get("status") or "updated")
            summary = f"{wr.get('name', 'Workflow')}: {status}"
        elif "commits" in data:
            cnt = len(data.get("commits") or [])
            status = "pushed"
            summary = f"Push +{cnt} commit(s) -> {repo}"
        else:
            status = str(request.headers.get("X-GitHub-Event") or "event")
            summary = f"GitHub {status} on {repo}"
        res = station_control.record_webhook_event("github", repo, status, summary)
        return web.json_response(res)

    async def handle_webhook_vercel(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        ev_type = str(data.get("type") or payload.get("state") or "deployment")
        name = str((payload.get("deployment") or {}).get("name") or payload.get("name") or "deskflow-web")
        summary = f"Vercel {name}: {ev_type}"
        res = station_control.record_webhook_event("vercel", name, ev_type, summary)
        return web.json_response(res)


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
    app.router.add_post("/api/app/control/alarm/confirm-awake", handler.handle_alarm_confirm_awake)
    app.router.add_post("/api/app/control/alarm/buzz", handler.handle_alarm_buzz)
    app.router.add_post("/api/app/control/alarm/schedule", handler.handle_alarm_schedule)
    app.router.add_post("/api/app/control/morse/play", handler.handle_morse_play)
    app.router.add_post("/api/app/control/morse/stop", handler.handle_morse_stop)
    app.router.add_get("/api/app/melody/presets", handler.handle_melody_presets)
    app.router.add_get("/api/app/control/melody/presets", handler.handle_melody_presets)
    app.router.add_post("/api/app/control/melody/play", handler.handle_melody_play)
    app.router.add_post("/api/app/control/melody/inspect", handler.handle_melody_inspect)
    app.router.add_post("/api/app/control/melody/upload", handler.handle_melody_upload)
    app.router.add_post("/api/app/control/melody/stop", handler.handle_melody_stop)
    app.router.add_get("/api/app/control/melody/library", handler.handle_melody_library_get)
    app.router.add_post("/api/app/control/melody/library", handler.handle_melody_library_post)
    app.router.add_delete("/api/app/control/melody/library/{id}", handler.handle_melody_library_delete)
    app.router.add_post("/api/app/control/melody/library/{id}/play", handler.handle_melody_library_play)

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
    app.router.add_post("/api/app/control/lamp/effect", handler.handle_lamp_effect)
    app.router.add_post("/api/app/control/lamp/ambient", handler.handle_lamp_ambient)
    app.router.add_post("/api/app/control/led", handler.handle_led)
    app.router.add_get("/api/app/control/notifications", handler.handle_notifications_get)
    app.router.add_post("/api/app/control/notify/test", handler.handle_notify_test)
    app.router.add_get("/api/app/control/ota/status", handler.handle_ota_status)
    app.router.add_post("/api/app/control/ota/check-update", handler.handle_ota_check_update)
    app.router.add_post("/api/app/control/ota/upload", handler.handle_ota_upload)
    app.router.add_get("/api/app/control/ota/firmware.bin", handler.handle_ota_firmware_download)
    app.router.add_post("/api/app/control/ota/flash", handler.handle_ota_flash)
    app.router.add_post("/api/app/station/ota-trigger", handler.handle_station_ota_trigger)
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
    app.router.add_get("/api/app/spotify/stats", handler.handle_spotify_stats)
    app.router.add_get("/api/app/spotify/queue", handler.handle_spotify_queue)
    app.router.add_get("/api/app/spotify/history", handler.handle_spotify_history)
    app.router.add_get("/api/app/spotify/devices", handler.handle_spotify_devices)
    app.router.add_post("/api/app/spotify/transfer", handler.handle_spotify_transfer)
    app.router.add_post("/api/app/spotify/playback", handler.handle_spotify_playback)
    app.router.add_post("/api/app/spotify/volume", handler.handle_spotify_volume)

    # Google Calendar (OAuth 2.0 + Private iCal)
    app.router.add_get("/api/app/calendar/status", handler.handle_calendar_status)
    app.router.add_get("/api/app/calendar/events", handler.handle_calendar_events)
    app.router.add_post("/api/app/calendar/config", handler.handle_calendar_config)
    app.router.add_post("/api/app/calendar/sync", handler.handle_calendar_sync)
    app.router.add_post("/api/app/calendar/events", handler.handle_calendar_create_event)
    app.router.add_post("/api/app/calendar/quick-add", handler.handle_calendar_quick_add)
    app.router.add_delete("/api/app/calendar/events/{id}", handler.handle_calendar_delete_event)
    app.router.add_post("/api/app/calendar/oauth/start", handler.handle_calendar_oauth_start)
    app.router.add_get("/api/app/calendar/oauth/callback", handler.handle_calendar_oauth_callback)
    app.router.add_post("/api/app/calendar/oauth/disconnect", handler.handle_calendar_oauth_disconnect)
    app.router.add_post("/api/app/calendar/ai-sync-tkb", handler.handle_calendar_ai_sync_tkb)

    # v2.2.0 Hardware, AI Copilot, Briefing, SSE, Backup & Public Badge
    app.router.add_post("/api/app/control/lamp/bpm-sync", handler.handle_lamp_bpm_sync)
    app.router.add_post("/api/app/control/boot-logo", handler.handle_boot_logo)
    app.router.add_post("/api/app/control/game/mode", handler.handle_game_mode)
    app.router.add_get("/api/app/control/briefing", handler.handle_control_briefing)
    app.router.add_get("/api/app/control/backup", handler.handle_backup_export)
    app.router.add_post("/api/app/control/restore", handler.handle_backup_restore)
    app.router.add_get("/api/app/stream", handler.handle_stream)
    app.router.add_post("/api/app/ai/chat", handler.handle_ai_chat)
    app.router.add_get("/api/public/status", handler.handle_public_status)
    app.router.add_get("/api/public/badge.svg", handler.handle_public_badge_svg)

    # v2.3.0 Extensions (#1, #11, #16, #17, #18, #57, #69, #75, #102, #103, #157)
    app.router.add_post("/api/app/control/timetable/upload-excel", handler.handle_timetable_upload_excel)
    app.router.add_delete("/api/app/control/timetable/custom", handler.handle_timetable_custom_clear)
    app.router.add_get("/api/app/control/timetable/stats", handler.handle_timetable_stats)
    app.router.add_get("/api/app/control/formulas", handler.handle_formulas_get)
    app.router.add_post("/api/app/control/formulas/custom", handler.handle_formulas_custom_post)
    app.router.add_delete("/api/app/control/formulas/custom/{id}", handler.handle_formulas_custom_delete)
    app.router.add_get("/api/app/timetable/teachers", handler.handle_teachers_get)
    app.router.add_post("/api/app/timetable/teachers", handler.handle_teachers_post)
    app.router.add_delete("/api/app/timetable/teachers/{id}", handler.handle_teachers_delete)
    app.router.add_get("/api/app/control/notes/export", handler.handle_notes_export)
    app.router.add_get("/api/app/control/alarm/check-tomorrow", handler.handle_alarm_check_tomorrow)
    app.router.add_post("/api/app/device/battery", handler.handle_device_battery_post)
    app.router.add_get("/api/app/device/battery", handler.handle_device_battery_get)
    app.router.add_post("/api/app/device/call", handler.handle_device_call_post)
    app.router.add_get("/api/app/github/repos", handler.handle_github_repos_get)
    app.router.add_post("/api/app/github/repos", handler.handle_github_repos_post)
    app.router.add_delete("/api/app/github/repos", handler.handle_github_repos_delete)
    app.router.add_get("/api/app/station/serial-logs", handler.handle_serial_logs_get)
    app.router.add_get("/api/app/changelog", handler.handle_changelog_get)
    app.router.add_post("/api/app/control/calendar/holiday", handler.handle_calendar_holiday)
    app.router.add_get("/api/app/control/weather/aqi", handler.handle_weather_aqi)
    app.router.add_post("/api/app/control/screenshot/capture", handler.handle_screenshot_capture)
    app.router.add_post("/api/app/station/screenshot", handler.handle_station_screenshot_upload)
    app.router.add_get("/api/app/control/screenshot/latest", handler.handle_screenshot_latest)
    app.router.add_post("/api/app/control/github/sync", handler.handle_github_sync)
    app.router.add_get("/api/app/station/pair-code", handler.handle_station_pair_code_http)
    app.router.add_post("/api/app/control/alarm/shake-dismiss", handler.handle_alarm_shake_dismiss)
    app.router.add_post("/api/app/control/alarm/anti-oversleep", handler.handle_alarm_anti_oversleep_config)
    app.router.add_post("/api/webhooks/github", handler.handle_webhook_github)
    app.router.add_post("/api/webhooks/vercel", handler.handle_webhook_vercel)

    # Also register CORS options preflight for /api/app/*
    async def options_app_handler(request: web.Request) -> web.Response:
        return web.Response(status=204)
    app.router.add_route("OPTIONS", "/api/app/{tail:.*}", options_app_handler)

    logger.info("[AppAPI] Unified App routes registered on aiohttp web app.")
