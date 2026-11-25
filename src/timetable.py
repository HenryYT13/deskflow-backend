import httpx
from datetime import datetime, timedelta, timezone
import config

VN_TZ = timezone(timedelta(hours=7))

WEEKDAY_MAP = {
    0: "Thứ 2", 1: "Thứ 3", 2: "Thứ 4", 3: "Thứ 5", 4: "Thứ 6", 5: "Thứ 7", 6: "Chủ Nhật"
}

# Lịch ra vào lớp thực hiện từ 24/8/2026
MORNING_PERIOD_TIMES = {
    1: ("07:15", "08:00"),
    2: ("08:05", "08:50"),
    3: ("09:05", "09:50"),
    4: ("09:55", "10:40"),
    5: ("10:50", "11:35"),
}

AFTERNOON_PERIOD_TIMES = {
    1: ("14:00", "14:45"),
    2: ("14:50", "15:35"),
    3: ("15:40", "16:25"),
    4: ("16:30", "17:15"),
    5: ("17:20", "18:05"),
}

BELL_META = {
    "morning_arrive": "06:30 - 07:00",
    "morning_prep": "07:00 - 07:15",
    "afternoon_gate": "13:30",
    "afternoon_arrive": "13:50",
}

async def fetch_current_weekly_schedule() -> dict:
    """Lấy dữ liệu TKB của tuần đang active (is_hidden = False) từ PocketBase API"""
    url = f"{config.POCKETBASE_URL}/api/collections/weekly_schedules/records?filter=(is_hidden=false)&sort=-created&perPage=1"
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(url, timeout=10.0)
            if res.status_code == 200:
                data = res.json()
                items = data.get("items", [])
                if items:
                    record = items[0]
                    return {
                        "week_number": record.get("week_number", "N/A"),
                        "schedule_data": record.get("schedule_data", {})
                    }
                else:
                    print("[TKB Error] Không tìm thấy record TKB đang kích hoạt (is_hidden=false)!")
            else:
                print(f"[TKB Error] PocketBase trả về mã lỗi: {res.status_code}")
        except Exception as e:
            print(f"[TKB Error] Lỗi kết nối PocketBase: {e}")
    return {}

def parse_day_data(schedule_data: dict, day_name: str) -> dict:
    """Bóc tách các môn sáng, chiều, giờ từng tiết và đồng phục chuẩn xác."""
    empty_res = {
        "day": day_name,
        "morning": [],
        "morning_slots": [],
        "dp_sang": "Tự do",
        "has_afternoon": False,
        "afternoon": [],
        "afternoon_slots": [],
        "dp_chieu": "",
        "dismissal_morning": "",
        "dismissal_afternoon": "",
        "bell_meta": dict(BELL_META),
        "has_any_class": False,
    }

    if not schedule_data or not isinstance(schedule_data, dict):
        return empty_res

    day_variants = [day_name]
    if "Thứ " in day_name:
        day_variants.append(day_name.replace("Thứ ", "Thu "))
        day_variants.append(day_name.replace("Thứ ", "T"))

    def _extract_session(prefix: str, time_table: dict):
        subs = []
        slots = []
        for i in range(1, 6):
            subj = ""
            for d_var in day_variants:
                val = schedule_data.get(f"{prefix}-{d_var}-{i}", "")
                if val and val.strip():
                    subj = val.strip()
                    break
            if subj:
                st, en = time_table.get(i, ("", ""))
                subs.append(f"Tiết {i}: {subj} ({st}-{en})" if st else f"Tiết {i}: {subj}")
                slots.append({
                    "period": i,
                    "subject": subj,
                    "start_time": st,
                    "end_time": en,
                    "session": "morning" if prefix == "sang" else "afternoon",
                })
        return subs, slots

    morning_subs, morning_slots = _extract_session("sang", MORNING_PERIOD_TIMES)
    afternoon_subs, afternoon_slots = _extract_session("chieu", AFTERNOON_PERIOD_TIMES)

    dp_sang = "Tự do"
    dp_chieu = ""
    for d_var in day_variants:
        s_val = schedule_data.get(f"sang-dp-{d_var}", "")
        c_val = schedule_data.get(f"chieu-dp-{d_var}", "")
        if s_val and s_val.strip():
            dp_sang = s_val.strip()
        if c_val and c_val.strip():
            dp_chieu = c_val.strip()

    has_afternoon = len(afternoon_subs) > 0
    has_any = (len(morning_subs) > 0) or has_afternoon
    dismissal_m = morning_slots[-1]["end_time"] if morning_slots else ""
    dismissal_a = afternoon_slots[-1]["end_time"] if afternoon_slots else ""

    return {
        "day": day_name,
        "morning": morning_subs,
        "morning_slots": morning_slots,
        "dp_sang": dp_sang,
        "has_afternoon": has_afternoon,
        "afternoon": afternoon_subs if has_afternoon else [],
        "afternoon_slots": afternoon_slots if has_afternoon else [],
        "dp_chieu": dp_chieu if has_afternoon else "",
        "dismissal_morning": dismissal_m,
        "dismissal_afternoon": dismissal_a,
        "bell_meta": dict(BELL_META),
        "has_any_class": has_any,
    }

