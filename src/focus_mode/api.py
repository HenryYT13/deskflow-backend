"""
DeskFlow Focus Mode - aiohttp REST API Server
Phase 7 implementation & Security Pass:
- Bound to 127.0.0.1:8787
- Base path: /api/focus
- Max body size: 16 KB (HTTP 413)
- Strict error envelope: {"error": {"code": ..., "retry_after": ...}}
- Sanitized errors: no stack traces, file paths, secrets, or raw exceptions
- Strict authentication and rate limiting
- Secure CORS origin validation:
  - Exact origin matching against explicitly configured origins via FOCUS_ALLOWED_ORIGINS
  - Explicit development origins (http://127.0.0.1:8787, http://localhost:8787) allowed ONLY when FOCUS_ALLOW_DEV_ORIGINS=1
  - Rejection of lookalike and malformed origins (e.g. http://localhost.attacker.example)
  - No wildcard '*' Access-Control-Allow-Origin
- Device self-revocation enforcement: authenticated devices can only revoke themselves (403 forbidden otherwise)
"""

import os
import json
import logging
import secrets
import time
import urllib.parse
from aiohttp import web
from typing import Dict, Any, Optional, Set

from .service import FocusService
from .storage import StorageUnavailableError
from .auth import AuthError, RateLimitExceeded
from .challenge import ChallengeError, CooldownActiveError, ChallengeNotActiveError

logger = logging.getLogger("DeskFlow.FocusMode.API")

ALLOWED_HOSTS: Set[str] = {
    "127.0.0.1:8787",
    "localhost:8787",
    "[::1]:8787",
    "df.api.thetechdude.io.vn"
}

DEV_ORIGINS: Set[str] = {
    "http://127.0.0.1:8787",
    "http://localhost:8787"
}


def get_allowed_hosts() -> Set[str]:
    hosts = set(ALLOWED_HOSTS)
    raw = os.getenv("FOCUS_ALLOWED_HOSTS", "")
    for h in raw.split(","):
        cleaned = h.strip().lower()
        if cleaned:
            hosts.add(cleaned)
    return hosts


def get_allowed_origins() -> Set[str]:
    """Retrieves explicitly configured allowed origins from FOCUS_ALLOWED_ORIGINS."""
    raw = os.getenv("FOCUS_ALLOWED_ORIGINS", "")
    origins = set()
    for item in raw.split(","):
        cleaned = item.strip().rstrip("/")
        if cleaned:
            origins.add(cleaned)
    return origins


def get_allowed_chrome_ids() -> Set[str]:
    """Retrieves explicitly authorized Chrome Extension IDs from FOCUS_ALLOWED_CHROME_IDS."""
    raw = os.getenv("FOCUS_ALLOWED_CHROME_IDS", "")
    ids = set()
    for item in raw.split(","):
        cleaned = item.strip()
        if cleaned:
            ids.add(cleaned)
    return ids


def is_dev_origins_allowed() -> bool:
    """Checks if development origins are explicitly enabled via FOCUS_ALLOW_DEV_ORIGINS=1."""
    val = os.getenv("FOCUS_ALLOW_DEV_ORIGINS", "").strip().lower()
    return val in ("1", "true", "yes")


def validate_origin(origin: Optional[str]) -> Optional[str]:
    """
    Validates origin against explicitly configured allowed origins and extension IDs.
    Rejects malformed origins, lookalikes, paths, queries, and credentials.
    Returns canonical origin string if allowed, None otherwise.
    """
    if not origin or not isinstance(origin, str):
        return None

    clean_origin = origin.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(clean_origin)
    except Exception:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in ("chrome-extension", "moz-extension", "http", "https"):
        return None

    # Origin must not have path, query, or fragment
    if parsed.path and parsed.path != "/":
        return None
    if parsed.query or parsed.fragment:
        return None
    # For HTTP/HTTPS origins, prevent credential/userinfo spoofing (e.g. http://user@host)
    if scheme in ("http", "https") and (parsed.username or parsed.password):
        return None
    if not parsed.netloc:
        return None

    canonical = f"{scheme}://{parsed.netloc}"

    # 1. Exact match against explicitly configured origins
    allowed = get_allowed_origins()
    if canonical in allowed:
        return canonical

    # 2. Chrome Extension ID verification
    if scheme == "chrome-extension":
        allowed_chrome_ids = get_allowed_chrome_ids()
        if parsed.netloc in allowed_chrome_ids:
            return canonical
        if is_dev_origins_allowed():
            return canonical

    # 3. Firefox dynamic UUID extension
    if scheme == "moz-extension":
        if is_dev_origins_allowed():
            return canonical

    # 4. Exact match against development origins only if explicitly permitted
    if is_dev_origins_allowed() and canonical in DEV_ORIGINS:
        return canonical

    return None


