from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()

if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_OWN_TELEGRAM_BOT_TOKEN_HERE":
    raise RuntimeError(
        "BOT_TOKEN is missing or still set to placeholder. "
        "Edit your .env file (copy from .env.example) and set BOT_TOKEN."
    )


def _parse_user_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise RuntimeError(
                f"Invalid Telegram user ID {part!r}. Must be numeric."
            )
        ids.add(int(part))
    return ids


OWNER_IDS = _parse_user_ids(OWNER_ID_RAW)

if not OWNER_IDS:
    raise RuntimeError(
        "OWNER_ID must be your numeric Telegram user ID in .env "
        "(comma-separated IDs are also supported)."
    )

# Primary owner ID kept for backward compatibility
OWNER_ID = next(iter(OWNER_IDS))

# Optional comma-separated additional allowed user IDs
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "").strip()
if ALLOWED_USERS_RAW:
    OWNER_IDS |= _parse_user_ids(ALLOWED_USERS_RAW)

# Optional Oracle Server tuning parameters
TTS_CONCURRENCY = max(1, min(8, int(os.getenv("TTS_CONCURRENCY", "3") or "3")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
LOG_FILE = os.getenv("LOG_FILE", str(BASE_DIR / "logs" / "bot.log")).strip()
