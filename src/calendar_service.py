"""
DeskFlow Google Calendar Service
Supports dual connection modes:
1. Google OAuth 2.0 (Calendar API v3): 2-way read/create/delete & calendar selection.
2. Private iCal (.ics) URL: zero-OAuth read-only sync using built-in RFC 5545 parser.

Caches normalized events locally and exposes compact summaries for the ESP32 station.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("DeskFlow.Calendar")

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DEFAULT_REDIRECT_URI = "https://10th012013.duckdns.org/api/app/calendar/oauth/callback"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_SCOPES = "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/userinfo.email"

# Common Windows timezone aliases found in some ICS feeds
_WINDOWS_TZ_MAP = {
    "SE Asia Standard Time": "Asia/Ho_Chi_Minh",
    "UTC": "UTC",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Pacific Standard Time": "America/Los_Angeles",
    "Eastern Standard Time": "America/New_York",
}

_WEEKDAY_MAP = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _resolve_tz(tzid: Optional[str]) -> timezone | ZoneInfo:
    if not tzid:
        return VN_TZ
    clean = tzid.strip().strip('"')
    if clean in _WINDOWS_TZ_MAP:
        clean = _WINDOWS_TZ_MAP[clean]
    try:
        return ZoneInfo(clean)
    except Exception:
        return VN_TZ


def _unescape_ics_text(val: str) -> str:
    return (
        val.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _unfold_ics_lines(raw_text: str) -> List[str]:
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    unfolded: List[str] = []
    for line in normalized.split("\n"):
        if (line.startswith(" ") or line.startswith("\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ics_dt(prop_params: str, value: str) -> Tuple[Optional[datetime], bool]:
    """Parses ICS date or datetime into VN_TZ aware datetime + is_all_day flag."""
    val = value.strip()
    is_date = "VALUE=DATE" in prop_params.upper() or (len(val) == 8 and val.isdigit())
    tzid_match = re.search(r"TZID=([^;:]+)", prop_params, re.IGNORECASE)
    tzid = tzid_match.group(1) if tzid_match else None

    if is_date:
        try:
            d = datetime.strptime(val[:8], "%Y%m%d").date()
            return datetime.combine(d, dt_time.min, tzinfo=VN_TZ), True
        except ValueError:
            return None, False

    if val.endswith("Z"):
        try:
            dt = datetime.strptime(val[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            return dt.astimezone(VN_TZ), False
        except ValueError:
            return None, False

    try:
        naive = datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
        tz = _resolve_tz(tzid)
        return naive.replace(tzinfo=tz).astimezone(VN_TZ), False
    except ValueError:
        return None, False


def _parse_ics_rrule(rrule_str: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in rrule_str.strip().split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip().upper()
    return out


def _expand_ics_occurrences(
    dt_start: datetime,
    duration: timedelta,
    rrule_str: str,
    exdates: List[date],
    win_start: datetime,
    win_end: datetime,
    all_day: bool,
) -> List[Tuple[datetime, datetime]]:
    if not rrule_str:
        return [(dt_start, dt_start + duration)]

    rules = _parse_ics_rrule(rrule_str)
    freq = rules.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [(dt_start, dt_start + duration)]

    try:
        interval = max(1, int(rules.get("INTERVAL", "1")))
    except ValueError:
        interval = 1

    max_count: Optional[int] = None
    if "COUNT" in rules:
        try:
            max_count = max(1, int(rules["COUNT"]))
        except ValueError:
            max_count = None

    until_dt: Optional[datetime] = None
    if "UNTIL" in rules:
        u_dt, _ = _parse_ics_dt("", rules["UNTIL"])
        if u_dt:
            until_dt = u_dt

    byday_nums: List[int] = []
    if "BYDAY" in rules:
        for tok in rules["BYDAY"].split(","):
            code = tok[-2:]
            if code in _WEEKDAY_MAP:
                byday_nums.append(_WEEKDAY_MAP[code])

    occurrences: List[Tuple[datetime, datetime]] = []
    ex_set = set(exdates)
    cur = dt_start
    generated = 0
    safety_limit = 2500

    # Fast-forward old infinite DAILY/WEEKLY series directly to near win_start
    if max_count is None and cur < (win_start - duration):
        if freq == "DAILY":
            step_days = interval
            skip_steps = max(0, int(((win_start - duration - cur).days) // step_days) - 1)
            cur = cur + timedelta(days=skip_steps * step_days)
        elif freq == "WEEKLY" and not byday_nums:
            step_days = 7 * interval
            skip_steps = max(0, int(((win_start - duration - cur).days) // step_days) - 1)
            cur = cur + timedelta(days=skip_steps * step_days)

    for _ in range(safety_limit):
        if until_dt and cur > until_dt:
            break
        if cur > win_end:
            break
        if max_count is not None and generated >= max_count:
            break

        candidate_dates: List[datetime] = []
        if freq == "WEEKLY" and byday_nums:
            week_monday = cur - timedelta(days=cur.weekday())
            for wd in sorted(set(byday_nums)):
                cand = week_monday + timedelta(days=wd)
                cand = cand.replace(hour=cur.hour, minute=cur.minute, second=cur.second)
                if cand >= dt_start:
                    candidate_dates.append(cand)
        else:
            candidate_dates.append(cur)

        for cand in candidate_dates:
            if until_dt and cand > until_dt:
                break
            if cand > win_end:
                break
            if max_count is not None and generated >= max_count:
                break
            generated += 1
            if cand.date() in ex_set:
                continue
            cand_end = cand + duration
            if cand_end >= win_start and cand <= win_end:
                occurrences.append((cand, cand_end))

        if freq == "DAILY":
            cur = cur + timedelta(days=interval)
        elif freq == "WEEKLY":
            cur = cur + timedelta(weeks=interval)
        elif freq == "MONTHLY":
            m = cur.month - 1 + interval
            y = cur.year + m // 12
            m = m % 12 + 1
            day = min(cur.day, 28)
            try:
                cur = cur.replace(year=y, month=m, day=cur.day)
            except ValueError:
                cur = cur.replace(year=y, month=m, day=day)
        elif freq == "YEARLY":
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:
                cur = cur.replace(year=cur.year + interval, day=28)

        if all_day and cur > win_end:
            break

    return occurrences


def parse_ics_events(raw_ics: str, win_start: datetime, win_end: datetime) -> List[Dict[str, Any]]:
    lines = _unfold_ics_lines(raw_ics)
    in_event = False
    cur: Dict[str, Any] = {}
    events: List[Dict[str, Any]] = []

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {"exdates": []}
            continue
        if line == "END:VEVENT":
            in_event = False
            if str(cur.get("status", "")).upper() == "CANCELLED":
                continue
            dt_start: Optional[datetime] = cur.get("dtstart")
            if not dt_start:
                continue
            all_day = bool(cur.get("all_day", False))
            dt_end: Optional[datetime] = cur.get("dtend")
            if not dt_end:
                dt_end = dt_start + (timedelta(days=1) if all_day else timedelta(hours=1))
            duration = max(timedelta(minutes=1), dt_end - dt_start)

            occs = _expand_ics_occurrences(
                dt_start=dt_start,
                duration=duration,
                rrule_str=cur.get("rrule", ""),
                exdates=cur.get("exdates", []),
                win_start=win_start,
                win_end=win_end,
                all_day=all_day,
            )
            uid = cur.get("uid", "") or f"ics_{int(dt_start.timestamp())}"
            title = cur.get("summary", "(Không có tiêu đề)") or "(Không có tiêu đề)"
            loc = cur.get("location", "")
            desc = cur.get("description", "")

            for idx, (o_start, o_end) in enumerate(occs):
                if o_end < win_start or o_start > win_end:
                    continue
                if all_day:
                    # ICS all-day end date is exclusive; display inclusive end if multi-day
                    ev_id = f"ics_{uid}_{o_start.strftime('%Y%m%d')}"
                else:
                    ev_id = f"ics_{uid}_{o_start.strftime('%Y%m%dT%H%M')}"
                events.append(
                    _normalize_event_dict(
                        event_id=ev_id,
                        uid=uid,
                        title=title,
                        start_dt=o_start,
                        end_dt=o_end,
                        all_day=all_day,
                        location=loc,
                        description=desc,
                        source="ical",
                        can_delete=False,
                    )
                )
            continue

        if not in_event or ":" not in line:
            continue

        prop_part, val_part = line.split(":", 1)
        if ";" in prop_part:
            prop_name, prop_params = prop_part.split(";", 1)
        else:
            prop_name, prop_params = prop_part, ""
        prop_name = prop_name.strip().upper()

        if prop_name == "UID":
            cur["uid"] = val_part.strip()
        elif prop_name == "SUMMARY":
            cur["summary"] = _unescape_ics_text(val_part)
        elif prop_name == "LOCATION":
            cur["location"] = _unescape_ics_text(val_part)
        elif prop_name == "DESCRIPTION":
            cur["description"] = _unescape_ics_text(val_part)
        elif prop_name == "STATUS":
            cur["status"] = val_part.strip()
        elif prop_name == "RRULE":
            cur["rrule"] = val_part.strip()
        elif prop_name == "DTSTART":
            dt, is_all = _parse_ics_dt(prop_params, val_part)
            cur["dtstart"] = dt
            cur["all_day"] = is_all
        elif prop_name == "DTEND":
            dt, _ = _parse_ics_dt(prop_params, val_part)
            cur["dtend"] = dt
        elif prop_name == "EXDATE":
            for raw_item in val_part.split(","):
                ex_dt, _ = _parse_ics_dt(prop_params, raw_item)
                if ex_dt:
                    cur["exdates"].append(ex_dt.date())

    return events


def _normalize_event_dict(
    event_id: str,
    title: str,
    start_dt: datetime,
    end_dt: datetime,
    all_day: bool,
    location: str = "",
    description: str = "",
    source: str = "google_oauth",
    can_delete: bool = False,
    calendar_id: str = "primary",
    html_link: str = "",
    uid: str = "",
) -> Dict[str, Any]:
    now = datetime.now(VN_TZ)
    today_date = now.date()
    ev_date = start_dt.date()

    if all_day:
        # Google Calendar uses exclusive DTEND for all-day events
        end_date_inclusive = (end_dt - timedelta(seconds=1)).date() if end_dt > start_dt else ev_date
        is_today = ev_date <= today_date <= end_date_inclusive
        is_ongoing = is_today
        is_upcoming = today_date <= end_date_inclusive
        start_time_str = "Cả ngày"
        end_time_str = ""
    else:
        is_today = ev_date == today_date
        is_ongoing = start_dt <= now <= end_dt
        is_upcoming = end_dt >= now
        start_time_str = start_dt.strftime("%H:%M")
        end_time_str = end_dt.strftime("%H:%M")

    return {
        "id": event_id,
        "uid": uid or event_id,
        "title": title,
        "date": ev_date.strftime("%Y-%m-%d"),
        "start_time": start_time_str,
        "end_time": end_time_str,
        "start_iso": start_dt.isoformat(),
        "end_iso": end_dt.isoformat(),
        "all_day": all_day,
        "is_today": is_today,
        "is_ongoing": is_ongoing,
        "is_upcoming": is_upcoming,
        "location": location,
        "description": description[:400] if description else "",
        "source": source,
        "can_delete": can_delete,
        "calendar_id": calendar_id,
        "html_link": html_link,
    }


def _recompute_event_flags(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Refreshes dynamic is_today / is_ongoing / is_upcoming flags against current clock."""
    now = datetime.now(VN_TZ)
    today_date = now.date()
    refreshed: List[Dict[str, Any]] = []
    for ev in events:
        item = dict(ev)
        try:
            start_dt = datetime.fromisoformat(item["start_iso"]).astimezone(VN_TZ)
            end_dt = datetime.fromisoformat(item["end_iso"]).astimezone(VN_TZ)
            if item.get("all_day"):
                end_inc = (end_dt - timedelta(seconds=1)).date() if end_dt > start_dt else start_dt.date()
                item["is_today"] = start_dt.date() <= today_date <= end_inc
                item["is_ongoing"] = item["is_today"]
                item["is_upcoming"] = today_date <= end_inc
            else:
                item["is_today"] = start_dt.date() == today_date
                item["is_ongoing"] = start_dt <= now <= end_dt
                item["is_upcoming"] = end_dt >= now
        except Exception:
            pass
        refreshed.append(item)
    return refreshed