def error_response(code: str, status: int = 400, retry_after: Optional[int] = None) -> web.Response:
    err_dict: Dict[str, Any] = {"code": code}
    if retry_after is not None:
        err_dict["retry_after"] = int(retry_after)

    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))

    return web.json_response({"error": err_dict}, status=status, headers=headers if headers else None)


def get_client_ip(request: web.Request) -> str:
    # Do not trust X-Forwarded-For unless FOCUS_TRUST_PROXY=1
    if os.getenv("FOCUS_TRUST_PROXY") == "1":
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote or "127.0.0.1"


def is_allowed_host(host: str) -> bool:
    if not host:
        return False
    host_clean = host.strip().lower()
    if host_clean in get_allowed_hosts():
        return True
    # Allow any loopback port on 127.0.0.1, localhost, or [::1] (e.g. ephemeral test servers)
    if host_clean.startswith("127.0.0.1:") or host_clean.startswith("localhost:") or host_clean.startswith("[::1]:"):
        return True
    if host_clean in ("127.0.0.1", "localhost", "[::1]"):
        return True
    return False


@web.middleware
async def host_validation_middleware(request: web.Request, handler):
    # Host header check against DNS rebinding attacks
    host = request.headers.get("Host", "")
    if not is_allowed_host(host):
        logger.warning(f"[Security] Host header rejected: '{host}'")
        return web.Response(status=400, text="Bad Request: Invalid Host Header")
    return await handler(request)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin")
    allow_origin = validate_origin(origin) if origin else None

    if request.method == "OPTIONS":
        # If an Origin header was provided but rejected, deny the preflight
        if origin and not allow_origin:
            return web.Response(status=403, text="Forbidden CORS Origin")

        resp = web.Response(status=204)
        if allow_origin:
            resp.headers["Access-Control-Allow-Origin"] = allow_origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
            resp.headers["Access-Control-Max-Age"] = "86400"
            resp.headers["Vary"] = "Origin"
        return resp

    try:
        response = await handler(request)
    except web.HTTPException as ex:
        # Wrap HTTPException if not already JSON
        if ex.status == 413:
            response = error_response("too_large", status=413)
        else:
            response = error_response("invalid_request", status=ex.status)
    except StorageUnavailableError:
        response = error_response("storage_unavailable", status=503)
    except RateLimitExceeded as rle:
        response = error_response("rate_limited", status=429, retry_after=rle.retry_after)
    except CooldownActiveError as cda:
        response = error_response("cooldown", status=429, retry_after=cda.retry_after)
    except AuthError as ae:
        response = error_response(ae.code, status=ae.status)
    except ChallengeNotActiveError as cna:
        response = error_response(cna.code, status=cna.status)
    except ChallengeError as ce:
        response = error_response(ce.code, status=ce.status, retry_after=ce.retry_after)
    except ValueError as ve:
        logger.debug(f"[API] ValueError: {ve}")
        response = error_response("invalid_request", status=400)
    except Exception as e:
        logger.error(f"[API] Unhandled server error: {e}", exc_info=True)
        response = error_response("internal", status=500)

    if allow_origin and response is not None:
        response.headers["Access-Control-Allow-Origin"] = allow_origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
        response.headers["Vary"] = "Origin"

    return response


