import io
import json
import os
import re
import httpx
from datetime import datetime, timedelta, timezone
import config

VN_TZ = timezone(timedelta(hours=7))
CUSTOM_TKB_FILE = os.path.join(os.path.dirname(__file__), "data", "tkb_custom.json")

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
    """Lấy dữ liệu TKB: ưu tiên file Excel/custom nếu đã import, ngược lại lấy từ PocketBase API."""
    try:
        if os.path.exists(CUSTOM_TKB_FILE):
            with open(CUSTOM_TKB_FILE, "r", encoding="utf-8") as f:
                custom = json.load(f)
            if isinstance(custom, dict) and custom.get("schedule_data"):
                return {
                    "week_number": custom.get("week_number", "Custom Excel"),
                    "schedule_data": custom.get("schedule_data", {}),
                    "source": "excel_custom",
                    "uploaded_at": custom.get("uploaded_at"),
                }
    except Exception as e:
        print(f"[TKB Custom Error] {e}")

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

    live = get_live_period_status(today)
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
        "live_label": clean(live.get("label", "")),
        "live_subject": clean(live.get("current_subject", "")),
        "live_mins_left": int(live.get("minutes_left", 0) or 0),
        "dismissal_time": str(live.get("dismissal_time") or ""),
        "mins_to_dismissal": int(live.get("minutes_to_dismissal", 0) or 0),
    }


def _hhmm_to_min(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":", 1)
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def get_live_period_status(today_data: dict | None) -> dict:
    """(#1) Tính tiết học hiện tại, số phút còn lại trong tiết và đếm ngược giờ ra về."""
    now = datetime.now(VN_TZ)
    cur_min = now.hour * 60 + now.minute
    today_data = today_data or {}
    m_slots = today_data.get("morning_slots") or []
    a_slots = today_data.get("afternoon_slots") or []
    has_m = len(m_slots) > 0
    has_a = len(a_slots) > 0
    dismissal_m = today_data.get("dismissal_morning") or (m_slots[-1]["end_time"] if m_slots else "11:35")
    dismissal_a = today_data.get("dismissal_afternoon") or (a_slots[-1]["end_time"] if a_slots else "16:25")

    res = {
        "now_time": now.strftime("%H:%M"),
        "state": "idle",
        "label": "Ngoài giờ học",
        "current_subject": "",
        "period": None,
        "session": None,
        "minutes_left": 0,
        "next_subject": "",
        "next_time": "",
        "dismissal_time": "",
        "minutes_to_dismissal": 0,
    }

    def _scan_session(slots: list, sess_name: str, arrive_min: int, prep_min: int, start_min: int, dismissal_str: str):
        d_min = _hhmm_to_min(dismissal_str)
        if arrive_min <= cur_min < prep_min:
            return {
                **res,
                "state": "arrive",
                "label": f"Có mặt tại lớp ({sess_name})",
                "session": sess_name,
                "minutes_left": prep_min - cur_min,
                "next_subject": slots[0]["subject"] if slots else "",
                "next_time": slots[0]["start_time"] if slots else "",
                "dismissal_time": dismissal_str,
                "minutes_to_dismissal": max(0, d_min - cur_min) if d_min > 0 else 0,
            }
        if prep_min <= cur_min < start_min:
            return {
                **res,
                "state": "prep",
                "label": "Ổn định / Ôn bài đầu giờ",
                "session": sess_name,
                "minutes_left": start_min - cur_min,
                "next_subject": slots[0]["subject"] if slots else "",
                "next_time": slots[0]["start_time"] if slots else "",
                "dismissal_time": dismissal_str,
                "minutes_to_dismissal": max(0, d_min - cur_min) if d_min > 0 else 0,
            }
        for idx, sl in enumerate(slots):
            s_m = _hhmm_to_min(sl.get("start_time", ""))
            e_m = _hhmm_to_min(sl.get("end_time", ""))
            if s_m <= cur_min < e_m:
                nxt = slots[idx + 1] if idx + 1 < len(slots) else None
                return {
                    **res,
                    "state": "class",
                    "label": f"Tiết {sl['period']} ({sess_name})",
                    "current_subject": sl["subject"],
                    "period": sl["period"],
                    "session": sess_name,
                    "minutes_left": e_m - cur_min,
                    "next_subject": nxt["subject"] if nxt else "Ra về",
                    "next_time": nxt["start_time"] if nxt else dismissal_str,
                    "dismissal_time": dismissal_str,
                    "minutes_to_dismissal": max(0, d_min - cur_min) if d_min > 0 else 0,
                }
            if idx + 1 < len(slots):
                n_m = _hhmm_to_min(slots[idx + 1].get("start_time", ""))
                if e_m <= cur_min < n_m:
                    return {
                        **res,
                        "state": "break",
                        "label": "Giờ ra chơi",
                        "session": sess_name,
                        "minutes_left": n_m - cur_min,
                        "next_subject": f"Tiết {slots[idx+1]['period']}: {slots[idx+1]['subject']}",
                        "next_time": slots[idx + 1]["start_time"],
                        "dismissal_time": dismissal_str,
                        "minutes_to_dismissal": max(0, d_min - cur_min) if d_min > 0 else 0,
                    }
        return None

    if has_m and 390 <= cur_min <= _hhmm_to_min(dismissal_m):
        found = _scan_session(m_slots, "Sáng", 390, 420, 435, dismissal_m)
        if found:
            return found
    if has_a and 810 <= cur_min <= _hhmm_to_min(dismissal_a):
        found = _scan_session(a_slots, "Chiều", 810, 830, 840, dismissal_a)
        if found:
            return found

    if has_m and cur_min < 390:
        res["next_subject"] = f"Sáng: {m_slots[0]['subject']}"
        res["next_time"] = m_slots[0]["start_time"]
        res["dismissal_time"] = dismissal_m
    elif has_a and cur_min < 810:
        res["state"] = "lunch"
        res["label"] = "Nghỉ trưa — chuẩn bị ca Chiều"
        res["next_subject"] = f"Chiều: {a_slots[0]['subject']}"
        res["next_time"] = a_slots[0]["start_time"]
        res["minutes_left"] = 810 - cur_min
        res["dismissal_time"] = dismissal_a
    elif has_m or has_a:
        res["state"] = "dismissed"
        res["label"] = "Đã tan học hôm nay"
    return res


