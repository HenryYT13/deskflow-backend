# DeskFlow Unified Backend — REST API, Discord Bot & MQTT Bridge

Python `aiohttp` + `discord.py` + `paho-mqtt` backend serving the DeskFlow cross-platform ecosystem (`Android`, `Linux`, `macOS`, `Windows`, `Browser Extension`, and `ESP32 Station`).

## Running Backend Tests
```bash
for f in tests/focus_mode/test_*.py tests/test_*.py; do
  PYTHONPATH=.:_server_copy/src python3 "$f"
done
```