class FocusApiHandler:
    def __init__(self, service: FocusService):
        self.service = service
        self.pair_sessions: Dict[str, Dict[str, Any]] = {}

    async def _parse_json(self, request: web.Request) -> Dict[str, Any]:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("JSON payload must be a dictionary object.")
            return body
        except web.HTTPException:
            raise
        except Exception:
            raise ValueError("Malformed or invalid JSON body.")

    async def _authenticate(self, request: web.Request) -> Dict[str, Any]:
        auth_header = request.headers.get("Authorization")
        return await self.service.auth.authenticate_token(auth_header)

    async def handle_pair_session_create(self, request: web.Request) -> web.Response:
        now = time.time()
        # Prune expired sessions (> 10 mins)
        self.pair_sessions = {k: v for k, v in self.pair_sessions.items() if v.get("expires_at", 0) > now}

        data = {}
        try:
            data = await self._parse_json(request)
        except Exception:
            pass

        session_id = secrets.token_hex(16)
        platform = data.get("platform", "desktop")
        default_name = data.get("device_name", "DeskFlow Client")
        proto = request.headers.get("X-Forwarded-Proto") or request.url.scheme
        host = request.headers.get("Host") or request.url.host
        server_url = f"{proto}://{host}"

        qr_payload = f"deskflow://pair?session={session_id}&server={server_url}&name={default_name}&platform={platform}"
        qr_image_url = f"{server_url}/api/focus/pair/session/{session_id}/qr.png"
        qr_data_url = None
        qr_png_bytes = None

        try:
            import qrcode
            import io
            import base64
            qr = qrcode.QRCode(box_size=6, border=2)
            qr.add_data(qr_payload)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            qr_png_bytes = buf.getvalue()
            b64 = base64.b64encode(qr_png_bytes).decode("ascii")
            qr_data_url = f"data:image/png;base64,{b64}"
        except Exception as e:
            logger.warning(f"[FocusApi] Failed to generate in-memory QR: {e}")

        self.pair_sessions[session_id] = {
            "status": "pending",
            "created_at": now,
            "expires_at": now + 600,
            "platform": platform,
            "default_name": default_name,
            "server_url": server_url,
            "qr_payload": qr_payload,
            "qr_png_bytes": qr_png_bytes,
            "qr_data_url": qr_data_url,
            "qr_image_url": qr_image_url
        }

        return web.json_response({
            "success": True,
            "session_id": session_id,
            "qr_payload": qr_payload,
            "qr_image_url": qr_image_url,
            "qr_data_url": qr_data_url,
            "expires_in": 600
        })

    async def handle_pair_session_qr(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        now = time.time()
        sess = self.pair_sessions.get(session_id)
        if not sess or sess.get("expires_at", 0) < now:
            return error_response("session_not_found_or_expired", status=404)

        png_bytes = sess.get("qr_png_bytes")
        if not png_bytes and sess.get("qr_payload"):
            try:
                import qrcode
                import io
                qr = qrcode.QRCode(box_size=6, border=2)
                qr.add_data(sess["qr_payload"])
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                png_bytes = buf.getvalue()
                sess["qr_png_bytes"] = png_bytes
            except Exception as e:
                logger.error(f"[FocusApi] QR generation error: {e}")
                png_bytes = None

        if not png_bytes:
            return error_response("qr_generation_failed", status=500)

        return web.Response(
            body=png_bytes,
            content_type="image/png",
            headers={
                "Cache-Control": "public, max-age=600",
                "Access-Control-Allow-Origin": "*"
            }
        )

    async def handle_pair_session_status(self, request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "").strip()
        now = time.time()
        sess = self.pair_sessions.get(session_id)
        if not sess or sess.get("expires_at", 0) < now:
            return error_response("session_not_found_or_expired", status=404)

        status = sess.get("status", "pending")
        if status == "approved":
            return web.json_response({
                "success": True,
                "status": "approved",
                "device_id": sess["device_id"],
                "device_token": sess["device_token"],
                "device_name": sess["device_name"],
                "scopes": sess.get("scopes", ["focus", "control", "spotify"]),
                "paired_at": sess.get("paired_at"),
                "focus_active": self.service.get_public_status()["enabled"]
            })
        elif status == "rejected":
            return web.json_response({
                "success": True,
                "status": "rejected"
            })
        else:
            return web.json_response({
                "success": True,
                "status": "pending"
            })

    async def handle_pair_session_approve(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        session_id = request.match_info.get("session_id", "").strip()
        now = time.time()
        sess = self.pair_sessions.get(session_id)
        if not sess or sess.get("expires_at", 0) < now:
            return error_response("session_not_found_or_expired", status=404)

        if sess.get("status") != "pending":
            return error_response("session_already_processed", status=409)

        data = await self._parse_json(request)
        custom_name = data.get("device_name") or sess.get("default_name") or "DeskFlow Desktop"
        platform = data.get("platform") or sess.get("platform") or "desktop"
        scopes = data.get("scopes", ["focus", "control", "spotify"])

        reg = await self.service.register_authorized_device(
            device_name=str(custom_name),
            platform=str(platform),
            scopes=scopes
        )

        sess["status"] = "approved"
        sess["device_id"] = reg["device_id"]
        sess["device_token"] = reg["device_token"]
        sess["device_name"] = reg["device_name"]
        sess["scopes"] = reg["scopes"]
        sess["paired_at"] = reg["paired_at"]

        return web.json_response({
            "success": True,
            "device_id": reg["device_id"],
            "device_name": reg["device_name"]
        })

    async def handle_pair_session_reject(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        session_id = request.match_info.get("session_id", "").strip()
        sess = self.pair_sessions.get(session_id)
        if sess:
            sess["status"] = "rejected"
        return web.json_response({"success": True})

    async def handle_pair(self, request: web.Request) -> web.Response:
        data = await self._parse_json(request)
        code = data.get("code") or data.get("pairing_code")
        if not code or not isinstance(code, str):
            return error_response("invalid_request", status=400)

        device_name = data.get("device_name", "Browser Extension")
        platform = data.get("platform") or data.get("device_type") or data.get("browser") or "chromium"

        client_ip = get_client_ip(request)
        reg = await self.service.pair_device(
            code=code,
            device_name=str(device_name),
            platform=str(platform),
            client_ip=client_ip
        )
        return web.json_response({
            "success": True,
            "device_id": reg["device_id"],
            "device_token": reg["device_token"],
            "device_name": reg["device_name"],
            "platform": reg["platform"],
            "scopes": reg.get("scopes", ["focus"]),
            "expires_at": reg["expires_at"],
            "paired_at": reg["paired_at"],
            "focus_active": self.service.get_public_status()["enabled"]
        })

    async def handle_status(self, request: web.Request) -> web.Response:
        device = await self._authenticate(request)
        status = self.service.get_detailed_status(device.get("device_id"))
        return web.json_response(status)

    async def handle_session(self, request: web.Request) -> web.Response:
        # Detailed status (authenticated)
        device = await self._authenticate(request)
        status = self.service.get_detailed_status(device.get("device_id"))
        return web.json_response(status)

    async def handle_history(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        history_data = self.service.get_history()
        return web.json_response(history_data)

    async def handle_enable(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)

        # Clients used to send {"enabled": false} here to stop a session. This
        # endpoint only ever STARTS one, so that request silently created a new
        # session and re-locked the desktop clients. Refuse it explicitly.
        if data.get("enabled") is False:
            return error_response("challenge_required", status=409)

        language = data.get("language", "en")
        if language not in ["en", "vi"]:
            language = "vi"

        session_type = data.get("session_type", "focus")
        dur = data.get("duration_minutes")
        brk = data.get("break_minutes", 5)
        reason = data.get("reason")

        try:
            dur_int = int(dur) if dur is not None else 25
        except (ValueError, TypeError):
            dur_int = 25

        try:
            brk_int = int(brk) if brk is not None else 5
        except (ValueError, TypeError):
            brk_int = 5

        status = await self.service.enable(
            duration_minutes=dur_int,
            break_minutes=brk_int,
            reason=reason,
            language=language,
            session_type=session_type
        )
        return web.json_response(status)

    async def handle_classify_url(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        url = data.get("url")
        if not url or not isinstance(url, str):
            return error_response("invalid_request", status=400)

        language = data.get("language", "en")
        title = data.get("title")

        verdict = await self.service.classify_url(url=url, language=language, title=title)
        return web.json_response(verdict)

    async def handle_challenge(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except ValueError:
                data = {}

        language = data.get("language", "en")
        force_new = bool(data.get("force_new", False))

        ch = await self.service.request_challenge(language=language, force_new=force_new)
        return web.json_response(ch)

    async def handle_verify(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        data = await self._parse_json(request)
        challenge_id = data.get("challenge_id")
        answer = data.get("answer")

        if not challenge_id or not isinstance(challenge_id, str):
            return error_response("invalid_request", status=400)

        if answer is None:
            return error_response("invalid_request", status=400)

        try:
            int_answer = int(answer)
        except (ValueError, TypeError):
            return error_response("invalid_request", status=400)

        result = await self.service.verify_challenge(challenge_id=challenge_id, answer=int_answer)
        return web.json_response(result)

    async def handle_devices(self, request: web.Request) -> web.Response:
        device = await self._authenticate(request)
        devices = self.service.get_paired_devices(current_device_id=device.get("device_id"))
        return web.json_response({
            "success": True,
            "devices": devices
        })

    async def handle_unpair(self, request: web.Request) -> web.Response:
        current_device = await self._authenticate(request)
        data = {}
        if request.can_read_body:
            try:
                data = await self._parse_json(request)
            except ValueError:
                data = {}

        current_device_id = current_device.get("device_id")
        requested_device_id = data.get("device_id")
        if requested_device_id and requested_device_id != current_device_id:
            caller_scopes = current_device.get("scopes") or []
            if "control" not in caller_scopes:
                return error_response("forbidden", status=403)
        target_device_id = requested_device_id or current_device_id
        revoked = await self.service.unpair_device(target_device_id)
        return web.json_response({
            "success": True,
            "device_id": target_device_id,
            "revoked": revoked
        })

    async def handle_device_delete(self, request: web.Request) -> web.Response:
        current_device = await self._authenticate(request)
        target_device_id = request.match_info.get("device_id") or request.query.get("device_id")
        if not target_device_id:
            return error_response("device_id_required", status=400)
        caller_scopes = current_device.get("scopes") or []
        if target_device_id != current_device.get("device_id"):
            if "control" not in caller_scopes and "focus" not in caller_scopes:
                return error_response("forbidden", status=403)
        deleted = await self.service.delete_device(target_device_id)
        return web.json_response({
            "success": True,
            "device_id": target_device_id,
            "deleted": deleted
        })

    async def handle_device_patch(self, request: web.Request) -> web.Response:
        """PATCH /api/devices/{device_id} {"name": "..."} - authenticated rename."""
        await self._authenticate(request)
        data = await self._parse_json(request)
        target_id = request.match_info.get("device_id", "")
        name = data.get("name")
        if not isinstance(name, str):
            return error_response("name_required", status=400)
        name = " ".join(name.split())
        if not name:
            return error_response("name_required", status=400)
        if len(name) > 64:
            return error_response("name_too_long", status=400)
        if not await self.service.rename_device(target_id, name):
            return error_response("device_not_found", status=404)
        try:
            from station_control import station_control
            station_control.rename_device(target_id, name)
        except Exception:
            pass
        return web.json_response({"success": True, "device_id": target_id, "device_name": name})

    async def handle_device_rename(self, request: web.Request) -> web.Response:
        try:
            current_device = await self._authenticate(request)
        except Exception:
            current_device = {}
        data = await self._parse_json(request)
        target_id = request.match_info.get("device_id") or data.get("device_id") or current_device.get("device_id")
        new_name = str(data.get("name") or data.get("device_name") or "").strip()
        if not target_id or not new_name:
            return error_response("name_required", status=400)
        success = await self.service.rename_device(target_id, new_name)
        try:
            from station_control import station_control
            station_control.rename_device(target_id, new_name)
        except Exception:
            pass
        return web.json_response({
            "success": True,
            "device_id": target_id,
            "device_name": new_name
        })


def create_app(service: FocusService) -> web.Application:
    # 16 KB body limit
    app = web.Application(client_max_size=16384, middlewares=[host_validation_middleware, cors_middleware])
    handler = FocusApiHandler(service)

    async def options_handler(request: web.Request) -> web.Response:
        return web.Response(status=204)

    app.router.add_post("/api/focus/pair", handler.handle_pair)
    app.router.add_post("/api/focus/pair/session/create", handler.handle_pair_session_create)
    app.router.add_get("/api/focus/pair/session/{session_id}", handler.handle_pair_session_status)
    app.router.add_get("/api/focus/pair/session/{session_id}/qr.png", handler.handle_pair_session_qr)
    app.router.add_get("/api/focus/pair/session/{session_id}/qr", handler.handle_pair_session_qr)
    app.router.add_post("/api/focus/pair/session/{session_id}/approve", handler.handle_pair_session_approve)
    app.router.add_post("/api/focus/pair/session/{session_id}/reject", handler.handle_pair_session_reject)
    app.router.add_get("/api/focus/status", handler.handle_status)
    app.router.add_get("/api/focus/session", handler.handle_session)
    app.router.add_get("/api/focus/history", handler.handle_history)
    app.router.add_post("/api/focus/enable", handler.handle_enable)
    app.router.add_post("/api/focus/classify-url", handler.handle_classify_url)
    app.router.add_post("/api/focus/challenge", handler.handle_challenge)
    app.router.add_post("/api/focus/verify", handler.handle_verify)
    app.router.add_get("/api/focus/devices", handler.handle_devices)
    app.router.add_post("/api/focus/devices/rename", handler.handle_device_rename)
    app.router.add_post("/api/focus/devices/{device_id}/rename", handler.handle_device_rename)
    app.router.add_patch("/api/devices/{device_id}", handler.handle_device_patch)
    app.router.add_delete("/api/focus/devices/{device_id}", handler.handle_device_delete)
    app.router.add_post("/api/focus/devices/{device_id}/delete", handler.handle_device_delete)
    app.router.add_route("OPTIONS", "/api/devices/{tail:.*}", options_handler)
    app.router.add_post("/api/focus/unpair", handler.handle_unpair)

    # Preflight options route
    app.router.add_route("OPTIONS", "/api/focus/{tail:.*}", options_handler)

    # Register unified app control & spotify routes
    from .app_routes import register_app_routes
    register_app_routes(app, service.auth)

    return app
