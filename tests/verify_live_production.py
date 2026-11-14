import urllib.request
import json
import sys
import os

BASE = "https://df.api.thetechdude.io.vn"
TOKEN = os.environ.get("DESKFLOW_TEST_TOKEN")
if not TOKEN:
    print("Set DESKFLOW_TEST_TOKEN in the environment to run authenticated live checks.", file=sys.stderr)
    sys.exit(2)
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def api(method, path, data=None):
    url = BASE + path
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, headers=HEADERS, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

print("=== 1. TEST ALL 9 DISPLAY VIEWS ===")
views = ["main", "stats", "tkb", "weather", "lyrics", "clock", "notes", "countdown", "focus"]
for v in views:
    res = api("POST", "/api/app/control/display/view", {"view": v})
    assert res.get("success") is True, f"Failed on view {v}: {res}"
    print(f"  [PASS] View: {v} -> {res}")

print("\n=== 2. TEST SCHEDULED DARK MODE ===")
dm_res = api("POST", "/api/app/control/settings", {
    "darkmode_mode": "scheduled",
    "darkmode_start": "22:00",
    "darkmode_end": "06:00"
})
assert dm_res.get("success") is True
s = dm_res.get("data", {})
assert s.get("darkmode_mode") == "scheduled", f"darkmode_mode mismatch: {s}"
print(f"  [PASS] Darkmode settings updated: mode={s.get('darkmode_mode')}, {s.get('darkmode_start')} -> {s.get('darkmode_end')}")

print("\n=== 3. TEST NOTES DUAL SCHEMA & NON-DESTRUCTIVE DELETE ===")
get_notes_res = api("GET", "/api/app/control/notes")
assert get_notes_res.get("success") is True
initial_notes = get_notes_res.get("notes", [])
initial_ids = [n["id"] for n in initial_notes]
print(f"  Existing notes count: {len(initial_notes)} (IDs: {initial_ids})")

# Create temporary test note
add_res = api("POST", "/api/app/control/notes", {"text": "Temporary Verification Note Agent", "color": "#A7F3D0"})
assert add_res.get("success") is True
created_note = add_res.get("note") or add_res.get("data")
test_note_id = created_note["id"]
print(f"  [PASS] Created test note ID: {test_note_id}, text: {created_note.get('text')}, content: {created_note.get('content')}")

# Verify list contains both old and new note
notes_after_add = api("GET", "/api/app/control/notes").get("notes", [])
assert any(n["id"] == test_note_id for n in notes_after_add)

# Delete ONLY the temporary note
del_res = api("DELETE", f"/api/app/control/notes/{test_note_id}")
assert del_res.get("success") is True
remaining_notes = del_res.get("notes", [])
remaining_ids = [n["id"] for n in remaining_notes]
assert test_note_id not in remaining_ids, "Test note was not deleted!"
for old_id in initial_ids:
    assert old_id in remaining_ids, f"CRITICAL: Old note {old_id} was lost during deletion!"
print(f"  [PASS] Successfully deleted test note {test_note_id}. All {len(initial_ids)} pre-existing notes preserved intact!")

print("\n=== 4. TEST COUNTDOWNS DUAL SCHEMA ===")
get_cd_res = api("GET", "/api/app/control/countdowns")
assert get_cd_res.get("success") is True
assert "countdowns" in get_cd_res and "data" in get_cd_res
print(f"  [PASS] Countdowns fetched: count={len(get_cd_res.get('countdowns', []))}")
for cd in get_cd_res.get("countdowns", []):
    print(f"    - ID: {cd['id']}, name: {cd.get('name')}, title: {cd.get('title')}, target: {cd.get('target_date')}")

# Restore display view to main
api("POST", "/api/app/control/display/view", {"view": "main"})
print("\n=== ALL TESTS PASSED ON LIVE PRODUCTION BACKEND! ===")