async def get_processed_daily_tkb(target_day: str = None) -> dict:
    """Lấy TKB của một ngày cụ thể (mặc định là ngày hôm nay)"""
    sched_info = await fetch_current_weekly_schedule()
    data = sched_info.get("schedule_data", {})
    if not data:
        return {}

    if not target_day:
        target_day = WEEKDAY_MAP.get(datetime.now(VN_TZ).weekday(), "Thứ 2")

    result = parse_day_data(data, target_day)
    result["week_number"] = sched_info.get("week_number", "N/A")
    return result

async def get_dual_day_tkb() -> dict:
    """
    Lấy dữ liệu TKB:
    - Khối Hôm nay
    - Khối Ngày mai: Nếu ngày mai không có tiết thì tự động nhảy sang ngày học gần nhất (ví dụ Thứ 2 tới)
    """
    sched_info = await fetch_current_weekly_schedule()
    data = sched_info.get("schedule_data", {})
    week_no = sched_info.get("week_number", "N/A")
    if not data:
        return {}

    now = datetime.now(VN_TZ)
    cur_idx = now.weekday()  # Thứ 2 là 0, Thứ 4 là 2, Thứ 5 là 3

    # 1. Hôm nay (Nếu hôm nay là Thứ 7 hoặc CN mà không có lịch học thì lấy Thứ 2 làm hôm nay)
    today_str = WEEKDAY_MAP.get(cur_idx, "Thứ 2")
    today_data = parse_day_data(data, today_str)
    if not today_data.get("has_any_class") and cur_idx >= 5:
        today_str = "Thứ 2"
        today_data = parse_day_data(data, today_str)

    # 2. Quét tìm ngày học kế tiếp có môn (offset = 1..6)
    next_day_data = None
    next_day_label = ""

    for offset in range(1, 7):
        target_idx = (cur_idx + offset) % 7
        target_name = WEEKDAY_MAP.get(target_idx, "Thứ 2")
        candidate = parse_day_data(data, target_name)

        if candidate.get("has_any_class", False):
            next_day_data = candidate
            if offset == 1:
                next_day_label = target_name  # Ví dụ: "Thứ 5"
            else:
                next_day_label = f"{target_name} tới"  # Ví dụ: "Thứ 2 tới"
            break

    # Nếu cả tuần đều không có tiết nào, fallback về Thứ 2
    if not next_day_data:
        next_day_data = parse_day_data(data, "Thứ 2")
        next_day_label = "Thứ 2"

    next_day_data["display_day"] = next_day_label

    return {
        "week_number": week_no,
        "today": today_data,
        "tomorrow": next_day_data
    }


def build_station_payload(dual_tkb: dict) -> dict:
    """Build the ESP32 MQTT payload (ASCII-normalised) from a dual-day TKB."""
    import unicodedata

    def clean(text) -> str:
        if not text:
            return ""
        text = str(text).replace("đ", "d").replace("Đ", "D")
        nfd = unicodedata.normalize("NFD", text)
        return "".join(c for c in nfd if unicodedata.category(c) != "Mn")

    today = (dual_tkb or {}).get("today") or {}
    tom = (dual_tkb or {}).get("tomorrow") or {}

    def _esp_subj(item_slots, fallback_list):
        if item_slots:
            return [clean(s["subject"]) for s in item_slots]
        return [clean(s.split(": ", 1)[-1].split(" (", 1)[0]) for s in fallback_list if s.strip()]

    morning = _esp_subj(today.get("morning_slots"), today.get("morning", []))
    afternoon = _esp_subj(today.get("afternoon_slots"), today.get("afternoon", []))
    has_afternoon = bool(today.get("has_afternoon")) and bool(afternoon)

    dp_sang = clean(today.get("dp_sang", "Tu do"))
    dp_chieu = clean(today.get("dp_chieu", ""))
    if has_afternoon and dp_chieu and dp_chieu != dp_sang:
        uniform = f"Sang: {dp_sang}, Chieu: {dp_chieu}"
    elif has_afternoon:
        uniform = f"{dp_sang} (chung ca ngay)"
    else:
        uniform = dp_sang

    tom_morning = _esp_subj(tom.get("morning_slots"), tom.get("morning", []))
    tom_afternoon = _esp_subj(tom.get("afternoon_slots"), tom.get("afternoon", []))
    tom_has_afternoon = bool(tom.get("has_afternoon")) and bool(tom_afternoon)
    tom_dp_sang = clean(tom.get("dp_sang", "Tu do"))
    tom_dp_chieu = clean(tom.get("dp_chieu", ""))
    if tom_has_afternoon and tom_dp_chieu and tom_dp_chieu != tom_dp_sang:
        tom_uniform = f"Sang: {tom_dp_sang}, Chieu: {tom_dp_chieu}"
    elif tom_has_afternoon:
        tom_uniform = f"{tom_dp_sang} (chung ca ngay)"
    else:
        tom_uniform = tom_dp_sang

    return {
        "action": "sync_data",
        "view": "tkb",
        "day_name": clean(today.get("day", "Thu 4")),
        "morning": ", ".join(morning) if morning else "Nghi hoc",
        "has_afternoon": has_afternoon,
        "afternoon": ", ".join(afternoon) if has_afternoon else "",
        "uniform": uniform,
        "tom_day": clean(tom.get("display_day", tom.get("day", "Ngay mai"))),
        "tom_morning": ", ".join(tom_morning) if tom_morning else "Nghi hoc",
        "tom_has_afternoon": tom_has_afternoon,
        "tom_afternoon": ", ".join(tom_afternoon) if tom_has_afternoon else "",
        "tom_uniform": tom_uniform,
    }
