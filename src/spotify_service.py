import io
import json
import os
import re
import time
import threading
import colorsys
import httpx
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import config
import logging

logger = logging.getLogger("DeskFlow")
sp_client = None

# Cache lưu trữ danh sách lời bài hát [(ms, text), ...]
cached_track_name = ""
cached_lyrics_timeline = []
_cover_color_cache: dict[str, str] = {}
_bpm_cache: dict[str, float | None] = {}
_history_file = os.path.join(os.path.dirname(__file__), "data", "spotify_history.json")
_history_lock = threading.Lock()
_last_recorded_key = ""


def _record_listening_history(track: str, artist: str, album: str, cover_url: str | None, duration_ms: int) -> None:
    global _last_recorded_key
    key = f"{track}|{artist}"
    if not track or key == _last_recorded_key:
        return
    _last_recorded_key = key
    with _history_lock:
        try:
            os.makedirs(os.path.dirname(_history_file), exist_ok=True)
            rows = []
            if os.path.exists(_history_file):
                with open(_history_file, "r", encoding="utf-8") as f:
                    rows = json.load(f) or []
            rows.append({
                "track": track,
                "artist": artist,
                "album": album,
                "cover_url": cover_url,
                "duration_ms": duration_ms,
                "played_at": int(time.time()),
            })
            if len(rows) > 300:
                rows = rows[-300:]
            with open(_history_file, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[Spotify History] Save skip: {e}")


def get_listening_stats() -> dict:
    with _history_lock:
        rows = []
        try:
            if os.path.exists(_history_file):
                with open(_history_file, "r", encoding="utf-8") as f:
                    rows = json.load(f) or []
        except Exception:
            rows = []

    track_counts: dict[str, dict] = {}
    artist_counts: dict[str, int] = {}
    hour_counts: dict[int, int] = {}
    days_set: set[str] = set()
    total_ms = 0
    for r in rows:
        t = str(r.get("track") or "")
        a = str(r.get("artist") or "Unknown")
        if not t:
            continue
        total_ms += int(r.get("duration_ms") or 180000)
        ts = int(r.get("played_at") or 0)
        if ts > 0:
            from datetime import datetime, timezone, timedelta
            dt_vn = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=7)))
            hour_counts[dt_vn.hour] = hour_counts.get(dt_vn.hour, 0) + 1
            days_set.add(dt_vn.strftime("%Y-%m-%d"))
        k = f"{t} — {a}"
        if k not in track_counts:
            track_counts[k] = {"track": t, "artist": a, "cover_url": r.get("cover_url"), "plays": 0}
        track_counts[k]["plays"] += 1
        for part in [p.strip() for p in a.split(",") if p.strip()]:
            artist_counts[part] = artist_counts.get(part, 0) + 1

    top_tracks = sorted(track_counts.values(), key=lambda x: x["plays"], reverse=True)[:10]
    top_artists = [{"artist": k, "plays": v} for k, v in sorted(artist_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    peak_hr = max(hour_counts.items(), key=lambda kv: kv[1])[0] if hour_counts else None
    est_min = int(round(total_ms / 60000.0))
    return {
        "total_plays": len(rows),
        "estimated_minutes": est_min,
        "estimated_hours": round(est_min / 60.0, 1),
        "unique_tracks": len(track_counts),
        "active_days": len(days_set),
        "peak_hour": f"{peak_hr:02d}:00 - {(peak_hr + 1) % 24:02d}:00" if peak_hr is not None else "N/A",
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "recent": list(reversed(rows[-10:])),
    }


def get_playback_queue() -> dict:
    """(#132) Lấy danh sách bài hát tiếp theo trong hàng chờ Spotify."""
    global sp_client
    if not sp_client:
        return {"connected": False, "queue": []}
    try:
        q = sp_client.queue()
        items = []
        for it in (q.get("queue") or [])[:15]:
            imgs = (it.get("album") or {}).get("images") or []
            items.append({
                "id": it.get("id", ""),
                "name": it.get("name", "Unknown"),
                "artist": ", ".join(a.get("name", "") for a in (it.get("artists") or [])),
                "album": (it.get("album") or {}).get("name", ""),
                "duration_ms": it.get("duration_ms", 0),
                "cover_url": imgs[-1].get("url") if imgs else None,
            })
        curr = q.get("currently_playing")
        return {
            "connected": True,
            "current": (curr or {}).get("name") if isinstance(curr, dict) else None,
            "queue": items,
        }
    except Exception as e:
        logger.debug(f"[Spotify Queue] {e}")
        return {"connected": bool(sp_client), "queue": [], "error": str(e)}


def get_available_devices() -> list:
    """(#140) Liệt kê các thiết bị Spotify Connect khả dụng."""
    global sp_client
    if not sp_client:
        return []
    try:
        res = sp_client.devices() or {}
        return [
            {
                "id": d.get("id"),
                "name": d.get("name", "Unknown Device"),
                "type": d.get("type", "Computer"),
                "is_active": bool(d.get("is_active", False)),
                "volume_percent": d.get("volume_percent"),
            }
            for d in (res.get("devices") or [])
            if d.get("id")
        ]
    except Exception as e:
        logger.debug(f"[Spotify Devices] {e}")
        return []


_AUDIO_FEATURES_CACHE: dict[str, dict] = {}


def get_track_audio_features(track_id: str) -> dict:
    """(#259) Lấy BPM, Energy, Valence của bài hát đang phát (có cache)."""
    global sp_client
    if not track_id:
        return {}
    if track_id in _AUDIO_FEATURES_CACHE:
        return _AUDIO_FEATURES_CACHE[track_id]
    if not sp_client:
        return {}
    try:
        feats = sp_client.audio_features([track_id]) or []
        f = feats[0] if feats and isinstance(feats[0], dict) else {}
        res = {
            "bpm": round(float(f.get("tempo", 0)), 1) if f.get("tempo") else None,
            "energy": round(float(f.get("energy", 0)), 2) if f.get("energy") is not None else None,
            "valence": round(float(f.get("valence", 0)), 2) if f.get("valence") is not None else None,
            "danceability": round(float(f.get("danceability", 0)), 2) if f.get("danceability") is not None else None,
        }
        _AUDIO_FEATURES_CACHE[track_id] = res
        if len(_AUDIO_FEATURES_CACHE) > 200:
            _AUDIO_FEATURES_CACHE.clear()
        return res
    except Exception as e:
        logger.debug(f"[Spotify AudioFeatures] {e}")
        return {}


def get_recently_played(limit: int = 20) -> list:
    """(#264) Lấy lịch sử 20 bài hát vừa nghe gần nhất."""
    global sp_client
    if not sp_client:
        return []
    try:
        res = sp_client.current_user_recently_played(limit=min(50, max(1, limit))) or {}
        items = []
        for entry in (res.get("items") or []):
            tr = entry.get("track") or {}
            imgs = (tr.get("album") or {}).get("images") or []
            items.append({
                "id": tr.get("id", ""),
                "name": tr.get("name", "Unknown"),
                "artist": ", ".join(a.get("name", "") for a in (tr.get("artists") or [])),
                "album": (tr.get("album") or {}).get("name", ""),
                "cover_url": imgs[-1].get("url") if imgs else None,
                "played_at": entry.get("played_at", ""),
            })
        return items
    except Exception as e:
        logger.debug(f"[Spotify History] {e}")
        return []


def transfer_playback_device(device_id: str, force_play: bool = True) -> dict:
    """(#140) Chuyển phát nhạc Spotify sang thiết bị đích."""
    global sp_client
    if not sp_client:
        raise RuntimeError("spotify_not_connected")
    sp_client.transfer_playback(device_id=device_id, force_play=force_play)
    return {"success": True, "device_id": device_id}


def _fetch_track_bpm(track_id: str | None) -> float | None:
    if not track_id or not sp_client:
        return None
    if track_id in _bpm_cache:
        return _bpm_cache[track_id]
    bpm_val: float | None = None
    try:
        feats = sp_client.audio_features([track_id])
        if feats and isinstance(feats, list) and feats[0] and feats[0].get("tempo"):
            bpm_val = round(float(feats[0]["tempo"]), 1)
    except Exception:
        bpm_val = None
    if len(_bpm_cache) > 128:
        _bpm_cache.clear()
    _bpm_cache[track_id] = bpm_val
    return bpm_val


def extract_dominant_hex(cover_url: str | None) -> str | None:
    """Fetches album cover thumbnail and picks the most vibrant dominant color as #RRGGBB."""
    if not cover_url or not isinstance(cover_url, str):
        return None
    cached = _cover_color_cache.get(cover_url)
    if cached:
        return cached
    try:
        from PIL import Image
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(cover_url)
            if resp.status_code != 200 or not resp.content:
                return None
        img = Image.open(io.BytesIO(resp.content)).convert("RGB").resize((24, 24))
        best_rgb = (255, 255, 255)
        best_score = -1.0
        sum_r = sum_g = sum_b = total = 0
        for r, g, b in img.getdata():
            sum_r += r
            sum_g += g
            sum_b += b
            total += 1
            _, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            # Prefer vivid mid-to-bright colors instead of black/white background pixels
            if v < 0.18 or v > 0.96:
                continue
            score = (s * 1.8) * (0.4 + v)
            if score > best_score:
                best_score = score
                best_rgb = (r, g, b)
        if best_score < 0.15 and total > 0:
            best_rgb = (sum_r // total, sum_g // total, sum_b // total)
        hex_col = f"#{best_rgb[0]:02X}{best_rgb[1]:02X}{best_rgb[2]:02X}"
        if len(_cover_color_cache) > 64:
            _cover_color_cache.clear()
        _cover_color_cache[cover_url] = hex_col
        return hex_col
    except Exception as e:
        logger.debug(f"[Spotify Ambient] Color extract skip: {e}")
        return None

# Biến lưu trạng thái nội suy tiến trình thời gian thực
_last_api_fetch_time = 0.0
_last_playback_data = None
_rate_limit_until = 0.0
_api_lock = threading.Lock()
_API_POLL_INTERVAL = 6.0  # Poll Spotify every 6s; interpolate progress/lyrics smoothly every 1s

def init_spotify():
    global sp_client
    try:
        sp_oauth = SpotifyOAuth(
            client_id=config.SPOTIFY_CLIENT_ID,
            client_secret=config.SPOTIFY_CLIENT_SECRET,
            redirect_uri=config.SPOTIFY_REDIRECT_URI,
            scope="user-read-currently-playing user-read-playback-state user-modify-playback-state",
            open_browser=False,
            cache_path=".spotify_cache"
        )
        # retries=0 để tránh sleep làm treo thread
        sp_client = spotipy.Spotify(auth_manager=sp_oauth, retries=0, requests_timeout=3)
        logger.info("[Spotify] Khởi tạo Spotify Client API thành công!")
    except Exception as e:
        logger.error(f"[Spotify] Lỗi khởi tạo Spotify Client: {e}", exc_info=True)

def _get_active_device_id():
    if not sp_client:
        return None
    try:
        devices = sp_client.devices().get("devices", [])
        if not devices:
            return None
        for d in devices:
            if d.get("is_active"):
                return d["id"]
        return devices[0]["id"]
    except Exception as e:
        logger.error(f"[Spotify] Lỗi lấy thiết bị: {e}")
        return None

def play_pause_toggle():
    global sp_client, _last_api_fetch_time
    if not sp_client:
        return
    try:
        playback = sp_client.current_playback()
        dev_id = _get_active_device_id()
        if playback and playback.get("is_playing"):
            sp_client.pause_playback(device_id=dev_id)
            logger.info("[Spotify Control] -> Tạm dừng (PAUSE)")
        else:
            sp_client.start_playback(device_id=dev_id)
            logger.info("[Spotify Control] -> Tiếp tục phát (PLAY)")
        _last_api_fetch_time = 0.0  # Reset để lần đọc tiếp theo gọi API ngay lập tức
    except Exception as e:
        logger.error(f"[Spotify Control Error] Toggle play/pause: {e}")

def next_track():
    global sp_client, _last_api_fetch_time
    if not sp_client:
        return
    try:
        dev_id = _get_active_device_id()
        sp_client.next_track(device_id=dev_id)
        _last_api_fetch_time = 0.0
        logger.info("[Spotify Control] -> Bài tiếp theo (NEXT)")
    except Exception as e:
        logger.error(f"[Spotify Control Error] Next track: {e}")

def previous_track():
    global sp_client, _last_api_fetch_time
    if not sp_client:
        return
    try:
        dev_id = _get_active_device_id()
        sp_client.previous_track(device_id=dev_id)
        _last_api_fetch_time = 0.0
        logger.info("[Spotify Control] -> Bài trước đó (PREV)")
    except Exception as e:
        logger.error(f"[Spotify Control Error] Prev track: {e}")

def set_playback_volume(volume_percent: int):
    """Sets Spotify playback volume (0-100%)."""
    global sp_client
    if not sp_client:
        return
    vol = max(0, min(100, int(volume_percent)))
    try:
        dev_id = _get_active_device_id()
        if not dev_id:
            # Spotify only applies volume changes on a known/active device.
            # Falling through with device_id=None silently did nothing, which
            # looked like "volume control is broken" from the apps.
            devices = sp_client.devices().get("devices", [])
            dev_id = devices[0]["id"] if devices else None
        if not dev_id:
            logger.warning("[Spotify Control] No Spotify device found; volume not applied.")
            return
        applied = False
        try:
            sp_client.volume(vol, device_id=dev_id)
            applied = True
        except Exception as first_err:
            logger.warning(f"[Spotify Control] volume() failed ({first_err}); activating {dev_id}")
            try:
                sp_client.transfer_playback(device_id=dev_id, force_play=False)
            except Exception:
                pass
            sp_client.volume(vol, device_id=dev_id)
            applied = True

        # Read back: Spotify silently ignores volume on some device types, and a
        # readback is the only way to know the app slider reflects reality.
        real = get_current_volume()
        if _last_playback_data is not None:
            _last_playback_data["volume_pct"] = real if real is not None else vol
        logger.info(f"[Spotify Control] -> Set volume: {vol}% (applied={applied}, device={dev_id}, readback={real})")
    except Exception as e:
        logger.error(f"[Spotify Control Error] Set volume: {e}")

def get_current_volume():
    """Returns the active Spotify device volume (0-100), or None if unknown."""
    global sp_client
    if not sp_client:
        return None
    try:
        playback = sp_client.current_playback()
        if playback and playback.get("device"):
            vol = playback["device"].get("volume_percent")
            if isinstance(vol, int):
                return max(0, min(100, vol))
    except Exception as e:
        logger.warning(f"[Spotify] Could not read volume: {e}")
    cached = (_last_playback_data or {}).get("volume_pct")
    return cached if isinstance(cached, int) else None
    return None


def parse_lrc(lrc_text: str):
    """Phân tích cú pháp chuỗi [mm:ss.xx] thành danh sách (mili_giay, text)"""
    timeline = []
    pattern = re.compile(r"\[(\d{2}):(\d{2})(?:\.(\d{2,3}))?\](.*)")
    for line in lrc_text.splitlines():
        match = pattern.match(line.strip())
        if match:
            mins = int(match.group(1))
            secs = int(match.group(2))
            ms_str = match.group(3) or "0"
            ms = int(ms_str) * 10 if len(ms_str) == 2 else int(ms_str)
            total_ms = (mins * 60 + secs) * 1000 + ms
            text = match.group(4).strip()
            if text:
                timeline.append((total_ms, text))
    timeline.sort(key=lambda x: x[0])
    return timeline

def fetch_live_lyrics(track_name: str, artist_name: str, duration_sec: int):
    """Lấy lời bài hát đồng bộ từ LrcLib API"""
    global cached_track_name, cached_lyrics_timeline
    if track_name == cached_track_name and cached_lyrics_timeline:
        return cached_lyrics_timeline

    cached_track_name = track_name
    cached_lyrics_timeline = []

    clean_track = re.sub(r"\(.*?\)|\[.*?\]", "", track_name).strip()
    clean_artist = artist_name.split(",")[0].strip()

    try:
        url = "https://lrclib.net/api/get"
        params = {
            "track_name": clean_track,
            "artist_name": clean_artist,
            "duration": duration_sec
        }
        headers = {"User-Agent": "DeskFlow-Station/1.0 (https://github.com/HenryYT13)"}
        with httpx.Client(timeout=4.0) as client:
            res = client.get(url, params=params, headers=headers)
            if res.status_code == 200:
                data = res.json()
                synced = data.get("syncedLyrics")
                if synced:
                    cached_lyrics_timeline = parse_lrc(synced)
                    logger.info(f"[LrcLib] Đã tải lời đồng bộ cho bài: {clean_track} ({len(cached_lyrics_timeline)} dòng)")
                    return cached_lyrics_timeline
    except Exception as e:
        logger.warning(f"[LrcLib Warning] Không tìm thấy lời bài hát: {e}")

    return []

def _match_lyrics_window(progress_ms: int, timeline: list, window_size: int = 5):
    """
    Computes a 5-line synchronized lyric window around the current playback position.
    Returns: (curr_line, next_line, lyrics_window_list, active_index_in_window)
    """
    if not timeline:
        return ("♪ Đang phát nhạc ♪", "", ["", "", "♪ Đang phát nhạc ♪", "", ""], 2)

    curr_idx = -1
    for i, (ts, text) in enumerate(timeline):
        if progress_ms >= ts:
            curr_idx = i
        else:
            break

    if curr_idx == -1:
        curr_line = "..."
        next_line = timeline[0][1] if len(timeline) > 0 else ""
        first_lines = [item[1] for item in timeline[:4]]
        window = ["..."] + first_lines
        while len(window) < window_size:
            window.append("")
        return (curr_line, next_line, window[:window_size], 0)

    curr_line = timeline[curr_idx][1]
    next_line = timeline[curr_idx + 1][1] if curr_idx + 1 < len(timeline) else ""

    start_idx = max(0, curr_idx - 2)
    end_idx = min(len(timeline), start_idx + window_size)
    if end_idx - start_idx < window_size:
        start_idx = max(0, end_idx - window_size)

    window = [timeline[k][1] for k in range(start_idx, end_idx)]
    active_idx = curr_idx - start_idx

    while len(window) < window_size:
        window.append("")

    return (curr_line, next_line, window, active_idx)

def _match_lyrics(progress_ms: int, timeline: list):
    """Tìm câu hiện tại và câu kế tiếp dựa trên mốc mili-giây"""
    curr, nxt, _, _ = _match_lyrics_window(progress_ms, timeline, 5)
    return (curr, nxt)

def _parse_retry_after(err: Exception) -> float:
    headers = getattr(err, "headers", None) or {}
    for k, v in headers.items():
        if str(k).lower() == "retry-after":
            try:
                return max(5.0, float(v))
            except (TypeError, ValueError):
                pass
    m = re.search(r"retry\s+(?:will\s+occur\s+)?after[:\s]+(\d+)", str(err), re.IGNORECASE)
    if m:
        return max(5.0, float(m.group(1)))
    return 60.0

def get_current_playing(force_api: bool = False):
    """
    Chỉ gọi Spotify API thực tế nếu đã cách lần trước >= 6.0 giây (và không bị 429).
    Trong các lần gọi trung gian, tự động bù giờ nội suy để trả về tiến trình và lời bài hát chuẩn xác.
    """
    global sp_client, _last_api_fetch_time, _last_playback_data, _rate_limit_until
    if not sp_client:
        return None

    now = time.time()

    # 0. Nếu đang trong thời gian chờ Retry-After của Spotify 429, không gọi API thêm
    if now < _rate_limit_until:
        return _last_playback_data

    # 1. Nếu chưa đủ _API_POLL_INTERVAL và đã có dữ liệu cache: Nội suy tiến trình theo đồng hồ máy tính
    if not force_api and _last_playback_data and (now - _last_api_fetch_time < _API_POLL_INTERVAL):
        if not _last_playback_data.get("is_playing"):
            paused = dict(_last_playback_data)
            paused["volume"] = (_last_playback_data or {}).get("volume_pct")
            return paused

        elapsed_ms = int((now - _last_api_fetch_time) * 1000)
        dur_ms = _last_playback_data["duration_ms"]
        curr_prog_ms = min(dur_ms, _last_playback_data["base_progress_ms"] + elapsed_ms)
        curr_pct = min(100, int((curr_prog_ms / dur_ms) * 100)) if dur_ms > 0 else 0

        curr_l, next_l, window_l, active_idx = _match_lyrics_window(curr_prog_ms, cached_lyrics_timeline, 5)

        result = dict(_last_playback_data)
        result["progress_ms"] = curr_prog_ms
        result["progress_pct"] = curr_pct
        result["lyric_curr"] = curr_l
        result["lyric_next"] = next_l
        result["lyrics_window"] = window_l
        result["active_lyric_index"] = active_idx
        return result

    # 2. Đã đủ >= _API_POLL_INTERVAL: Gửi tối đa 1 request thực tế lên Spotify API
    with _api_lock:
        now = time.time()
        if now < _rate_limit_until:
            return _last_playback_data
        if not force_api and _last_playback_data and (now - _last_api_fetch_time < _API_POLL_INTERVAL):
            return _last_playback_data
        _last_api_fetch_time = now
        try:
            playback = sp_client.current_playback()

            if not playback or not playback.get("item"):
                _last_playback_data = {
                    "is_playing": False,
                    "track": "Tạm dừng / Chưa phát",
                    "artist": "N/A",
                    "base_progress_ms": 0,
                    "progress_ms": 0,
                    "duration_ms": 1,
                    "progress_pct": 0,
                    "lyric_curr": "",
                    "lyric_next": "",
                    "lyrics_window": ["", "", "", "", ""],
                    "active_lyric_index": 2,
                    "cover_url": None,
                    "volume": None
                }
                return _last_playback_data

            item = playback["item"]
            progress_ms = playback.get("progress_ms", 0)
            duration_ms = item.get("duration_ms", 1)
            progress_pct = int((progress_ms / duration_ms) * 100) if duration_ms > 0 else 0
            track_id = item.get("id")
            track_name = item.get("name", "Unknown Track")
            artist_name = ", ".join(a["name"] for a in item.get("artists", []))
            album_name = item.get("album", {}).get("name", "")
            duration_sec = int(duration_ms / 1000)

            # Lấy ảnh bìa Album & màu chủ đạo cho Ambient Light + BPM + History
            images = item.get("album", {}).get("images", [])
            cover_url = images[-1]["url"] if images else None
            display_cover_url = images[0]["url"] if images else cover_url
            ambient_hex = extract_dominant_hex(cover_url)
            bpm_val = _fetch_track_bpm(track_id)
            if playback.get("is_playing"):
                _record_listening_history(track_name, artist_name, album_name, display_cover_url, duration_ms)

            # Lấy lời bài hát từ cache hoặc API LrcLib
            timeline = fetch_live_lyrics(track_name, artist_name, duration_sec)
            curr_l, next_l, window_l, active_idx = _match_lyrics_window(progress_ms, timeline, 5)

            _last_playback_data = {
                "is_playing": playback.get("is_playing", False),
                "track": track_name,
                "artist": artist_name,
                "album": album_name,
                "bpm": bpm_val,
                "base_progress_ms": progress_ms,
                "progress_ms": progress_ms,
                "duration_ms": duration_ms,
                "progress_pct": progress_pct,
                "lyric_curr": curr_l,
                "lyric_next": next_l,
                "lyrics_window": window_l,
                "active_lyric_index": active_idx,
                "cover_url": display_cover_url,
                "ambient_color": ambient_hex,
                "volume": (playback.get("device") or {}).get("volume_percent")
            }
            return _last_playback_data

        except Exception as e:
            http_status = getattr(e, "http_status", None)
            if http_status == 429 or "429" in str(e):
                wait_sec = _parse_retry_after(e)
                _rate_limit_until = time.time() + wait_sec
                logger.warning(f"[Spotify] Bị rate-limit 429; tạm ngưng gọi Spotify API trong {int(wait_sec)}s.")
            else:
                logger.warning(f"[Spotify] Lỗi đọc bài đang phát: {e}")
            return _last_playback_data