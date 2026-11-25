import json
import httpx
import logging
import os
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import config
from timetable import get_dual_day_tkb, MORNING_PERIOD_TIMES
import spotify_service
from calendar_service import calendar_service
from focus_mode.auth import parse_iso_utc

logger = logging.getLogger("DeskFlow")
VN_TZ = timezone(timedelta(hours=7))

bot_ref = None
mqtt_ref = None

def remove_vietnamese_diacritics(text: str) -> str:
    """Khử sạch 100% dấu tiếng Việt chuẩn quốc tế bằng unicodedata NFD"""
    if not text:
        return ""
    text = text.replace("đ", "d").replace("Đ", "D")
    nfd_form = unicodedata.normalize('NFD', text)
    clean_text = "".join([c for c in nfd_form if unicodedata.category(c) != 'Mn'])
    return clean_text

_weather_aqi_cache: Dict[str, Dict[str, Any]] = {}


def _aqi_label(aqi: int) -> str:
    return {1: "Tốt", 2: "Khá", 3: "Trung bình", 4: "Kém", 5: "Xấu"}.get(aqi, "Khá")


def _uv_label(uv: float) -> str:
    if uv >= 11:
        return "Cực hại"
    if uv >= 8:
        return "Rất cao"
    if uv >= 6:
        return "Cao"
    if uv >= 3:
        return "Trung bình"
    return "Thấp"


