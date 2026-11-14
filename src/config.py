import os
from dotenv import load_dotenv

load_dotenv()

# ================= DISCORD & AI =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))  # Điền ID tài khoản Discord của bạn
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")  # Điền Groq API Key để sinh Quotes/Morning Briefing
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")  # Điền nếu muốn quét mưa tự động
SPOTIFY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI", "")

# ================= MQTT & DATABASE =================
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = 1883
POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090")

# ================= MQTT TOPICS =================
TOPIC_ALARM = "deskflow/alarm"
TOPIC_SOLVE = "deskflow/solve"
TOPIC_CLOSE_TKB = "deskflow/close_tkb"
TOPIC_TIMER = "deskflow/timer"
TOPIC_EXAM = "deskflow/exam"
TOPIC_LAMP = "deskflow/lamp"
TOPIC_CLOCK_STYLE = "deskflow/clock_style"
TOPIC_SAY = "deskflow/say"
TOPIC_QUOTE = "deskflow/quote"
TOPIC_CONFIG = "deskflow/config"

# Subscribed Topics (Nhận từ ESP32)
TOPIC_STATUS_UPDATE = "deskflow/status_update"
TOPIC_MATH_TRIGGER = "deskflow/math_trigger"
TOPIC_BATTERY_ALERT = "deskflow/battery_alert"
