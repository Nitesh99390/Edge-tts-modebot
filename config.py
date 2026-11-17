import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Create a .env file using .env.example."
    )

if not OWNER_ID_RAW.isdigit():
    raise RuntimeError(
        "OWNER_ID must be your numeric Telegram user ID."
    )

OWNER_ID = int(OWNER_ID_RAW)