def get_weekly_subject_stats(schedule_data: dict) -> dict:
    """(#17) Tổng hợp số tiết & số giờ học từng môn trong tuần (45p = 0.75h/tiết)."""
    counts: dict[str, int] = {}
    morning_total = 0
    afternoon_total = 0
    for day in ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7"]:
        day_parsed = parse_day_data(schedule_data, day)
        for sl in day_parsed.get("morning_slots", []):
            subj = sl["subject"].strip()
            if subj:
                counts[subj] = counts.get(subj, 0) + 1
                morning_total += 1
        for sl in day_parsed.get("afternoon_slots", []):
            subj = sl["subject"].strip()
            if subj:
                counts[subj] = counts.get(subj, 0) + 1
                afternoon_total += 1

    total_periods = sum(counts.values())
    items = [
        {
            "subject": subj,
            "periods": cnt,
            "hours": round(cnt * 0.75, 2),
            "pct": round((cnt / total_periods) * 100, 1) if total_periods else 0.0,
        }
        for subj, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "total_periods": total_periods,
        "total_hours": round(total_periods * 0.75, 2),
        "morning_periods": morning_total,
        "afternoon_periods": afternoon_total,
        "subjects": items,
    }


def _normalize_day_token(raw: str) -> str | None:
    s = raw.strip().lower()
    for d in range(2, 8):
        if f"thứ {d}" in s or f"thu {d}" in s or re.search(rf"\bt{d}\b", s) or s == str(d):
            return f"Thứ {d}"
    return None


