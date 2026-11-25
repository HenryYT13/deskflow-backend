"""DeskFlow v2.4.0 backend feature store: custom formulas, teachers (#167),
device battery/call (#315/#317), GitHub multi-repo CI (#432), serial logs (#373),
and release changelog (#397).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("DESKFLOW_DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
V24_STATE_FILE = DATA_DIR / "v24_state.json"


def _load_state() -> dict[str, Any]:
    if V24_STATE_FILE.exists():
        try:
            return json.loads(V24_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "custom_formulas": [],
        "teachers": [],
        "github_repos": ["HenryYT13/deskflow-app", "HenryYT13/deskflow-backend"],
        "phone_battery": {"level": None, "charging": False, "updated_at": 0},
        "last_call": None,
    }


def _save_state(state: dict[str, Any]) -> None:
    V24_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    V24_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Custom Formulas ---
def get_custom_formulas() -> list[dict[str, Any]]:
    return _load_state().get("custom_formulas", [])


def add_custom_formula(subject: str, title: str, formula: str, note: str = "") -> dict[str, Any]:
    state = _load_state()
    items = state.setdefault("custom_formulas", [])
    item = {
        "id": f"cf_{int(time.time() * 1000)}",
        "subject": (subject or "Khác").strip(),
        "title": title.strip(),
        "formula": formula.strip(),
        "note": note.strip(),
        "custom": True,
    }
    items.insert(0, item)
    _save_state(state)
    return item


def delete_custom_formula(formula_id: str) -> bool:
    state = _load_state()
    items = state.get("custom_formulas", [])
    new_items = [x for x in items if x.get("id") != formula_id]
    if len(new_items) == len(items):
        return False
    state["custom_formulas"] = new_items
    _save_state(state)
    return True


# --- #167 Teacher Directory ---
def get_teachers() -> list[dict[str, Any]]:
    return _load_state().get("teachers", [])


def add_teacher(name: str, subject: str, phone: str = "", zalo: str = "", email: str = "", note: str = "") -> dict[str, Any]:
    state = _load_state()
    items = state.setdefault("teachers", [])
    item = {
        "id": f"tch_{int(time.time() * 1000)}",
        "name": name.strip(),
        "subject": subject.strip(),
        "phone": phone.strip(),
        "zalo": zalo.strip(),
        "email": email.strip(),
        "note": note.strip(),
    }
    items.append(item)
    _save_state(state)
    return item


def delete_teacher(teacher_id: str) -> bool:
    state = _load_state()
    items = state.get("teachers", [])
    new_items = [x for x in items if x.get("id") != teacher_id]
    if len(new_items) == len(items):
        return False
    state["teachers"] = new_items
    _save_state(state)
    return True


# --- #315 Phone Battery & #317 Incoming Call ---
def update_phone_battery(level: int, charging: bool = False) -> dict[str, Any]:
    state = _load_state()
    state["phone_battery"] = {
        "level": max(0, min(100, int(level))),
        "charging": bool(charging),
        "updated_at": int(time.time()),
    }
    _save_state(state)
    return state["phone_battery"]


def get_phone_battery() -> dict[str, Any]:
    return _load_state().get("phone_battery", {"level": None, "charging": False, "updated_at": 0})


def record_incoming_call(caller: str, number: str = "") -> dict[str, Any]:
    state = _load_state()
    call_info = {"caller": caller.strip() or "Cuộc gọi đến", "number": number.strip(), "ts": int(time.time())}
    state["last_call"] = call_info
    _save_state(state)
    return call_info


# --- #432 GitHub Multi-Repo CI Monitor ---
def get_github_repos() -> list[str]:
    return _load_state().get("github_repos", ["HenryYT13/deskflow-app"])


def add_github_repo(repo: str) -> list[str]:
    repo = repo.strip().strip("/")
    state = _load_state()
    repos = state.setdefault("github_repos", [])
    if repo and repo not in repos:
        repos.append(repo)
        _save_state(state)
    return repos


def delete_github_repo(repo: str) -> list[str]:
    state = _load_state()
    repos = [r for r in state.get("github_repos", []) if r != repo]
    state["github_repos"] = repos
    _save_state(state)
    return repos


def fetch_github_ci_summary() -> list[dict[str, Any]]:
    results = []
    for repo in get_github_repos()[:6]:
        entry: dict[str, Any] = {"repo": repo, "status": "unknown", "conclusion": "unknown", "workflow": ""}
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/actions/runs?per_page=1",
                headers={"User-Agent": "DeskFlow-Monitor", "Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                runs = data.get("workflow_runs") or []
                if runs:
                    r = runs[0]
                    entry["status"] = r.get("status", "unknown")
                    entry["conclusion"] = r.get("conclusion") or r.get("status", "unknown")
                    entry["workflow"] = r.get("name", "")
                    entry["url"] = r.get("html_url", "")
                    entry["updated_at"] = r.get("updated_at", "")
        except Exception as exc:
            entry["error"] = str(exc)
        results.append(entry)
    return results


# --- #373 Live Serial Log Buffer ---
_SERIAL_LOGS: list[dict[str, Any]] = []


def append_serial_log(line: str) -> None:
    _SERIAL_LOGS.append({"ts": int(time.time()), "line": line})
    if len(_SERIAL_LOGS) > 200:
        del _SERIAL_LOGS[: len(_SERIAL_LOGS) - 200]


def get_serial_logs(limit: int = 80) -> list[dict[str, Any]]:
    return _SERIAL_LOGS[-limit:]


# --- #397 Changelog ---
CHANGELOG_V240 = {
    "version": "2.4.0",
    "date": "2026-11-25",
    "highlights": [
        "ESP32: Danh sách màn hình dạng lưới 6 ô/trang, đồng hồ kim 60fps (#3), 4 font chữ (#52), hiệu ứng chuyển cảnh (#53).",
        "ESP32: Biểu đồ nhiệt độ 24h (#22), cảnh báo mưa (#23), AQI + lời khuyên (#24), Lịch Âm VN (#25), đếm ngược Tết/Hè/Noel (#26), Year Progress (#33), Bấm giờ (#30), Nhắc uống nước (#35), Xúc xắc/Tung xu (#58), Cookie Clicker (#133).",
        "Công thức Custom (thêm/xóa) đồng bộ Web, Android & ESP32; Danh bạ giáo viên (#167); LaTeX trong Ghi chú (#188) & Export Markdown (#215).",
        "Web: PWA (#365), Kéo thả file (#366), 5 Theme (#368), WebSerial Flash & Wi-Fi Config (#371/#372), Live Serial Monitor (#373), Onboarding Tour (#499).",
        "Spotify: BPM/Energy/Valence (#259), Lịch sử 20 bài (#264), Tạo ảnh Now Playing Card (#265), Đĩa than xoay (#296).",
        "Android: 5 Widget mới (#301-#305), Báo thức quét QR (#269), Đồng bộ pin điện thoại (#315), Báo cuộc gọi lên ESP32 (#317).",
        "CI/CD & Automation: Bot Discord nâng cao (#429), Giám sát GitHub Actions nhiều repo (#432), Auto-updater Desktop (#396).",
    ],
}
