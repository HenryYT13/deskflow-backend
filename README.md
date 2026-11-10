# DeskFlow Unified Backend — REST API, Discord Bot & MQTT Bridge

Python `aiohttp` + `discord.py` + `paho-mqtt` backend serving the DeskFlow cross-platform ecosystem (`Android`, `Linux`, `macOS`, `Windows`, `Browser Extension`, and `ESP32 Station`).

## Running Backend Tests
```bash
for f in tests/focus_mode/test_*.py tests/test_*.py; do
  PYTHONPATH=.:_server_copy/src python3 "$f"
done
```

## Rename a device

`PATCH /api/devices/<device_id>` — requires `Authorization: Bearer <device token>`.

Request: `{"name": "My Laptop"}` (string, whitespace collapsed, 1–64 chars).

Responses: `200 {"success": true, "device_id": "...", "device_name": "..."}`;
`400 name_required | name_too_long`; `401` unauthenticated; `404 device_not_found`.