class CalendarService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sync_async_lock = asyncio.Lock()
        self._data_dir = os.path.join(os.path.dirname(__file__), "data")
        self._config_file = os.path.join(self._data_dir, "calendar_config.json")
        self._oauth_states: Dict[str, float] = {}

        self.config: Dict[str, Any] = {
            "ical_url": "",
            "google_client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            "google_client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
            "google_redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip() or DEFAULT_REDIRECT_URI,
            "google_refresh_token": "",
            "google_access_token": "",
            "google_token_expires_at": 0,
            "google_connected_email": "",
            "selected_calendar_id": "primary",
            "selected_calendar_name": "Primary",
            "calendars": [],
            "last_sync_at": None,
            "last_error": None,
            "events_cache": [],
        }
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not os.path.exists(self._config_file):
                return
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    for k in self.config:
                        if k in saved and saved[k] is not None:
                            # Keep .env defaults if saved credentials are empty
                            if k in ("google_client_id", "google_client_secret") and not saved[k] and self.config[k]:
                                continue
                            self.config[k] = saved[k]
            except Exception as e:
                logger.warning(f"[Calendar] Failed to load calendar_config.json: {e}")

    def _save(self) -> None:
        with self._lock:
            os.makedirs(self._data_dir, exist_ok=True)
            tmp_path = f"{self._config_file}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._config_file)

    def get_public_status(self) -> Dict[str, Any]:
        with self._lock:
            events = _recompute_event_flags(self.config.get("events_cache", []))
            ical_url = str(self.config.get("ical_url", "") or "").strip()
            has_refresh = bool(self.config.get("google_refresh_token"))
            client_id = str(self.config.get("google_client_id", "") or "").strip()
            client_secret = str(self.config.get("google_client_secret", "") or "").strip()
            redirect_uri = str(self.config.get("google_redirect_uri", "") or DEFAULT_REDIRECT_URI).strip()

            return {
                "oauth_connected": has_refresh,
                "ical_connected": bool(ical_url),
                "connected": has_refresh or bool(ical_url),
                "ical_url": ical_url,
                "google_client_id": client_id,
                "has_google_client_secret": bool(client_secret),
                "oauth_configured": bool(client_id and client_secret),
                "google_redirect_uri": redirect_uri,
                "connected_email": self.config.get("google_connected_email", "") or "",
                "selected_calendar_id": self.config.get("selected_calendar_id", "primary") or "primary",
                "selected_calendar_name": self.config.get("selected_calendar_name", "Primary") or "Primary",
                "calendars": list(self.config.get("calendars", []) or []),
                "last_sync_at": self.config.get("last_sync_at"),
                "last_error": self.config.get("last_error"),
                "events": events,
                "today_count": sum(1 for e in events if e.get("is_today")),
                "upcoming_count": len(events),
            }

    def update_config(
        self,
        ical_url: Optional[str] = None,
        google_client_id: Optional[str] = None,
        google_client_secret: Optional[str] = None,
        google_redirect_uri: Optional[str] = None,
        selected_calendar_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if ical_url is not None:
                cleaned = ical_url.strip()
                if cleaned and not (cleaned.startswith("https://") or cleaned.startswith("http://")):
                    raise ValueError("ical_url must start with https:// or http://")
                self.config["ical_url"] = cleaned
            if google_client_id is not None:
                self.config["google_client_id"] = google_client_id.strip()
            if google_client_secret is not None:
                secret_clean = google_client_secret.strip()
                # Ignore placeholder mask if UI submits empty string only when explicitly clearing?
                # Empty string clears secret; non-empty updates it.
                self.config["google_client_secret"] = secret_clean
            if google_redirect_uri is not None:
                uri = google_redirect_uri.strip()
                self.config["google_redirect_uri"] = uri or DEFAULT_REDIRECT_URI
            if selected_calendar_id is not None:
                cal_id = selected_calendar_id.strip() or "primary"
                self.config["selected_calendar_id"] = cal_id
                cals = self.config.get("calendars", []) or []
                match = next((c for c in cals if c.get("id") == cal_id), None)
                if match:
                    self.config["selected_calendar_name"] = match.get("summary", cal_id)
            self.config["last_error"] = None
            self._save()
        return self.get_public_status()

    def build_oauth_start_url(self) -> Dict[str, Any]:
        with self._lock:
            client_id = str(self.config.get("google_client_id", "") or "").strip()
            client_secret = str(self.config.get("google_client_secret", "") or "").strip()
            redirect_uri = str(self.config.get("google_redirect_uri", "") or DEFAULT_REDIRECT_URI).strip()
            if not client_id or not client_secret:
                raise ValueError("google_oauth_credentials_missing")

            now = time.time()
            # Prune expired states (> 15 min)
            self._oauth_states = {k: v for k, v in self._oauth_states.items() if now - v < 900}
            state = secrets.token_urlsafe(24)
            self._oauth_states[state] = now

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return {
            "auth_url": f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}",
            "redirect_uri": redirect_uri,
            "state": state,
        }

    async def complete_oauth_callback(self, code: str, state: str) -> Dict[str, Any]:
        with self._lock:
            issued = self._oauth_states.pop(state, None)
            if not issued or (time.time() - issued) > 900:
                raise ValueError("invalid_or_expired_state")
            client_id = str(self.config.get("google_client_id", "") or "").strip()
            client_secret = str(self.config.get("google_client_secret", "") or "").strip()
            redirect_uri = str(self.config.get("google_redirect_uri", "") or DEFAULT_REDIRECT_URI).strip()
            if not client_id or not client_secret:
                raise ValueError("google_oauth_credentials_missing")

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:250]}")
            tok = resp.json()

            access_token = tok.get("access_token", "")
            refresh_token = tok.get("refresh_token", "")
            expires_in = int(tok.get("expires_in", 3600) or 3600)

            email = ""
            try:
                u_resp = await client.get(
                    GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if u_resp.status_code == 200:
                    email = u_resp.json().get("email", "")
            except Exception:
                pass

            calendars = await self._fetch_calendar_list(client, access_token)

        with self._lock:
            self.config["google_access_token"] = access_token
            if refresh_token:
                self.config["google_refresh_token"] = refresh_token
            self.config["google_token_expires_at"] = int(time.time()) + expires_in
            if email:
                self.config["google_connected_email"] = email
            if calendars:
                self.config["calendars"] = calendars
                cur_id = self.config.get("selected_calendar_id") or "primary"
                if not any(c["id"] == cur_id for c in calendars):
                    primary = next((c for c in calendars if c.get("primary")), calendars[0])
                    self.config["selected_calendar_id"] = primary["id"]
                    self.config["selected_calendar_name"] = primary["summary"]
            self.config["last_error"] = None
            self._save()

        await self.sync_events(push_to_station=True)
        return self.get_public_status()

    def disconnect_oauth(self) -> Dict[str, Any]:
        with self._lock:
            self.config["google_refresh_token"] = ""
            self.config["google_access_token"] = ""
            self.config["google_token_expires_at"] = 0
            self.config["google_connected_email"] = ""
            self.config["calendars"] = []
            self.config["selected_calendar_id"] = "primary"
            self.config["selected_calendar_name"] = "Primary"
            self.config["events_cache"] = [
                e for e in self.config.get("events_cache", []) if e.get("source") != "google_oauth"
            ]
            self.config["last_error"] = None
            self._save()
        return self.get_public_status()

    async def _ensure_access_token(self, client: httpx.AsyncClient) -> str:
        with self._lock:
            refresh_token = str(self.config.get("google_refresh_token", "") or "").strip()
            access_token = str(self.config.get("google_access_token", "") or "").strip()
            expires_at = int(self.config.get("google_token_expires_at", 0) or 0)
            client_id = str(self.config.get("google_client_id", "") or "").strip()
            client_secret = str(self.config.get("google_client_secret", "") or "").strip()

        if not refresh_token:
            raise ValueError("google_oauth_not_connected")
        if access_token and time.time() < expires_at - 60:
            return access_token
        if not client_id or not client_secret:
            raise ValueError("google_oauth_credentials_missing")

        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OAuth token refresh failed ({resp.status_code}): {resp.text[:200]}")
        tok = resp.json()
        new_token = tok.get("access_token", "")
        expires_in = int(tok.get("expires_in", 3600) or 3600)
        with self._lock:
            self.config["google_access_token"] = new_token
            self.config["google_token_expires_at"] = int(time.time()) + expires_in
            self._save()
        return new_token

    async def _fetch_calendar_list(self, client: httpx.AsyncClient, token: str) -> List[Dict[str, Any]]:
        try:
            resp = await client.get(
                f"{GOOGLE_API_BASE}/users/me/calendarList",
                headers={"Authorization": f"Bearer {token}"},
                params={"minAccessRole": "reader"},
            )
            if resp.status_code != 200:
                return []
            items = resp.json().get("items", []) or []
            return [
                {
                    "id": item.get("id", "primary"),
                    "summary": item.get("summaryOverride") or item.get("summary") or item.get("id", "Calendar"),
                    "primary": bool(item.get("primary", False)),
                    "access_role": item.get("accessRole", "reader"),
                    "background_color": item.get("backgroundColor", "#38BDF8"),
                }
                for item in items
            ]
        except Exception as e:
            logger.warning(f"[Calendar] Failed to list Google calendars: {e}")
            return []

    async def _fetch_google_oauth_events(
        self, client: httpx.AsyncClient, win_start: datetime, win_end: datetime
    ) -> List[Dict[str, Any]]:
        token = await self._ensure_access_token(client)
        calendars = await self._fetch_calendar_list(client, token)
        with self._lock:
            if calendars:
                self.config["calendars"] = calendars
            cal_id = str(self.config.get("selected_calendar_id", "primary") or "primary")
            match = next((c for c in (calendars or []) if c.get("id") == cal_id), None)
            if match:
                self.config["selected_calendar_name"] = match.get("summary", cal_id)
            can_write = (match.get("access_role") in ("owner", "writer")) if match else True

        encoded_cal = urllib.parse.quote(cal_id, safe="")
        resp = await client.get(
            f"{GOOGLE_API_BASE}/calendars/{encoded_cal}/events",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeMin": win_start.isoformat(),
                "timeMax": win_end.isoformat(),
                "maxResults": 100,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Google Calendar API error ({resp.status_code}): {resp.text[:200]}")

        out: List[Dict[str, Any]] = []
        for item in resp.json().get("items", []) or []:
            if item.get("status") == "cancelled":
                continue
            start_obj = item.get("start", {}) or {}
            end_obj = item.get("end", {}) or {}
            if "date" in start_obj:
                try:
                    sd = datetime.combine(date.fromisoformat(start_obj["date"]), dt_time.min, tzinfo=VN_TZ)
                    ed_raw = end_obj.get("date") or start_obj["date"]
                    ed = datetime.combine(date.fromisoformat(ed_raw), dt_time.min, tzinfo=VN_TZ)
                    all_day = True
                except Exception:
                    continue
            elif "dateTime" in start_obj:
                try:
                    sd = datetime.fromisoformat(start_obj["dateTime"].replace("Z", "+00:00")).astimezone(VN_TZ)
                    ed_str = end_obj.get("dateTime") or start_obj["dateTime"]
                    ed = datetime.fromisoformat(ed_str.replace("Z", "+00:00")).astimezone(VN_TZ)
                    all_day = False
                except Exception:
                    continue
            else:
                continue

            out.append(
                _normalize_event_dict(
                    event_id=str(item.get("id", "")),
                    uid=str(item.get("iCalUID") or item.get("id", "")),
                    title=str(item.get("summary") or "(Không có tiêu đề)"),
                    start_dt=sd,
                    end_dt=ed,
                    all_day=all_day,
                    location=str(item.get("location") or ""),
                    description=str(item.get("description") or ""),
                    source="google_oauth",
                    can_delete=can_write,
                    calendar_id=cal_id,
                    html_link=str(item.get("htmlLink") or ""),
                )
            )
        return out

    async def _fetch_ical_events(
        self, client: httpx.AsyncClient, ical_url: str, win_start: datetime, win_end: datetime
    ) -> List[Dict[str, Any]]:
        resp = await client.get(ical_url, follow_redirects=True)
        if resp.status_code != 200:
            raise RuntimeError(f"iCal fetch failed ({resp.status_code})")
        return parse_ics_events(resp.text, win_start, win_end)

    async def sync_events(self, days_ahead: int = 30, push_to_station: bool = True) -> Dict[str, Any]:
        async with self._sync_async_lock:
            now = datetime.now(VN_TZ)
            win_start = datetime.combine(now.date(), dt_time.min, tzinfo=VN_TZ)
            win_end = win_start + timedelta(days=max(1, min(days_ahead, 90)))

            with self._lock:
                has_oauth = bool(self.config.get("google_refresh_token"))
                ical_url = str(self.config.get("ical_url", "") or "").strip()

            if not has_oauth and not ical_url:
                with self._lock:
                    self.config["events_cache"] = []
                    self.config["last_error"] = None
                    self._save()
                if push_to_station:
                    self.push_to_station()
                return self.get_public_status()

            collected: List[Dict[str, Any]] = []
            errors: List[str] = []

            async with httpx.AsyncClient(timeout=20.0) as client:
                if has_oauth:
                    try:
                        collected.extend(await self._fetch_google_oauth_events(client, win_start, win_end))
                    except Exception as e:
                        logger.error(f"[Calendar] OAuth sync failed: {e}")
                        errors.append(f"OAuth: {e}")

                if ical_url:
                    try:
                        ical_events = await self._fetch_ical_events(client, ical_url, win_start, win_end)
                        # Deduplicate against OAuth events by UID+date or title+date+start
                        seen_keys = {
                            (
                                ev.get("uid", "").split("@")[0].lower(),
                                ev.get("date", ""),
                                ev.get("start_time", ""),
                            )
                            for ev in collected
                        }
                        seen_titles = {
                            (ev.get("title", "").strip().lower(), ev.get("date", ""), ev.get("start_time", ""))
                            for ev in collected
                        }
                        for ev in ical_events:
                            k_uid = (
                                ev.get("uid", "").split("@")[0].lower(),
                                ev.get("date", ""),
                                ev.get("start_time", ""),
                            )
                            k_title = (
                                ev.get("title", "").strip().lower(),
                                ev.get("date", ""),
                                ev.get("start_time", ""),
                            )
                            if k_uid not in seen_keys and k_title not in seen_titles:
                                collected.append(ev)
                    except Exception as e:
                        logger.error(f"[Calendar] iCal sync failed: {e}")
                        errors.append(f"iCal: {e}")

            # Filter expired events from earlier today only if finished > 2h ago? Keep all today + upcoming!
            collected.sort(key=lambda e: (e.get("date", ""), "00:00" if e.get("all_day") else e.get("start_time", "")))

            with self._lock:
                # If both failed and we had cached events, keep cache if transient error; if at least 1 succeeded, update cache
                if collected or not errors:
                    self.config["events_cache"] = collected[:150]
                self.config["last_sync_at"] = now.isoformat()
                self.config["last_error"] = "; ".join(errors) if errors else None
                self._save()

            if push_to_station:
                self.push_to_station()

            return self.get_public_status()

    async def create_event(
        self,
        title: str,
        date_str: str,
        start_time: str = "",
        end_time: str = "",
        all_day: bool = False,
        location: str = "",
        description: str = "",
        calendar_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("title_required")
        try:
            ev_date = date.fromisoformat(date_str.strip())
        except ValueError:
            raise ValueError("invalid_date_format")

        with self._lock:
            if not self.config.get("google_refresh_token"):
                raise PermissionError("google_oauth_required")
            target_cal = (calendar_id or self.config.get("selected_calendar_id") or "primary").strip()

        body: Dict[str, Any] = {
            "summary": clean_title,
        }
        if location.strip():
            body["location"] = location.strip()
        if description.strip():
            body["description"] = description.strip()

        if all_day or not start_time.strip():
            body["start"] = {"date": ev_date.isoformat()}
            body["end"] = {"date": (ev_date + timedelta(days=1)).isoformat()}
        else:
            st_parts = start_time.strip().split(":")
            if len(st_parts) < 2:
                raise ValueError("invalid_start_time")
            sh, sm = int(st_parts[0]), int(st_parts[1])
            start_dt = datetime.combine(ev_date, dt_time(sh, sm), tzinfo=VN_TZ)

            if end_time.strip():
                et_parts = end_time.strip().split(":")
                if len(et_parts) < 2:
                    raise ValueError("invalid_end_time")
                eh, em = int(et_parts[0]), int(et_parts[1])
                end_dt = datetime.combine(ev_date, dt_time(eh, em), tzinfo=VN_TZ)
                if end_dt <= start_dt:
                    end_dt = start_dt + timedelta(hours=1)
            else:
                end_dt = start_dt + timedelta(hours=1)

            body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"}
            body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Ho_Chi_Minh"}

        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._ensure_access_token(client)
            encoded_cal = urllib.parse.quote(target_cal, safe="")
            resp = await client.post(
                f"{GOOGLE_API_BASE}/calendars/{encoded_cal}/events",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Create event failed ({resp.status_code}): {resp.text[:220]}")
            created_raw = resp.json()

        status = await self.sync_events(push_to_station=True)
        return {"created_id": created_raw.get("id", ""), "status": status}

    async def delete_event(self, event_id: str, calendar_id: Optional[str] = None) -> Dict[str, Any]:
        clean_id = event_id.strip()
        if not clean_id:
            raise ValueError("event_id_required")
        with self._lock:
            if not self.config.get("google_refresh_token"):
                raise PermissionError("google_oauth_required")
            target_cal = (calendar_id or self.config.get("selected_calendar_id") or "primary").strip()

        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await self._ensure_access_token(client)
            encoded_cal = urllib.parse.quote(target_cal, safe="")
            encoded_ev = urllib.parse.quote(clean_id, safe="")
            resp = await client.delete(
                f"{GOOGLE_API_BASE}/calendars/{encoded_cal}/events/{encoded_ev}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code not in (200, 204):
                raise RuntimeError(f"Delete event failed ({resp.status_code}): {resp.text[:220]}")

        return await self.sync_events(push_to_station=True)

    def build_station_payload(self, days_ahead: int = 7, max_events_per_day: int = 5) -> Dict[str, Any]:
        """Compact day-by-day UTF-8 agenda payload pushed to the ESP32 TFT station."""
        status = self.get_public_status()
        all_events = status["events"]
        now_date = datetime.now(VN_TZ).date()

        vi_wd = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
        en_wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        def _event_spans_day(ev: Dict[str, Any], target_d: date) -> bool:
            try:
                sd = datetime.fromisoformat(ev["start_iso"]).astimezone(VN_TZ).date()
                ed = datetime.fromisoformat(ev["end_iso"]).astimezone(VN_TZ).date()
                if ev.get("all_day"):
                    end_inc = (datetime.fromisoformat(ev["end_iso"]).astimezone(VN_TZ) - timedelta(seconds=1)).date()
                    if end_inc < sd:
                        end_inc = sd
                    return sd <= target_d <= end_inc
                # Timed event: show on start date (or spanning dates)
                return sd <= target_d <= ed if (ed > sd and ev.get("end_time", "") != "00:00") else sd == target_d
            except Exception:
                return ev.get("date", "") == target_d.isoformat()

        days_out: List[Dict[str, Any]] = []
        compact_flat: List[Dict[str, Any]] = []

        for offset in range(max(1, min(days_ahead, 14))):
            d = now_date + timedelta(days=offset)
            wd_idx = d.weekday()
            d_dm = d.strftime("%d/%m")
            if offset == 0:
                lbl_vi = f"Hôm nay • {vi_wd[wd_idx]}, {d_dm}"
                lbl_en = f"Today • {en_wd[wd_idx]}, {d_dm}"
            elif offset == 1:
                lbl_vi = f"Ngày mai • {vi_wd[wd_idx]}, {d_dm}"
                lbl_en = f"Tomorrow • {en_wd[wd_idx]}, {d_dm}"
            else:
                lbl_vi = f"{vi_wd[wd_idx]}, {d_dm}"
                lbl_en = f"{en_wd[wd_idx]}, {d_dm}"

            day_events = [e for e in all_events if _event_spans_day(e, d)]
            ev_items: List[Dict[str, Any]] = []
            for ev in day_events[:max_events_per_day]:
                if ev.get("all_day"):
                    time_lbl = "Cả ngày"
                else:
                    st = ev.get("start_time", "")
                    et = ev.get("end_time", "")
                    time_lbl = f"{st}-{et}" if et else st

                item = {
                    "id": str(ev.get("id", ""))[:20],
                    "title": str(ev.get("title", ""))[:48],
                    "date": d.isoformat(),
                    "date_label": "Hôm nay" if offset == 0 else ("Ngày mai" if offset == 1 else d_dm),
                    "time_label": time_lbl,
                    "location": str(ev.get("location", ""))[:28],
                    "all_day": bool(ev.get("all_day", False)),
                    "is_today": (offset == 0),
                    "is_ongoing": bool(ev.get("is_ongoing", False)) and (offset == 0),
                }
                ev_items.append(item)
                if len(compact_flat) < 8:
                    compact_flat.append(item)

            days_out.append({
                "date": d.isoformat(),
                "day_label": lbl_vi,
                "day_label_en": lbl_en,
                "is_today": (offset == 0),
                "total": len(day_events),
                "events": ev_items,
            })

        return {
            "action": "sync_calendar",
            "connected": bool(status["connected"]),
            "today_count": int(status["today_count"]),
            "days": days_out,
            "events": compact_flat,
        }

    def push_to_station(self) -> bool:
        try:
            import station_control
            payload = self.build_station_payload()
            return station_control.station_control.mqtt_publish(
                "deskflow/display",
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning(f"[Calendar] Failed to push events to station: {e}")
            return False


def render_oauth_result_html(success: bool, email: str = "", error: str = "") -> str:
    if success:
        title = "Google Calendar Connected"
        headline = "Kết nối Google Calendar thành công!"
        body = (
            f"Đã liên kết tài khoản <b>{html.escape(email or 'Google')}</b> với trạm DeskFlow. "
            "Lịch sẽ tự động đồng bộ lên máy tính, điện thoại và màn hình ESP32. "
            "Bạn có thể đóng tab này ngay."
        )
        accent = "#22C55E"
    else:
        title = "Google Calendar Connection Failed"
        headline = "Không thể kết nối Google Calendar"
        body = f"Chi tiết lỗi: <code>{html.escape(error or 'unknown_error')}</code>"
        accent = "#EF4444"

    return f"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title}</title>
<style>
  body {{ background:#0B1120; color:#F8FAFC; font-family:system-ui,-apple-system,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:24px; }}
  .card {{ max-width:460px; background:#111827; border:1px solid #1F2937; border-radius:16px;
          padding:28px; box-shadow:0 20px 50px rgba(0,0,0,0.4); text-align:center; }}
  .dot {{ width:14px; height:14px; border-radius:50%; background:{accent}; display:inline-block; margin-bottom:12px; }}
  h1 {{ font-size:20px; margin:0 0 12px 0; }}
  p {{ font-size:14px; line-height:1.55; color:#CBD5E1; margin:0; }}
  code {{ background:#1E293B; padding:2px 6px; border-radius:6px; color:#FCA5A5; }}
</style>
</head>
<body>
  <div class="card">
    <div class="dot"></div>
    <h1>{headline}</h1>
    <p>{body}</p>
  </div>
</body>
</html>"""


calendar_service = CalendarService()