def parse_excel_timetable(file_bytes: bytes, week_number: str | None = None) -> dict:
    """(#16) Parse file Excel (.xlsx) thành schedule_data chuẩn DeskFlow & lưu custom override."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = [
        [("" if c is None else str(c).strip()) for c in row]
        for row in ws.iter_rows(values_only=True)
    ]
    rows = [r for r in rows if any(cell for cell in r)]
    if not rows:
        raise ValueError("excel_empty_sheet")

    schedule_data: dict[str, str] = {}

    header_idx = -1
    day_cols: dict[int, str] = {}
    for r_i, row in enumerate(rows[:6]):
        found_cols: dict[int, str] = {}
        for c_i, val in enumerate(row):
            dn = _normalize_day_token(val)
            if dn:
                found_cols[c_i] = dn
        if len(found_cols) >= 2:
            header_idx = r_i
            day_cols = found_cols
            break

    if header_idx >= 0 and day_cols:
        current_sess = "sang"
        for row in rows[header_idx + 1 :]:
            label = " ".join(row[:2]).lower() if len(row) >= 2 else (row[0].lower() if row else "")
            if "chiều" in label or "chieu" in label:
                current_sess = "chieu"
            elif "sáng" in label or "sang" in label:
                current_sess = "sang"

            if "đồng phục" in label or "dong phuc" in label or "uniform" in label:
                for c_i, dn in day_cols.items():
                    if c_i < len(row) and row[c_i]:
                        schedule_data[f"{current_sess}-dp-{dn}"] = row[c_i]
                continue

            m_p = re.search(r"ti[eế]t\s*([1-5])|\b([1-5])\b", label)
            if m_p:
                p_num = int(m_p.group(1) or m_p.group(2))
                for c_i, dn in day_cols.items():
                    if c_i < len(row) and row[c_i]:
                        schedule_data[f"{current_sess}-{dn}-{p_num}"] = row[c_i]
    else:
        for row in rows:
            dn = _normalize_day_token(row[0]) if row else None
            if not dn:
                continue
            row_txt = " ".join(row[:2]).lower()
            sess = "chieu" if ("chiều" in row_txt or "chieu" in row_txt) else "sang"
            offset = 2 if len(row) > 2 and ("sáng" in row[1].lower() or "chiều" in row[1].lower() or "sang" in row[1].lower() or "chieu" in row[1].lower()) else 1
            for p_idx in range(1, 6):
                col = offset + (p_idx - 1)
                if col < len(row) and row[col]:
                    schedule_data[f"{sess}-{dn}-{p_idx}"] = row[col]
            if offset + 5 < len(row) and row[offset + 5]:
                schedule_data[f"{sess}-dp-{dn}"] = row[offset + 5]

    if not schedule_data:
        raise ValueError("excel_unrecognized_timetable_format")

    os.makedirs(os.path.dirname(CUSTOM_TKB_FILE), exist_ok=True)
    payload = {
        "week_number": week_number or "Excel Custom",
        "uploaded_at": datetime.now(VN_TZ).isoformat(),
        "schedule_data": schedule_data,
    }
    with open(CUSTOM_TKB_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return payload


def clear_custom_timetable() -> bool:
    if os.path.exists(CUSTOM_TKB_FILE):
        os.remove(CUSTOM_TKB_FILE)
        return True
    return False


QUICK_FORMULAS = [
    {"id": "quad", "subject": "Toán", "title": "Phương trình bậc 2", "formula": "Δ = b² - 4ac | x = (-b ± √Δ) / (2a)"},
    {"id": "vieta", "subject": "Toán", "title": "Hệ thức Vi-ét", "formula": "x₁ + x₂ = -b/a | x₁·x₂ = c/a"},
    {"id": "pyth", "subject": "Toán", "title": "Tam giác vuông", "formula": "a² + b² = c² | S = ½·a·b | sin²α + cos²α = 1"},
    {"id": "ohm", "subject": "Vật lí", "title": "Định luật Ôm & Công suất", "formula": "I = U / R | P = U·I = I²R = U²/R | Q = I²Rt"},
    {"id": "kinetic", "subject": "Vật lí", "title": "Cơ năng & Động năng", "formula": "Wđ = ½mv² | Wt = mgh | p = mv | v = v₀ + at"},
    {"id": "optics", "subject": "Vật lí", "title": "Thấu kính & Sóng", "formula": "1/f = 1/d + 1/d' | λ = v·T = v/f"},
    {"id": "mol", "subject": "Hóa học", "title": "Số mol & Nồng độ", "formula": "n = m/M = V(đkc)/24.79 | C% = (mct/mdd)·100% | CM = n/V"},
    {"id": "gas", "subject": "Hóa học", "title": "Tỉ khối & Bảo toàn e", "formula": "d(A/B) = MA/MB | Σn(nhường) = Σn(nhận) | pV = nRT"},
]

PERIODIC_ELEMENTS = [
    {"z": 1, "sym": "H", "name": "Hydro", "mass": 1.0, "val": "I", "group": "IA"},
    {"z": 2, "sym": "He", "name": "Heli", "mass": 4.0, "val": "0", "group": "VIIIA"},
    {"z": 6, "sym": "C", "name": "Cacbon", "mass": 12.0, "val": "II, IV", "group": "IVA"},
    {"z": 7, "sym": "N", "name": "Nitơ", "mass": 14.0, "val": "III, V", "group": "VA"},
    {"z": 8, "sym": "O", "name": "Oxi", "mass": 16.0, "val": "II", "group": "VIA"},
    {"z": 11, "sym": "Na", "name": "Natri", "mass": 23.0, "val": "I", "group": "IA"},
    {"z": 12, "sym": "Mg", "name": "Magie", "mass": 24.0, "val": "II", "group": "IIA"},
    {"z": 13, "sym": "Al", "name": "Nhôm", "mass": 27.0, "val": "III", "group": "IIIA"},
    {"z": 14, "sym": "Si", "name": "Silic", "mass": 28.0, "val": "IV", "group": "IVA"},
    {"z": 15, "sym": "P", "name": "Photpho", "mass": 31.0, "val": "III, V", "group": "VA"},
    {"z": 16, "sym": "S", "name": "Lưu huỳnh", "mass": 32.0, "val": "II, IV, VI", "group": "VIA"},
    {"z": 17, "sym": "Cl", "name": "Clo", "mass": 35.5, "val": "I", "group": "VIIA"},
    {"z": 19, "sym": "K", "name": "Kali", "mass": 39.0, "val": "I", "group": "IA"},
    {"z": 20, "sym": "Ca", "name": "Canxi", "mass": 40.0, "val": "II", "group": "IIA"},
    {"z": 26, "sym": "Fe", "name": "Sắt", "mass": 56.0, "val": "II, III", "group": "VIIIB"},
    {"z": 29, "sym": "Cu", "name": "Đồng", "mass": 64.0, "val": "I, II", "group": "IB"},
    {"z": 30, "sym": "Zn", "name": "Kẽm", "mass": 65.0, "val": "II", "group": "IIB"},
    {"z": 47, "sym": "Ag", "name": "Bạc", "mass": 108.0, "val": "I", "group": "IB"},
    {"z": 56, "sym": "Ba", "name": "Bari", "mass": 137.0, "val": "II", "group": "IIA"},
    {"z": 79, "sym": "Au", "name": "Vàng", "mass": 197.0, "val": "I, III", "group": "IB"},
]
