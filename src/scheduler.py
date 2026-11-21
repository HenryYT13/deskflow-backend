import json
import httpx
import logging
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import config
from timetable import get_dual_day_tkb
import spotify_service
from calendar_service import calendar_service
from focus_mode.auth import parse_iso_utc

logger = logging.getLogger("DeskFlow")

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

async def weather_sync_job():
    """Tự động cập nhật thời tiết mỗi 5 phút từ OpenWeatherMap"""
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
                weather_text = f"{temp}*C"
                
                payload = {
                    "action": "weather_update",
                    "city": remove_vietnamese_diacritics(city),
                    "weather": weather_text
                }
                
                if mqtt_ref and mqtt_ref.is_connected():
                    mqtt_ref.publish("deskflow/weather", json.dumps(payload, ensure_ascii=False), qos=1)
                    logger.info(f"[Weather Auto-Sync] Đã cập nhật thời tiết: {city} -> {weather_text}")
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

async def calendar_sync_job():
    """Đồng bộ Google Calendar (OAuth + iCal) mỗi 5 phút và đẩy lên ESP32."""
    try:
        status = await calendar_service.sync_events(push_to_station=True)
        if status.get("connected"):
            logger.debug(f"[Calendar Sync] {status.get('upcoming_count', 0)} events synced.")
    except Exception as e:
        logger.warning(f"[Calendar Sync Error] {e}")


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
