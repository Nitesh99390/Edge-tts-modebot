from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path

import edge_tts

logger = logging.getLogger(__name__)

# 100% live-verified Microsoft Edge Neural TTS voices
LANGUAGES = {
    "en": {
        "label": "🇬🇧 English",
        "voices": {
            "en-US-AriaNeural": "👩 Aria — US",
            "en-US-GuyNeural": "👨 Guy — US",
            "en-US-JennyNeural": "👩 Jenny — US",
            "en-US-ChristopherNeural": "👨 Christopher — US",
            "en-US-EricNeural": "👨 Eric — US",
            "en-US-MichelleNeural": "👩 Michelle — US",
            "en-US-RogerNeural": "👨 Roger — US",
            "en-US-SteffanNeural": "👨 Steffan — US",
            "en-US-AndrewMultilingualNeural": "👨 Andrew — US (Warm)",
            "en-US-AvaMultilingualNeural": "👩 Ava — US (Expressive)",
            "en-IN-NeerjaNeural": "👩 Neerja — India",
            "en-IN-NeerjaExpressiveNeural": "👩 Neerja Expressive — India",
            "en-IN-PrabhatNeural": "👨 Prabhat — India",
            "en-GB-SoniaNeural": "👩 Sonia — UK",
            "en-GB-RyanNeural": "👨 Ryan — UK",
            "en-AU-NatashaNeural": "👩 Natasha — Australia",
            "en-AU-WilliamMultilingualNeural": "👨 William — Australia",
        },
    },
    "hi": {
        "label": "🇮🇳 Hindi / Hinglish",
        "voices": {
            "hi-IN-SwaraNeural": "👩 Swara — Hindi (Native)",
            "hi-IN-MadhurNeural": "👨 Madhur — Hindi (Native)",
            "en-US-AndrewMultilingualNeural": "👨 Andrew — Multilingual (Hindi+Eng)",
            "en-US-AvaMultilingualNeural": "👩 Ava — Multilingual (Hindi+Eng)",
            "en-US-BrianMultilingualNeural": "👨 Brian — Multilingual (Hindi+Eng)",
            "en-US-EmmaMultilingualNeural": "👩 Emma — Multilingual (Hindi+Eng)",
            "fr-FR-RemyMultilingualNeural": "👨 Remy — Multilingual (Deep)",
            "fr-FR-VivienneMultilingualNeural": "👩 Vivienne — Multilingual (Soft)",
        },
    },
}

STYLES = {
    "normal": {"label": "🎙️ Normal", "rate": 0, "pitch": 0},
    "slow": {"label": "🐢 Slow", "rate": -15, "pitch": 0},
    "fast": {"label": "⚡ Fast", "rate": 15, "pitch": 0},
    "dramatic": {"label": "🎭 Dramatic", "rate": -3, "pitch": 1},
    "calm": {"label": "🌙 Calm", "rate": -8, "pitch": -1},
    "energetic": {"label": "🔥 Energetic", "rate": 8, "pitch": 2},
}

SPEEDS = {
    -25: "0.75×",
    -15: "0.85×",
    -10: "0.90×",
    -5: "0.95×",
    0: "1.00×",
    5: "1.05×",
    10: "1.10×",
    15: "1.15×",
    25: "1.25×",
    50: "1.50×",
}

TTS_ATTEMPTS = 5
TTS_CHUNK_TIMEOUT_SECONDS = 75.0

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPEAKABLE = re.compile(r"[\w\u0900-\u097F]", flags=re.UNICODE)


def has_speakable_content(text: str) -> bool:
    return bool(text and _SPEAKABLE.search(text))


def sanitize_tts_text(text: str) -> str:
    return _CONTROL_CHARS.sub(" ", text).strip() if text else ""


def _format_rate(rate) -> str:
    if isinstance(rate, bool):
        raise TypeError(f"Invalid rate value: {rate!r}")
    raw = rate.strip().rstrip("%").strip() if isinstance(rate, str) else rate
    try:
        value = max(-90, min(100, int(round(float(raw)))))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Invalid rate value: {rate!r}") from exc
    return f"{value:+d}%"


def _format_pitch(pitch) -> str:
    if isinstance(pitch, bool):
        raise TypeError(f"Invalid pitch value: {pitch!r}")
    if isinstance(pitch, str):
        cleaned = pitch.strip()
        raw = cleaned[:-2].strip() if cleaned.lower().endswith("hz") else cleaned
    else:
        raw = pitch
    try:
        value = max(-50, min(50, int(round(float(raw)))))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Invalid pitch value: {pitch!r}") from exc
    return f"{value:+d}Hz"


async def synthesize_with_retry(
    text: str,
    output_path: str | Path,
    voice: str,
    rate=0,
    pitch=0,
    attempts: int = TTS_ATTEMPTS,
) -> bool:
    """
    Fast, zero-leak async Edge TTS synthesis with jittered backoff.
    Writes atomically via a .part file so partial audio never corrupts FFmpeg concat.
    Returns True if audio was written, False if chunk has no speakable text.
    """
    cleaned = sanitize_tts_text(text)
    if not cleaned:
        raise ValueError("Cannot synthesize empty text.")
    if not has_speakable_content(cleaned):
        return False

    rate_str = _format_rate(rate)
    pitch_str = _format_pitch(pitch)
    out = Path(output_path)
    tmp_out = out.with_suffix(".mp3.part")
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            communicate = edge_tts.Communicate(
                text=cleaned,
                voice=voice,
                rate=rate_str,
                pitch=pitch_str,
                connect_timeout=12,
                receive_timeout=45,
            )

            async def _stream_to_disk() -> int:
                written = 0
                with tmp_out.open("wb") as handle:
                    async for msg in communicate.stream():
                        if msg["type"] == "audio":
                            data = msg["data"]
                            handle.write(data)
                            written += len(data)
                return written

            total_bytes = await asyncio.wait_for(
                _stream_to_disk(),
                timeout=TTS_CHUNK_TIMEOUT_SECONDS,
            )
            if total_bytes > 0:
                tmp_out.replace(out)
                return True

            last_error = RuntimeError("Edge TTS returned 0 audio bytes.")

        except asyncio.CancelledError:
            tmp_out.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
            raise
        except (ValueError, TypeError):
            tmp_out.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "TTS retry %d/%d (%s): %s", attempt, attempts, voice, exc
            )

        tmp_out.unlink(missing_ok=True)
        if attempt < attempts:
            # Exponential backoff + random jitter prevents thundering-herd rate limits
            delay = min(15.0, (1.4 ** attempt) + random.uniform(0.2, 0.9))
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"TTS failed after {attempts} attempts: {last_error}"
    ) from last_error
