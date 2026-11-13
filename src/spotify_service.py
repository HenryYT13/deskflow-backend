import re
import time
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

# Biến lưu trạng thái nội suy tiến trình thời gian thực
_last_api_fetch_time = 0.0
_last_playback_data = None

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

def get_current_playing(force_api: bool = False):
    """
    Chỉ gọi Spotify API thực tế nếu đã cách lần trước >= 1.0 giây.
    Trong các lần gọi trung gian, tự động bù giờ nội suy để trả về tiến trình và lời bài hát chuẩn xác.
    """
    global sp_client, _last_api_fetch_time, _last_playback_data
    if not sp_client:
        return None

    now = time.time()

    # 1. Nếu chưa đủ 4s và đã có dữ liệu cache: Nội suy tiến trình theo đồng hồ máy tính
    if not force_api and _last_playback_data and (now - _last_api_fetch_time < 1.0):
        if not _last_playback_data.get("is_playing"):
            # Still return a normalised copy carrying the latest volume so the
            # apps stop showing a stale slider when playback is paused.
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

    # 2. Đã đủ >= 1s: Gửi 1 request thực tế lên Spotify API
    try:
        playback = sp_client.current_playback()
        _last_api_fetch_time = now

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
        track_name = item.get("name", "Unknown Track")
        artist_name = ", ".join(a["name"] for a in item.get("artists", []))
        duration_sec = int(duration_ms / 1000)

        # Lấy ảnh bìa Album
        images = item.get("album", {}).get("images", [])
        cover_url = images[0]["url"] if images else None

        # Lấy lời bài hát từ cache hoặc API LrcLib
        timeline = fetch_live_lyrics(track_name, artist_name, duration_sec)
        curr_l, next_l, window_l, active_idx = _match_lyrics_window(progress_ms, timeline, 5)

        _last_playback_data = {
            "is_playing": playback.get("is_playing", False),
            "track": track_name,
            "artist": artist_name,
            "album": item.get("album", {}).get("name", ""),
            "base_progress_ms": progress_ms,
            "progress_ms": progress_ms,
            "duration_ms": duration_ms,
            "progress_pct": progress_pct,
            "lyric_curr": curr_l,
            "lyric_next": next_l,
            "lyrics_window": window_l,
            "active_lyric_index": active_idx,
            "cover_url": cover_url,
            "volume": (playback.get("device") or {}).get("volume_percent")
        }
        return _last_playback_data

    except Exception as e:
        logger.warning(f"[Spotify] Lỗi đọc bài đang phát: {e}")
        return _last_playback_data