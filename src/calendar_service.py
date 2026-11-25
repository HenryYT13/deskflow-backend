"""
DeskFlow Google Calendar Service
Supports dual connection modes:
1. Google OAuth 2.0 (Calendar API v3): 2-way read/create/delete & calendar selection.
2. Private iCal (.ics) URL: zero-OAuth read-only sync using built-in RFC 5545 parser.

Caches normalized events locally and exposes compact summaries for the ESP32 station.
"""

from __future__ import annotations

import asyncio
import calendar as py_calendar
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

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logger = logging.getLogger("DeskFlow.Calendar")

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DEFAULT_REDIRECT_URI = "https://df.api.thetechdude.io.vn/api/app/calendar/oauth/callback"
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
            "selected_calendar_ids": [],
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

    def is_oauth_connected(self) -> bool:
        with self._lock:
            return bool(self.config.get("google_refresh_token"))

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
                "selected_calendar_ids": list(self.config.get("selected_calendar_ids") or []),
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
        selected_calendar_ids: Optional[List[str]] = None,
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
            if selected_calendar_ids is not None:
                clean_ids = [str(c).strip() for c in selected_calendar_ids if str(c).strip()]
                self.config["selected_calendar_ids"] = clean_ids
                # Ensure the primary write calendar (selected_calendar_id) isn't stuck on a read-only calendar
                cals = self.config.get("calendars", []) or []
                cal_map = {c.get("id"): c for c in cals if isinstance(c, dict)}
                cur_id = str(self.config.get("selected_calendar_id") or "")
                cur_writable = cal_map.get(cur_id, {}).get("access_role") in ("owner", "writer")
                if not cur_writable or (clean_ids and cur_id not in clean_ids):
                    writable_pick = next(
                        (cid for cid in clean_ids if cal_map.get(cid, {}).get("access_role") in ("owner", "writer")),
                        None,
                    ) or next(
                        (c.get("id") for c in cals if c.get("access_role") in ("owner", "writer")),
                        "primary",
                    )
                    self.config["selected_calendar_id"] = writable_pick
                    if cal_map.get(writable_pick):
                        self.config["selected_calendar_name"] = cal_map[writable_pick].get("summary", writable_pick)
            elif selected_calendar_id is not None:
                # Single-select fallback: still preserve read-only protection if user picked primary/writable
                cal_id = selected_calendar_id.strip() or "primary"
                self.config["selected_calendar_id"] = cal_id
                cals = self.config.get("calendars", []) or []
                match = next((c for c in cals if c.get("id") == cal_id), None)
                if match:
                    self.config["selected_calendar_name"] = match.get("summary", cal_id)
            self.config["last_error"] = None
            self._save()
        return self.get_public_status()

    def _resolve_writable_calendar_id(self, requested_id: Optional[str] = None) -> str:
        """Picks a calendar with owner/writer access so create_event & AI sync never fail with 403 on holiday calendars."""
        cals = self.config.get("calendars", []) or []
        cal_map = {c.get("id"): c for c in cals if isinstance(c, dict)}

        def _is_writable(cid: Optional[str]) -> bool:
            if not cid:
                return False
            c = str(cid).strip()
            if c in cal_map:
                return cal_map[c].get("access_role") in ("owner", "writer")
            if "#holiday@" in c or "#contacts@" in c:
                return False
            return True

        candidates: List[Optional[str]] = [
            requested_id,
            self.config.get("selected_calendar_id"),
            *(self.config.get("selected_calendar_ids") or []),
            next((c.get("id") for c in cals if c.get("primary")), None),
            *(c.get("id") for c in cals),
        ]
        for cand in candidates:
            if cand and _is_writable(cand):
                return str(cand).strip()
        return "primary"

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
            cal_map_tmp = {c.get("id"): c for c in (calendars or []) if isinstance(c, dict)}
            cal_id = str(self.config.get("selected_calendar_id", "primary") or "primary")
            extra_ids = list(self.config.get("selected_calendar_ids") or [])
            if cal_map_tmp and cal_id in cal_map_tmp and cal_map_tmp[cal_id].get("access_role") not in ("owner", "writer"):
                writable_fallback = next(
                    (cid for cid in extra_ids if cal_map_tmp.get(cid, {}).get("access_role") in ("owner", "writer")),
                    None,
                ) or next(
                    (c.get("id") for c in calendars if c.get("access_role") in ("owner", "writer")),
                    cal_id,
                )
                cal_id = writable_fallback
                self.config["selected_calendar_id"] = cal_id
                if cal_map_tmp.get(cal_id):
                    self.config["selected_calendar_name"] = cal_map_tmp[cal_id].get("summary", cal_id)
                self._save()
            target_ids = extra_ids if extra_ids else [cal_id]
            if cal_id not in target_ids and not extra_ids:
                target_ids = [cal_id]

        PALETTE = ["#38BDF8", "#10B981", "#F59E0B", "#A855F7", "#F43F5E", "#14B8A6"]
        cal_lookup = {c.get("id"): c for c in (calendars or [])}
        out: List[Dict[str, Any]] = []

        for idx_c, target_id in enumerate(target_ids):
            match = cal_lookup.get(target_id)
            if match and target_id == cal_id:
                with self._lock:
                    self.config["selected_calendar_name"] = match.get("summary", target_id)
            can_write = (match.get("access_role") in ("owner", "writer")) if match else True
            cal_color = (match.get("background_color") if match else None) or PALETTE[idx_c % len(PALETTE)]
            cal_summary = (match.get("summary") if match else None) or target_id

            encoded_cal = urllib.parse.quote(target_id, safe="")
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
                if len(target_ids) == 1:
                    raise RuntimeError(f"Google Calendar API error ({resp.status_code}): {resp.text[:200]}")
                continue

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

                norm = _normalize_event_dict(
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
                    calendar_id=target_id,
                    html_link=str(item.get("htmlLink") or ""),
                )
                norm["color"] = cal_color
                norm["calendar_name"] = cal_summary
                out.append(norm)
        out.sort(key=lambda e: (e["start_iso"], e["title"].lower()))
        return out

    async def _fetch_ical_events(
        self, client: httpx.AsyncClient, ical_url: str, win_start: datetime, win_end: datetime
    ) -> List[Dict[str, Any]]:
        resp = await client.get(ical_url, follow_redirects=True)
        if resp.status_code != 200:
            raise RuntimeError(f"iCal fetch failed ({resp.status_code})")
        return parse_ics_events(resp.text, win_start, win_end)

    async def sync_events(self, days_ahead: int = 35, push_to_station: bool = True) -> Dict[str, Any]:
        async with self._sync_async_lock:
            now = datetime.now(VN_TZ)
            # Fetch from the 1st of the current month so full month-grid view has complete data
            month_start = now.date().replace(day=1)
            win_start = datetime.combine(month_start, dt_time.min, tzinfo=VN_TZ)
            # Always cover at least through the end of next month
            if now.month == 12:
                next_y, next_m = now.year + 1, 1
            else:
                next_y, next_m = now.year, now.month + 1
            next_last_day = py_calendar.monthrange(next_y, next_m)[1]
            min_end_date = date(next_y, next_m, next_last_day)
            target_end_date = max(min_end_date, now.date() + timedelta(days=max(1, min(days_ahead, 90))))
            win_end = datetime.combine(target_end_date, dt_time(23, 59, 59), tzinfo=VN_TZ)

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
                    self.config["events_cache"] = collected[:250]
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
        sync_after: bool = True,
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
            target_cal = self._resolve_writable_calendar_id(calendar_id)

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

        status = await self.sync_events(push_to_station=True) if sync_after else self.get_public_status()
        return {"created_id": created_raw.get("id", ""), "status": status}

    async def delete_event(self, event_id: str, calendar_id: Optional[str] = None, sync_after: bool = True) -> Dict[str, Any]:
        clean_id = event_id.strip()
        if not clean_id:
            raise ValueError("event_id_required")
        with self._lock:
            if not self.config.get("google_refresh_token"):
                raise PermissionError("google_oauth_required")
            target_cal = self._resolve_writable_calendar_id(calendar_id)

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

        return await self.sync_events(push_to_station=True) if sync_after else self.get_public_status()

    def build_station_payload(self, max_events: int = 90) -> Dict[str, Any]:
        """Compact 2-month Month-Grid + Day-Events UTF-8 payload pushed to ESP32 (< 7.5 KB)."""
        status = self.get_public_status()
        all_events = status["events"]
        now_date = datetime.now(VN_TZ).date()

        cur_y, cur_m = now_date.year, now_date.month
        cur_first = date(cur_y, cur_m, 1)
        cur_first_wd = (cur_first.weekday() + 1) % 7  # 0=Sun..6=Sat (matches Google Calendar)
        cur_dim = py_calendar.monthrange(cur_y, cur_m)[1]

        if cur_m == 12:
            nxt_y, nxt_m = cur_y + 1, 1
        else:
            nxt_y, nxt_m = cur_y, cur_m + 1
        nxt_first = date(nxt_y, nxt_m, 1)
        nxt_first_wd = (nxt_first.weekday() + 1) % 7
        nxt_dim = py_calendar.monthrange(nxt_y, nxt_m)[1]

        def _event_dates_in_window(ev: Dict[str, Any]) -> List[date]:
            try:
                sd = datetime.fromisoformat(ev["start_iso"]).astimezone(VN_TZ).date()
                ed = datetime.fromisoformat(ev["end_iso"]).astimezone(VN_TZ).date()
                if ev.get("all_day"):
                    end_inc = (datetime.fromisoformat(ev["end_iso"]).astimezone(VN_TZ) - timedelta(seconds=1)).date()
                    if end_inc < sd:
                        end_inc = sd
                else:
                    end_inc = ed if (ed > sd and ev.get("end_time", "") != "00:00") else sd
                dates: List[date] = []
                cur = max(sd, cur_first)
                limit_end = min(end_inc, date(nxt_y, nxt_m, nxt_dim))
                while cur <= limit_end and len(dates) < 31:
                    dates.append(cur)
                    cur += timedelta(days=1)
                return dates
            except Exception:
                try:
                    d_only = date.fromisoformat(ev.get("date", ""))
                    return [d_only]
                except Exception:
                    return []

        compact_events: List[Dict[str, Any]] = []
        for ev in all_events:
            if len(compact_events) >= max_events:
                break
            if ev.get("all_day"):
                time_lbl = "Cả ngày"
            else:
                st = ev.get("start_time", "")
                et = ev.get("end_time", "")
                time_lbl = f"{st}-{et}" if et else st

            title = str(ev.get("title", ""))[:38]
            loc = str(ev.get("location", ""))[:24]

            for d_obj in _event_dates_in_window(ev):
                if (d_obj.year, d_obj.month) not in ((cur_y, cur_m), (nxt_y, nxt_m)):
                    continue
                compact_events.append({
                    "y": d_obj.year,
                    "m": d_obj.month,
                    "d": d_obj.day,
                    "t": time_lbl,
                    "s": title,
                    "l": loc,
                })
                if len(compact_events) >= max_events:
                    break

        payload: Dict[str, Any] = {
            "action": "sync_calendar",
            "connected": bool(status["connected"]),
            "today_count": int(status["today_count"]),
            "year": cur_y,
            "month": cur_m,
            "today": now_date.day,
            "first_wd": cur_first_wd,
            "days_in_month": cur_dim,
            "next_year": nxt_y,
            "next_month": nxt_m,
            "next_first_wd": nxt_first_wd,
            "next_days_in_month": nxt_dim,
            "events": compact_events,
        }

        # Ensure MQTT packet never exceeds ESP32's 8192-byte buffer
        while len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 7200 and payload["events"]:
            payload["events"].pop()

        return payload

    async def ai_sync_tkb_to_calendar(
        self,
        week_offset: int = 0,
        use_ai: bool = True,
    ) -> Dict[str, Any]:
        """Reads current PocketBase timetable and creates deduplicated Google Calendar events for target week."""
        if not self.is_oauth_connected():
            raise PermissionError("oauth_not_connected")

        import timetable
        import config
        import station_control as sc_mod

        raw_tkb = await timetable.fetch_current_weekly_schedule()
        sched_data = raw_tkb.get("schedule_data") or {}
        if not sched_data:
            raise ValueError("timetable_empty")

        now_vn = datetime.now(VN_TZ).date()
        this_monday = now_vn - timedelta(days=now_vn.weekday())
        target_monday = this_monday + timedelta(days=7 * int(week_offset))

        days_vn = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7"]
        parsed_days: List[Dict[str, Any]] = []
        for idx, day_name in enumerate(days_vn):
            d_date = target_monday + timedelta(days=idx)
            info = timetable.parse_day_data(sched_data, day_name)
            if not info.get("has_any_class"):
                continue
            parsed_days.append({
                "date": d_date.isoformat(),
                "day": day_name,
                "morning": info.get("morning") or [],
                "morning_slots": info.get("morning_slots") or [],
                "afternoon": info.get("afternoon") or [],
                "afternoon_slots": info.get("afternoon_slots") or [],
                "has_afternoon": bool(info.get("has_afternoon")),
                "uniform": info.get("dp_sang") or "Tự do",
                "uniform_afternoon": info.get("dp_chieu") or "",
                "dismissal_morning": info.get("dismissal_morning") or "",
                "dismissal_afternoon": info.get("dismissal_afternoon") or "",
            })

        ai_summaries: Dict[str, Dict[str, str]] = {}
        groq_key = getattr(config, "GROQ_API_KEY", "") or os.getenv("GROQ_API_KEY", "")
        if use_ai and groq_key and parsed_days:
            try:
                from groq import Groq
                prompt = (
                    "Bạn là trợ lý học tập. Dựa vào TKB JSON bên dưới, hãy viết tóm tắt ngắn gọn cho từng ngày (<=60 ký tự) "
                    "và 1 câu nhắc chuẩn bị bài/đồng phục (<=100 ký tự). Trả về đúng JSON map theo key `date`: "
                    '{"YYYY-MM-DD": {"morning_title": "...", "afternoon_title": "...", "tip": "..."}}. '
                    f"TKB: {json.dumps(parsed_days, ensure_ascii=False)}"
                )
                def _ask():
                    client = Groq(api_key=groq_key)
                    resp = client.chat.completions.create(
                        model="openai/gpt-oss-20b",
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3,
                        max_completion_tokens=1024,
                        reasoning_effort="low",
                        response_format={"type": "json_object"},
                    )
                    return resp.choices[0].message.content
                raw_json = await asyncio.to_thread(_ask)
                loaded = json.loads(raw_json or "{}")
                if isinstance(loaded, dict):
                    ai_summaries = loaded
            except Exception as e:
                logger.warning(f"[Calendar AI TKB] Groq formatting fallback: {e}")

        # Refresh existing events to deduplicate cleanly & remove legacy coarse blocks
        await self.sync_events(push_to_station=False)
        existing_tags = set()
        target_dates = {item["date"] for item in parsed_days}
        for ev in list(self.config.get("events_cache") or []):
            desc = str(ev.get("description") or "")
            tags = re.findall(r"\[DF-TKB:[^\]]+\]", desc)
            for m in tags:
                existing_tags.add(m)
            # Clean up old coarse blocks ([DF-TKB:YYYY-MM-DD:M] or :A without slot number)
            if ev.get("can_delete") and ev.get("id"):
                for m in tags:
                    if re.match(r"^\[DF-TKB:\d{4}-\d{2}-\d{2}:[MA]\]$", m) and ev.get("date") in target_dates:
                        try:
                            await self.delete_event(ev["id"], calendar_id=ev.get("calendar_id"), sync_after=False)
                        except Exception:
                            pass

        created = 0
        skipped = 0
        week_num = raw_tkb.get("week_number", "?")
        for item in parsed_days:
            d_iso = item["date"]
            ai_d = ai_summaries.get(d_iso) if isinstance(ai_summaries.get(d_iso), dict) else {}
            tip = str(ai_d.get("tip") or "").strip()

            sessions = [
                ("M", "Sáng", item.get("morning_slots") or [], item["uniform"], item.get("dismissal_morning") or "11:35"),
                ("A", "Chiều", item.get("afternoon_slots") or [], item.get("uniform_afternoon") or item["uniform"], item.get("dismissal_afternoon") or "16:25"),
            ]
            for sess_code, sess_lbl, slots, uni_txt, dismiss_str in sessions:
                for slot in slots:
                    p_num = slot.get("period", 1)
                    subj = slot.get("subject", "")
                    st_t = slot.get("start_time") or ("07:15" if sess_code == "M" else "14:00")
                    en_t = slot.get("end_time") or ("08:00" if sess_code == "M" else "14:45")
                    tag = f"[DF-TKB:{d_iso}:{sess_code}{p_num}]"
                    if tag in existing_tags:
                        skipped += 1
                        continue
                    title = (
                        f"Tiết {p_num}: {subj}"
                        if sess_code == "M"
                        else f"Chiều Tiết {p_num}: {subj}"
                    )[:95]
                    bell_note = (
                        f"Có mặt 06:30–07:00 • Đầu giờ 07:00–07:15 • Ra về {dismiss_str}"
                        if sess_code == "M"
                        else f"Mở cổng 13:30 • Có mặt 13:50 • Ra về {dismiss_str}"
                    )
                    desc = f"{tag}\nTuần {week_num} ({sess_lbl}) • Đồng phục: {uni_txt}\n{bell_note}"
                    if tip:
                        desc += f"\nGợi ý AI: {tip}"
                    await self.create_event(
                        title=title,
                        date_str=d_iso,
                        start_time=st_t,
                        end_time=en_t,
                        all_day=False,
                        location="Trường học",
                        description=desc,
                        sync_after=False,
                    )
                    created += 1

        await self.sync_events(push_to_station=True)
        try:
            sc_mod.station_control.push_notification(
                "tkb_sync",
                "AI Auto-Sync TKB → Calendar",
                f"Đã tạo {created} buổi học mới (bỏ qua {skipped} trùng) cho tuần {target_monday.isoformat()}.",
                "default",
            )
        except Exception:
            pass
        return {
            "success": True,
            "week_monday": target_monday.isoformat(),
            "week_number": week_num,
            "created": created,
            "skipped": skipped,
            "ai_enhanced": bool(ai_summaries),
        }

    async def ai_quick_add(self, natural_text: str) -> Dict[str, Any]:
        """#11 Parse natural Vietnamese/English text via Groq AI and create a Google Calendar event."""
        if not self.is_oauth_connected():
            raise PermissionError("oauth_not_connected")
        clean_in = (natural_text or "").strip()
        if not clean_in:
            raise ValueError("text_required")
        import config
        groq_key = getattr(config, "GROQ_API_KEY", "") or os.getenv("GROQ_API_KEY", "")
        if not groq_key:
            raise ValueError("groq_api_key_missing")

        now_vn = datetime.now(VN_TZ)
        wd_names = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ Nhật"]
        prompt = (
            f"Hôm nay là {wd_names[now_vn.weekday()]} ngày {now_vn.date().isoformat()}, giờ hiện tại {now_vn.strftime('%H:%M')} (GMT+7).\n"
            "Hãy phân tích câu sau thành sự kiện Google Calendar dưới dạng JSON:\n"
            '{"title": "...", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "all_day": false, "location": ""}\n'
            "Nếu không rõ giờ kết thúc thì cộng thêm 1 tiếng sau start_time. Nếu không có giờ cụ thể thì all_day=true.\n"
            f"Câu người dùng: {clean_in}"
        )
        from groq import Groq
        def _ask():
            c = Groq(api_key=groq_key)
            r = c.chat.completions.create(
                model="openai/gpt-oss-20b",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_completion_tokens=1024,
                reasoning_effort="low",
                response_format={"type": "json_object"},
            )
            return r.choices[0].message.content

        parsed = json.loads(await asyncio.to_thread(_ask) or "{}")
        ev = await self.create_event(
            title=str(parsed.get("title") or clean_in),
            date_str=str(parsed.get("date") or now_vn.date().isoformat()),
            start_time=str(parsed.get("start_time") or ""),
            end_time=str(parsed.get("end_time") or ""),
            all_day=bool(parsed.get("all_day", False)),
            location=str(parsed.get("location") or ""),
            description=f"AI Quick Add: {clean_in}",
        )
        return {
            "success": True,
            "created_id": ev.get("created_id", ""),
            "status": ev.get("status", {}),
            "parsed": parsed,
            "event": ev,
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
