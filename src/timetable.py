import httpx
from datetime import datetime, timedelta
import config

WEEKDAY_MAP = {
    0: "Thứ 2", 1: "Thứ 3", 2: "Thứ 4", 3: "Thứ 5", 4: "Thứ 6", 5: "Thứ 7", 6: "Chủ Nhật"
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
    """Bóc tách các môn sáng, chiều và đồng phục chuẩn xác, chống trượt key"""
    empty_res = {
        "day": day_name,
        "morning": [],
        "dp_sang": "Tự do",
        "has_afternoon": False,
        "afternoon": [],
        "dp_chieu": "",
        "has_any_class": False,
    }

    if not schedule_data or not isinstance(schedule_data, dict):
        return empty_res

    # Danh sách các biến thể tên ngày để dò key (VD: Thứ 4, Thu 4, T4)
    day_variants = [day_name]
    if "Thứ " in day_name:
        day_variants.append(day_name.replace("Thứ ", "Thu "))
        day_variants.append(day_name.replace("Thứ ", "T"))

    # 1. Bóc tách tiết Sáng
    morning_subs = []
    for i in range(1, 6):
        subj = ""
        for d_var in day_variants:
            val = schedule_data.get(f"sang-{d_var}-{i}", "")
            if val and val.strip():
                subj = val.strip()
                break
        if subj:
            morning_subs.append(f"Tiết {i}: {subj}")

    # 2. Bóc tách tiết Chiều
    afternoon_subs = []
    for i in range(1, 6):
        subj = ""
        for d_var in day_variants:
            val = schedule_data.get(f"chieu-{d_var}-{i}", "")
            if val and val.strip():
                subj = val.strip()
                break
        if subj:
            afternoon_subs.append(f"Tiết {i}: {subj}")

    # 3. Bóc tách Đồng phục
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

    return {
        "day": day_name,
        "morning": morning_subs,
        "dp_sang": dp_sang,
        "has_afternoon": has_afternoon,
        "afternoon": afternoon_subs if has_afternoon else [],
        "dp_chieu": dp_chieu if has_afternoon else "",
        "has_any_class": has_any,
    }

async def get_processed_daily_tkb(target_day: str = None) -> dict:
    """Lấy TKB của một ngày cụ thể (mặc định là ngày hôm nay)"""
    sched_info = await fetch_current_weekly_schedule()
    data = sched_info.get("schedule_data", {})
    if not data:
        return {}

    if not target_day:
        target_day = WEEKDAY_MAP.get(datetime.now().weekday(), "Thứ 2")

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

    now = datetime.now()
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