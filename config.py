from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_OWN_TELEGRAM_BOT_TOKEN_HERE":
    raise RuntimeError("BOT_TOKEN missing in .env file.")


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise RuntimeError(f"Invalid numeric Telegram user ID: {part!r}")
        ids.add(int(part))
    return ids


OWNER_IDS = _parse_ids(os.getenv("OWNER_ID", ""))
if not OWNER_IDS:
    raise RuntimeError("OWNER_ID missing in .env file.")

OWNER_ID = next(iter(OWNER_IDS))
OWNER_IDS |= _parse_ids(os.getenv("ALLOWED_USERS", ""))

# Safe high-speed worker count: 8 parallel Edge TTS streams convert a 4-hour book in ~2.5-3.5 min
TTS_CONCURRENCY = max(1, min(12, int(os.getenv("TTS_CONCURRENCY", "8") or "8")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
LOG_FILE = os.getenv("LOG_FILE", str(BASE_DIR / "logs" / "bot.log")).strip()