async def get_weather_aqi_uv(city: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None) -> Dict[str, Any]:
    """(#69) Lấy chỉ số AQI & UV Index theo giờ cho Hạ Long / thành phố đang chọn."""
    target_city = (city or getattr(config, "WEATHER_CITY", "Ha Long")).strip()
    cache_key = target_city.lower()
    now_ts = time.time()
    cached = _weather_aqi_cache.get(cache_key)
    if cached and (now_ts - cached.get("timestamp", 0) < 900):
        return cached["data"]

    if lat is None or lon is None:
        lat, lon = 20.9511, 107.0748  # Ha Long default

    api_key = getattr(config, "OPENWEATHER_API_KEY", None)
    aqi_score = 2
    pm2_5 = 18.0
    pm10 = 28.0
    uv_val = 4.2
    hourly_uv: List[Dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        if api_key:
            try:
                if city and "ha long" not in cache_key:
                    gr = await client.get(
                        f"https://api.openweathermap.org/geo/1.0/direct?q={target_city}&limit=1&appid={api_key}",
                        timeout=6.0,
                    )
                    if gr.status_code == 200 and gr.json():
                        lat = float(gr.json()[0].get("lat", lat))
                        lon = float(gr.json()[0].get("lon", lon))
                ar = await client.get(
                    f"https://api.openweathermap.org/data/2.5/air_pollution?lat={lat}&lon={lon}&appid={api_key}",
                    timeout=8.0,
                )
                if ar.status_code == 200:
                    aitem = (ar.json().get("list") or [{}])[0]
                    aqi_score = int((aitem.get("main") or {}).get("aqi") or 2)
                    comps = aitem.get("components") or {}
                    pm2_5 = round(float(comps.get("pm2_5", pm2_5)), 1)
                    pm10 = round(float(comps.get("pm10", pm10)), 1)
            except Exception:
                pass
        try:
            om = await client.get(
                f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=uv_index&hourly=uv_index&forecast_days=1&timezone=Asia%2FHo_Chi_Minh",
                timeout=8.0,
            )
            if om.status_code == 200:
                omj = om.json()
                uv_val = round(float((omj.get("current") or {}).get("uv_index") or uv_val), 1)
                h_times = (omj.get("hourly") or {}).get("time") or []
                h_uvs = (omj.get("hourly") or {}).get("uv_index") or []
                for t_s, u_s in zip(h_times[:12], h_uvs[:12]):
                    hourly_uv.append({"time": t_s[-5:], "uv": round(float(u_s or 0.0), 1)})
        except Exception:
            pass

    res = {
        "city": target_city,
        "aqi": aqi_score,
        "aqi_label": _aqi_label(aqi_score),
        "pm2_5": pm2_5,
        "pm10": pm10,
        "uv_index": uv_val,
        "uv_label": _uv_label(uv_val),
        "hourly_uv": hourly_uv,
    }
    _weather_aqi_cache[cache_key] = {"timestamp": now_ts, "data": res}
    return res


async def weather_sync_job():
    """Tự động cập nhật thời tiết + AQI/UV mỗi 5 phút từ OpenWeatherMap & Open-Meteo"""
    api_key = getattr(config, "OPENWEATHER_API_KEY", None)
    if not api_key:
        logger.warning("[Weather] Chưa cấu hình OPENWEATHER_API_KEY trong config.py")
        return

    city = getattr(config, "WEATHER_CITY", "Ha Long")
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric&lang=vi"
    
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                temp = round(data["main"]["temp"])
                coord = data.get("coord") or {}
                weather_text = f"{temp}*C"
                aqi_uv = await get_weather_aqi_uv(city=city, lat=coord.get("lat"), lon=coord.get("lon"))
                
                payload = {
                    "action": "weather_update",
                    "city": remove_vietnamese_diacritics(city),
                    "weather": weather_text,
                    "aqi": aqi_uv.get("aqi", 2),
                    "aqi_label": remove_vietnamese_diacritics(aqi_uv.get("aqi_label", "Kha")),
                    "pm2_5": aqi_uv.get("pm2_5", 18.0),
                    "uv": aqi_uv.get("uv_index", 4.0),
                    "uv_label": remove_vietnamese_diacritics(aqi_uv.get("uv_label", "TB")),
                }
                
                if mqtt_ref and mqtt_ref.is_connected():
                    mqtt_ref.publish("deskflow/weather", json.dumps(payload, ensure_ascii=False), qos=1)
                    logger.info(f"[Weather Auto-Sync] {city} -> {weather_text} | AQI {payload['aqi']} | UV {payload['uv']}")
        except Exception as e:
            logger.error(f"[Weather Auto-Sync Error]: {e}")

async def tkb_auto_sync_job():
    """Kéo TKB cả Hôm nay & Ngày mai từ PocketBase phát xuống ESP32"""
    try:
        dual_tkb = await get_dual_day_tkb()
        if not dual_tkb or not dual_tkb.get("today"):
            return

        today = dual_tkb["today"]
        tom = dual_tkb.get("tomorrow", {})

        clean_morning = [s.split(": ")[-1] for s in today.get("morning", []) if s.strip()]
        clean_afternoon = [s.split(": ")[-1] for s in today.get("afternoon", []) if s.strip()]
        has_afternoon = bool(today.get("has_afternoon", False)) and len(clean_afternoon) > 0

        dp_sang = today.get("dp_sang", "Tu do")
        dp_chieu = today.get("dp_chieu", "")
        if has_afternoon:
            if dp_chieu and dp_chieu != dp_sang:
                uniform_today = f"Sang: {dp_sang}, Chieu: {dp_chieu}"
            else:
                uniform_today = f"{dp_sang} (chung ca ngay)"
        else:
            uniform_today = dp_sang

        tom_morning = [s.split(": ")[-1] for s in tom.get("morning", []) if s.strip()]
        tom_afternoon = [s.split(": ")[-1] for s in tom.get("afternoon", []) if s.strip()]
        tom_has_afternoon = bool(tom.get("has_afternoon", False)) and len(tom_afternoon) > 0

        tom_dp_sang = tom.get("dp_sang", "Tu do")
        tom_dp_chieu = tom.get("dp_chieu", "")
        if tom_has_afternoon:
            if tom_dp_chieu and tom_dp_chieu != tom_dp_sang:
                uniform_tom = f"Sang: {tom_dp_sang}, Chieu: {tom_dp_chieu}"
            else:
                uniform_tom = f"{tom_dp_sang} (chung ca ngay)"
        else:
            uniform_tom = tom_dp_sang

        tom_label = tom.get("display_day", tom.get("day", "Ngay mai"))

        payload = {
            "action": "sync_data",
            "view": "tkb",
            "day_name": remove_vietnamese_diacritics(today.get("day", "Hom nay")),
            "morning": remove_vietnamese_diacritics(", ".join(clean_morning) if clean_morning else "Nghi hoc"),
            "has_afternoon": has_afternoon,
            "afternoon": remove_vietnamese_diacritics(", ".join(clean_afternoon) if has_afternoon else ""),
            "uniform": remove_vietnamese_diacritics(uniform_today),
            "tom_day": remove_vietnamese_diacritics(tom_label),
            "tom_morning": remove_vietnamese_diacritics(", ".join(tom_morning) if tom_morning else "Nghi hoc"),
            "tom_has_afternoon": tom_has_afternoon,
            "tom_afternoon": remove_vietnamese_diacritics(", ".join(tom_afternoon) if tom_has_afternoon else ""),
            "tom_uniform": remove_vietnamese_diacritics(uniform_tom)
        }

        if mqtt_ref and mqtt_ref.is_connected():
            mqtt_ref.publish("deskflow/schedule", json.dumps(payload, ensure_ascii=False), qos=1)
            logger.info(f"[TKB Sync] Đã đồng bộ TKB Hôm nay ({payload['day_name']}) & Kế tiếp ({payload['tom_day']}).")

    except Exception as e:
        logger.error(f"[TKB Auto-Sync Error]: {e}")

async def check_rain_job():
    """Tự động kiểm tra mưa lúc 06:15 sáng"""
    api_key = getattr(config, "OPENWEATHER_API_KEY", None)
    if not api_key:
        return

    city = getattr(config, "WEATHER_CITY", "Ha Long")
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric&lang=vi"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                weather_main = data["weather"][0]["main"].lower()
                weather_desc = data["weather"][0]["description"]

                if any(k in weather_main for k in ["rain", "drizzle", "thunder"]):
                    if bot_ref:
                        user = await bot_ref.fetch_user(config.OWNER_USER_ID)
                        if user:
                            await user.send(f"🌧️ **DeskFlow:** Hôm nay {city} có **{weather_desc}**. Nhớ mang áo mưa đi học!")
        except Exception as e:
            logger.error(f"[Rain Check Error]: {e}")

scheduler_instance = None

def reschedule_rain_check(time_str: str) -> bool:
    global scheduler_instance
    if not scheduler_instance:
        logger.warning("[Scheduler] Cannot reschedule: scheduler_instance is None")
        return False
    try:
        parts = time_str.strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        trigger = CronTrigger(hour=h, minute=m, timezone="Asia/Ho_Chi_Minh")
        if scheduler_instance.get_job("check_rain_job"):
            scheduler_instance.reschedule_job("check_rain_job", trigger=trigger)
            logger.info(f"[Scheduler] Rescheduled check_rain_job to {h:02d}:{m:02d} (Asia/Ho_Chi_Minh)")
        else:
            scheduler_instance.add_job(check_rain_job, trigger, id="check_rain_job", replace_existing=True)
            logger.info(f"[Scheduler] Added check_rain_job at {h:02d}:{m:02d} (Asia/Ho_Chi_Minh)")
        return True
    except Exception as e:
        logger.error(f"[Scheduler] Failed to reschedule check_rain_job: {e}")
        return False

# In-memory 60-minute weather forecast cache
_weather_forecast_cache: Dict[str, Dict[str, Any]] = {}

async def get_weather_forecast(city: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve actual forecast points from OpenWeatherMap (5 day / 3 hour intervals) with 60-minute caching, or dynamic fallback."""
    global _weather_forecast_cache
    target_city = city or getattr(config, "WEATHER_CITY", "Ha Long")
    cache_key = target_city.strip().lower()
    now_ts = time.time()
    api_key = getattr(config, "OPENWEATHER_API_KEY", None)

    if cache_key in _weather_forecast_cache:
        cached = _weather_forecast_cache[cache_key]
        if now_ts - cached.get("timestamp", 0) < 3600:
            return cached["data"]

    intervals = []
    if api_key:
        url = f"https://api.openweathermap.org/data/2.5/forecast?q={target_city}&appid={api_key}&units=metric&lang=vi"
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(url, timeout=10.0)
                if r.status_code == 200:
                    data = r.json()
                    for item in data.get("list", [])[:16]:  # Next 48 hours (16 * 3h)
                        w_obj = item.get("weather", [{}])[0] if item.get("weather") else {}
                        w_main = w_obj.get("main", "")
                        w_desc = w_obj.get("description", "")
                        w_icon = w_obj.get("icon", "")
                        pop_val = float(item.get("pop", 0) or 0.0)
                        dt_val = item.get("dt", 0)
                        dt_txt_val = item.get("dt_txt", "")
                        is_rain = bool(pop_val >= 0.4 or w_main in ("Rain", "Drizzle", "Thunderstorm"))
                        intervals.append({
                            "dt": dt_val,
                            "timestamp": dt_val,
                            "dt_txt": dt_txt_val,
                            "time": dt_txt_val,
                            "temp": item.get("main", {}).get("temp", 0.0),
                            "feels_like": item.get("main", {}).get("feels_like", 0.0),
                            "humidity": item.get("main", {}).get("humidity", 0),
                            "weather_main": w_main,
                            "weather_desc": w_desc,
                            "description": w_desc,
                            "icon": w_icon,
                            "pop": pop_val,
                            "rain_prob": round(pop_val * 100, 1),
                            "is_rain": is_rain
                        })
            except Exception:
                pass

    if not intervals:
        # Fallback realistic 24-48h forecast intervals
        weather_states = [
            ("Clouds", "Mây rải rác", "03d", 28.5, 65, 0.1),
            ("Clear", "Trời quang đãng", "01n", 26.0, 72, 0.0),
            ("Clear", "Nắng ấm", "01d", 30.2, 58, 0.0),
            ("Rain", "Mưa rào nhẹ", "10d", 27.0, 85, 0.6),
            ("Clouds", "Nhiều mây", "04d", 28.0, 68, 0.2),
            ("Clear", "Trời trong xanh", "01d", 29.5, 60, 0.0),
            ("Clouds", "Mây thay đổi", "02d", 27.5, 70, 0.1),
            ("Clear", "Mát mẻ dễ chịu", "01n", 25.0, 75, 0.0),
        ]
        base_dt = int(now_ts)
        for i in range(12):
            w_main, w_desc, w_icon, temp_val, hum_val, pop_val = weather_states[i % len(weather_states)]
            step_ts = base_dt + i * 3600 * 3
            dt_txt = datetime.fromtimestamp(step_ts).strftime("%Y-%m-%d %H:%M:%S")
            intervals.append({
                "dt": step_ts,
                "timestamp": step_ts,
                "dt_txt": dt_txt,
                "time": dt_txt,
                "temp": round(temp_val + (i % 3) * 0.5, 1),
                "feels_like": round(temp_val + 1.2, 1),
                "humidity": hum_val,
                "weather_main": w_main,
                "weather_desc": w_desc,
                "description": w_desc,
                "icon": w_icon,
                "pop": pop_val,
                "rain_prob": round(pop_val * 100, 1),
                "is_rain": pop_val >= 0.4
            })

    res = {
        "city": target_city,
        "provider": "open_weather" if api_key else "deskflow_forecast_service",
        "available": True,
        "intervals": intervals
    }
    _weather_forecast_cache[cache_key] = {
        "timestamp": now_ts,
        "data": res
    }
    return res

_last_ambient_hex: str | None = None
_last_bpm_track_id: str | None = None
_notified_event_ids: set[str] = set()


async def calendar_sync_job():
    """Đồng bộ Google Calendar (OAuth + iCal) mỗi 5 phút và đẩy lên ESP32."""
    try:
        status = await calendar_service.sync_events(push_to_station=True)
        if status.get("connected"):
            logger.debug(f"[Calendar Sync] {status.get('upcoming_count', 0)} events synced.")
    except Exception as e:
        logger.warning(f"[Calendar Sync Error] {e}")


async def ambient_cover_sync_job():
    """Đồng bộ màu bìa album Spotify & tốc độ BPM sang đèn WS2812B ESP32."""
    global _last_ambient_hex, _last_bpm_track_id
    try:
        import asyncio
        import station_control
        import spotify_service
        sc = station_control.station_control
        ambient_on = bool(sc.device_settings.get("ambient_sync_enabled", False))
        bpm_on = bool(sc.device_settings.get("bpm_sync_enabled", False))
        if not ambient_on and not bpm_on:
            _last_ambient_hex = None
            _last_bpm_track_id = None
            return
        sp = await asyncio.to_thread(spotify_service.get_current_playing, False)
        if not sp or not sp.get("is_playing"):
            return
        if ambient_on:
            hex_col = sp.get("ambient_color")
            if hex_col and hex_col != _last_ambient_hex:
                _last_ambient_hex = hex_col
                sc.set_lamp_color(hex_col)
                logger.info(f"[Ambient] Spotify cover -> LED {hex_col} ({sp.get('track')})")
        if bpm_on:
            track_id = str(sp.get("track_id") or "")
            bpm = sp.get("bpm")
            if bpm and track_id and track_id != _last_bpm_track_id:
                _last_bpm_track_id = track_id
                # Map typical 60..170 BPM -> LED speed 15..98
                mapped_speed = max(15, min(98, int(round((float(bpm) - 50.0) * 0.72 + 15.0))))
                eff = str(sc.device_settings.get("led_effect") or "breathing")
                if eff in ("solid", "ambient"):
                    eff = "breathing"
                sc.set_lamp_effect(eff, speed=mapped_speed)
                logger.info(f"[BPM Sync] {sp.get('track')} ({bpm} BPM) -> {eff} @ speed {mapped_speed}")
    except Exception as e:
        logger.debug(f"[Ambient Sync] Skip: {e}")


async def wakeup_check_job():
    """Kiểm tra trạng thái báo thức chống ngủ lại (Wake-up verification)."""
    try:
        from alarm_engine import alarm_engine
        await alarm_engine.check_wakeup_tick()
    except Exception as e:
        logger.debug(f"[Wakeup Tick] {e}")


async def calendar_upcoming_reminder_job():
    """Bắn thông báo về ĐT/Web trước giờ sự kiện Google Calendar 15 phút."""
    try:
        import station_control
        now_vn = datetime.now(VN_TZ)
        status = calendar_service.get_public_status()
        for ev in status.get("events", []):
            if not ev.get("is_today") or ev.get("all_day"):
                continue
            eid = str(ev.get("id") or "")
            if not eid or eid in _notified_event_ids:
                continue
            try:
                start_dt = datetime.fromisoformat(ev["start_iso"]).astimezone(VN_TZ)
                delta_min = (start_dt - now_vn).total_seconds() / 60.0
                if 0.0 < delta_min <= 15.5:
                    _notified_event_ids.add(eid)
                    if len(_notified_event_ids) > 200:
                        _notified_event_ids.clear()
                    station_control.station_control.push_notification(
                        "calendar_reminder",
                        f"Sắp tới lịch ({int(round(delta_min))}p nữa)",
                        f"{ev.get('title')} ({ev.get('start_time')})",
                        "high",
                    )
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[Calendar Reminder] {e}")


async def ai_tkb_weekly_cron_job():
    """Tự động chạy AI Sync TKB -> Google Calendar mỗi sáng Thứ 2 lúc 06:00."""
    try:
        import station_control
        if not station_control.station_control.device_settings.get("tkb_auto_sync_gcal", False):
            return
        await calendar_service.ai_sync_tkb_to_calendar(week_offset=0, use_ai=True)
    except Exception as e:
        logger.warning(f"[AI TKB Weekly Cron] {e}")


async def build_morning_report_embed():
    """(#91) Tạo Discord Embed bản tin sáng: Thời tiết, TKB, Google Calendar & Gợi ý."""
    import asyncio
    import discord
    import station_control

    sc = station_control.station_control
    briefing = await asyncio.to_thread(sc.build_morning_briefing)
    subjects_txt = "\n".join(f"• {s}" for s in (briefing.get("subjects") or [])) or "• Trống lịch học"
    events_list = briefing.get("events") or []
    events_txt = "\n".join(
        f"• `{e.get('start_time', 'Cả ngày')}` {e.get('title', '')}" for e in events_list
    ) or "• Không có sự kiện Calendar hôm nay"

    embed = discord.Embed(
        title=f"🌅 Bản Tin Sáng DeskFlow — {briefing.get('date')}",
        description=briefing.get("tts_text", "")[:350],
        color=0xFFFFFF,
    )
    embed.add_field(name="🌤 Thời tiết", value=briefing.get("weather") or "N/A", inline=True)
    embed.add_field(name="👔 Đồng phục", value=briefing.get("uniform") or "Tự do", inline=True)
    embed.add_field(name="📝 Ghi chú", value=f"{briefing.get('notes_count', 0)} ghi chú", inline=True)
    embed.add_field(name="📚 Thời khóa biểu hôm nay", value=subjects_txt[:1000], inline=False)
    embed.add_field(name="📅 Google Calendar", value=events_txt[:1000], inline=False)
    embed.set_footer(text="DeskFlow v2.2.0 • Daily 06:30 VN Report")
    return embed


async def send_morning_report(target=None) -> bool:
    """(#91) Gửi bản tin sáng tới DM Owner hoặc Discord channel/interaction."""
    try:
        embed = await build_morning_report_embed()
        if target is not None:
            await target.send(embed=embed)
            return True
        if not bot_ref:
            return False
        owner_id = getattr(config, "OWNER_USER_ID", None)
        if not owner_id:
            return False
        user = bot_ref.get_user(owner_id) or await bot_ref.fetch_user(owner_id)
        if user:
            await user.send(embed=embed)
            logger.info("[Scheduler] Sent daily Discord Morning Report DM.")
            return True
    except Exception as e:
        logger.warning(f"[Morning Report] Failed: {e}")
    return False


_esp32_offline_alerted = False
_last_triggered_alarm_min = ""


def _infer_gear_for_subjects(subjects: List[str]) -> List[str]:
    gear: List[str] = []
    joined = " ".join(subjects).lower()
    if "gdtc" in joined or "thể dục" in joined or "the duc" in joined:
        gear.append("Giày thể thao + Bình nước + Đồng phục TD")
    if "toán" in joined or "toan" in joined or "vật lí" in joined or "hóa" in joined or "khtn" in joined:
        gear.append("Máy tính Casio + Thước kẻ")
    if "địa" in joined or "dia" in joined:
        gear.append("Atlat Địa lí")
    if "mĩ thuật" in joined or "mi thuat" in joined:
        gear.append("Hộp màu + Giấy vẽ")
    if "tin" in joined:
        gear.append("USB / Tài liệu Tin học")
    if "anh" in joined or "văn" in joined:
        gear.append("Sách giáo khoa & Vở soạn bài")
    return gear


async def evening_prep_job():
    """(#3) 21:00 hằng đêm nhắc soạn cặp sách & đồng phục ngày mai."""
    try:
        import station_control
        dual_tkb = await get_dual_day_tkb()
        tom = (dual_tkb or {}).get("tomorrow") or {}
        if not tom.get("has_any_class"):
            return
        m_list = [s.split(": ")[-1].split(" (")[0] for s in tom.get("morning", []) if s.strip()]
        a_list = [s.split(": ")[-1].split(" (")[0] for s in tom.get("afternoon", []) if s.strip()]
        uniform = tom.get("dp_sang") or "Đồng phục quy định"
        gear = _infer_gear_for_subjects(m_list + a_list)
        msg = f"{tom.get('display_day', 'Ngày mai')}: Sáng ({', '.join(m_list) or 'Nghỉ'})"
        if a_list:
            msg += f" | Chiều ({', '.join(a_list)})"
        msg += f" • Đồng phục: {uniform}"
        if gear:
            msg += f" • Mang theo: {', '.join(gear)}"

        station_control.station_control.push_notification("evening_prep", "Nhắc soạn cặp 21:00", msg, "high")
        if bot_ref and getattr(config, "OWNER_USER_ID", None):
            user = bot_ref.get_user(config.OWNER_USER_ID) or await bot_ref.fetch_user(config.OWNER_USER_ID)
            if user:
                await user.send(f"🎒 **Nhắc soạn cặp 21:00:**\n{msg}")
    except Exception as e:
        logger.debug(f"[Evening Prep Job] {e}")


async def school_gear_checklist_job(session_kind: str = "morning"):
    """(#170) Nhắc đồ cần mang trước giờ học ~30 phút."""
    try:
        if calendar_service.is_today_holiday():
            return
        import station_control
        dual_tkb = await get_dual_day_tkb()
        today = (dual_tkb or {}).get("today") or {}
        slots = today.get("morning_slots" if session_kind == "morning" else "afternoon_slots") or []
        if not slots:
            return
        subjs = [s.get("subject", "") for s in slots if s.get("subject")]
        gear = _infer_gear_for_subjects(subjs)
        uniform = today.get("dp_sang" if session_kind == "morning" else "dp_chieu") or today.get("dp_sang") or ""
        body = f"Sắp vào ca {('Sáng' if session_kind == 'morning' else 'Chiều')}: {', '.join(subjs)}"
        if uniform:
            body += f" • Đồng phục: {uniform}"
        if gear:
            body += f" • Checklist: {', '.join(gear)}"
        station_control.station_control.push_notification("gear_checklist", "Checklist trước giờ học", body, "high")
    except Exception as e:
        logger.debug(f"[Gear Checklist Job] {e}")


async def esp32_offline_watchdog_job():
    """(#162) Cảnh báo khi ESP32 mất kết nối > 5 phút."""
    global _esp32_offline_alerted
    try:
        import station_control
        snap = station_control.station_control.get_station_snapshot()
        last_seen = float(snap.get("last_seen") or 0)
        if last_seen <= 0:
            return
        offline_sec = time.time() - last_seen
        if offline_sec > 300 and not _esp32_offline_alerted:
            _esp32_offline_alerted = True
            mins = int(offline_sec // 60)
            station_control.station_control.push_notification(
                "esp32_offline",
                "ESP32 Station Offline",
                f"Trạm DeskFlow mất kết nối MQTT/WiFi đã {mins} phút!",
                "urgent",
            )
            if bot_ref and getattr(config, "OWNER_USER_ID", None):
                user = bot_ref.get_user(config.OWNER_USER_ID) or await bot_ref.fetch_user(config.OWNER_USER_ID)
                if user:
                    await user.send(f"📡 **Cảnh báo Watchdog:** ESP32 DeskFlow đã offline `{mins}` phút!")
        elif offline_sec <= 120 and _esp32_offline_alerted:
            _esp32_offline_alerted = False
    except Exception as e:
        logger.debug(f"[Offline Watchdog] {e}")


async def scheduled_alarms_tick_job():
    """Kiểm tra danh sách báo thức đã lưu & (#18) tự bỏ qua ngày nghỉ lễ đánh dấu trên Lịch."""
    global _last_triggered_alarm_min
    try:
        if calendar_service.is_today_holiday():
            return
        import random
        import station_control
        from alarm_engine import alarm_engine
        now_vn = datetime.now(VN_TZ)
        cur_hhmm = now_vn.strftime("%H:%M")
        if cur_hhmm == _last_triggered_alarm_min:
            return
        cur_day = now_vn.weekday()  # 0=Mon..6=Sun
        alarms = station_control.station_control.get_alarms()
        for al in alarms:
            if not al.get("enabled", True):
                continue
            al_time = str(al.get("time") or "").strip()
            if al_time != cur_hhmm:
                continue
            days = al.get("days")
            if isinstance(days, list) and days and cur_day not in days:
                continue
            _last_triggered_alarm_min = cur_hhmm
            a = random.randint(12, 49)
            b = random.randint(13, 48)
            await alarm_engine.trigger_alarm(question=f"{a} + {b} = ?", solution=a + b)
            logger.info(f"[Scheduled Alarm] Triggered {cur_hhmm} ({al.get('label', 'Báo thức')})")
            break
    except Exception as e:
        logger.debug(f"[Scheduled Alarm Tick] {e}")


async def morning_report_job():
    await send_morning_report()


def setup_scheduler(bot, mqtt_client):
    global bot_ref, mqtt_ref, scheduler_instance
    bot_ref = bot
    mqtt_ref = mqtt_client

    scheduler = AsyncIOScheduler()
    scheduler_instance = scheduler
    
    # 1. Cập nhật thời tiết mỗi 5 phút (288 request/ngày)
    scheduler.add_job(weather_sync_job, "interval", minutes=5, id="weather_sync_job")
    
    # 2. Cập nhật TKB từ PocketBase mỗi 1 phút
    scheduler.add_job(tkb_auto_sync_job, "interval", minutes=1, id="tkb_sync_job")

    # 3. Đồng bộ Google Calendar mỗi 5 phút
    scheduler.add_job(calendar_sync_job, "interval", minutes=5, id="calendar_sync_job")
    
    # 4. Quét mưa theo cấu hình thời gian (Asia/Ho_Chi_Minh)
    try:
        import station_control
        alert_time = station_control.station_control.device_settings.get("weather_alert_time", "06:15")
        parts = alert_time.split(":")
        init_h, init_m = int(parts[0]), int(parts[1])
    except Exception:
        init_h, init_m = 6, 15

    scheduler.add_job(
        check_rain_job,
        CronTrigger(hour=init_h, minute=init_m, timezone="Asia/Ho_Chi_Minh"),
        id="check_rain_job",
        replace_existing=True
    )

    # 5. Ambient Cover Art Sync (4s) + Anti-oversleep Wakeup Tick (5s) + Calendar 15m Reminder (1m) + AI TKB Weekly (Mon 06:00)
    scheduler.add_job(ambient_cover_sync_job, "interval", seconds=4, id="ambient_cover_sync_job")
    scheduler.add_job(wakeup_check_job, "interval", seconds=5, id="wakeup_check_job")
    scheduler.add_job(calendar_upcoming_reminder_job, "interval", minutes=1, id="calendar_reminder_job")
    scheduler.add_job(
        ai_tkb_weekly_cron_job,
        CronTrigger(day_of_week="mon", hour=6, minute=0, timezone="Asia/Ho_Chi_Minh"),
        id="ai_tkb_weekly_cron_job",
        replace_existing=True,
    )
    scheduler.add_job(
        morning_report_job,
        CronTrigger(hour=6, minute=30, timezone="Asia/Ho_Chi_Minh"),
        id="morning_report_job",
        replace_existing=True,
    )
    # 6. (#3) 21:00 Night-before Prep, (#170) 30m Gear Checklist, (#162) Offline Watchdog, (#18) Alarm Cron
    scheduler.add_job(
        evening_prep_job,
        CronTrigger(hour=21, minute=0, timezone="Asia/Ho_Chi_Minh"),
        id="evening_prep_job",
        replace_existing=True,
    )
    scheduler.add_job(
        school_gear_checklist_job,
        CronTrigger(hour=6, minute=45, timezone="Asia/Ho_Chi_Minh"),
        kwargs={"session_kind": "morning"},
        id="morning_gear_job",
        replace_existing=True,
    )
    scheduler.add_job(
        school_gear_checklist_job,
        CronTrigger(hour=13, minute=20, timezone="Asia/Ho_Chi_Minh"),
        kwargs={"session_kind": "afternoon"},
        id="afternoon_gear_job",
        replace_existing=True,
    )
    scheduler.add_job(esp32_offline_watchdog_job, "interval", seconds=60, id="esp32_offline_watchdog_job")
    scheduler.add_job(scheduled_alarms_tick_job, "interval", seconds=20, id="scheduled_alarms_tick_job")
    
    scheduler.start()
    logger.info(f"[Scheduler] Đã kích hoạt lập lịch: Weather (5m), TKB (1m), Calendar (5m), Quét mưa ({init_h:02d}:{init_m:02d} Asia/Ho_Chi_Minh).")

    # Chạy đồng bộ ngay 1 lần khi vừa bật server
    bot.loop.create_task(tkb_auto_sync_job())
    bot.loop.create_task(weather_sync_job())
    bot.loop.create_task(calendar_sync_job())


def setup_focus_scheduler(focus_service, loop=None):
    """Integrates Focus Mode session deadline management and watchdog with AsyncIOScheduler."""
    global scheduler_instance
    if not scheduler_instance:
        logger.warning("[Scheduler] scheduler_instance not initialized. Cannot setup focus scheduler.")
        return

    async def focus_deadline_job():
        try:
            logger.info("[Scheduler] Focus deadline expired. Advancing session phase...")
            await focus_service.advance_deadline()
        except Exception as e:
            logger.error(f"[Scheduler] Error advancing focus deadline: {e}", exc_info=True)

    def schedule_deadline(ends_at_dt: Optional[datetime]):
        if not scheduler_instance:
            return
        if ends_at_dt is None:
            if scheduler_instance.get_job("focus_session_deadline"):
                scheduler_instance.remove_job("focus_session_deadline")
            return

        now_utc = datetime.now(timezone.utc)
        if ends_at_dt > now_utc:
            scheduler_instance.add_job(
                focus_deadline_job,
                "date",
                run_date=ends_at_dt,
                id="focus_session_deadline",
                replace_existing=True
            )
            logger.info(f"[Scheduler] Focus deadline job scheduled for {ends_at_dt.isoformat()}")
        else:
            # Deadline has already passed
            if loop:
                loop.create_task(focus_deadline_job())

    async def focus_watchdog_job():
        """Periodic safety net checking for passed deadlines or clock jumps."""
        try:
            state = focus_service.storage.get_state()
            if not state.get("enabled"):
                return
            sess = state.get("session") or state.get("current_session")
            if not sess or sess.get("status") != "active":
                return
            ends_at_str = sess.get("ends_at")
            if not ends_at_str:
                return
            ends_at_dt = parse_iso_utc(ends_at_str)
            now_utc = datetime.now(timezone.utc)
            if now_utc >= ends_at_dt:
                logger.info("[Scheduler] Watchdog detected passed deadline. Advancing phase...")
                await focus_service.advance_deadline()
        except Exception as e:
            logger.debug(f"[Scheduler] Watchdog check error: {e}")

    focus_service.set_deadline_callback(schedule_deadline)
    scheduler_instance.add_job(focus_watchdog_job, "interval", seconds=5, id="focus_watchdog_job", replace_existing=True)

    # Run synchronous downtime reconciliation on startup
    if loop:
        loop.create_task(focus_service.reconcile_downtime())
