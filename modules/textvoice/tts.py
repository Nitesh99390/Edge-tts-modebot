from __future__ import annotations

import asyncio
import time
from pathlib import Path

import edge_tts


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
            "en-IN-NeerjaNeural": "👩 Neerja — India",
            "en-IN-PrabhatNeural": "👨 Prabhat — India",
            "en-GB-SoniaNeural": "👩 Sonia — UK",
            "en-GB-RyanNeural": "👨 Ryan — UK",
            "en-AU-NatashaNeural": "👩 Natasha — Australia",
            "en-AU-WilliamNeural": "👨 William — Australia",
        },
    },

    "hi": {
        "label": "🇮🇳 Hindi",
        "voices": {
            "hi-IN-AaravNeural": "👨 Aarav",
            "hi-IN-AnanyaNeural": "👩 Ananya",
            "hi-IN-AartiNeural": "👩 Aarti",
            "hi-IN-ArjunNeural": "👨 Arjun",
            "hi-IN-KavyaNeural": "👩 Kavya",
            "hi-IN-KunalNeural": "👨 Kunal",
            "hi-IN-RehaanNeural": "👨 Rehaan",
            "hi-IN-SwaraNeural": "👩 Swara",
            "hi-IN-MadhurNeural": "👨 Madhur",
        },
    },
}


STYLES = {
    "normal": {
        "label": "🎙️ Normal",
        "rate": 0,
        "pitch": 0,
    },
    "slow": {
        "label": "🐢 Slow",
        "rate": -15,
        "pitch": 0,
    },
    "fast": {
        "label": "⚡ Fast",
        "rate": 15,
        "pitch": 0,
    },
    "dramatic": {
        "label": "🎭 Dramatic",
        "rate": -3,
        "pitch": 1,
    },
    "calm": {
        "label": "🌙 Calm",
        "rate": -8,
        "pitch": -1,
    },
    "energetic": {
        "label": "🔥 Energetic",
        "rate": 8,
        "pitch": 2,
    },
}


# Keys are edge-tts rate offsets in percent (-15 means 0.85x).
SPEEDS = {
    -15: "0.85×",
    -10: "0.90×",
    -5: "0.95×",
    0: "1.00×",
    5: "1.05×",
    10: "1.10×",
    15: "1.15×",
}


# Retries for transient Edge TTS failures (network drops, websocket errors).
TTS_ATTEMPTS = 3
TTS_RETRY_DELAY_SECONDS = 2


def get_languages():
    return LANGUAGES


def get_voices(language):
    return LANGUAGES[language]["voices"]


def get_styles():
    return STYLES


def get_speeds():
    return SPEEDS


def _format_rate(rate) -> str:
    """
    edge-tts expects rate as a string such as:
        '+0%'
        '-3%'
        '+15%'
    """
    if isinstance(rate, str):
        rate = rate.strip()

        if rate.endswith("%"):
            return rate

        try:
            value = int(float(rate))
            return f"{value:+d}%"
        except ValueError:
            raise TypeError(f"Invalid rate value: {rate!r}")

    try:
        value = int(rate)
    except (TypeError, ValueError):
        raise TypeError(f"Invalid rate value: {rate!r}")

    return f"{value:+d}%"


def _format_pitch(pitch) -> str:
    """
    edge-tts expects pitch as a string such as:
        '+0Hz'
        '+1Hz'
        '-2Hz'
    """
    if isinstance(pitch, str):
        pitch = pitch.strip()

        if pitch.lower().endswith("hz"):
            return pitch

        try:
            value = int(float(pitch))
            return f"{value:+d}Hz"
        except ValueError:
            raise TypeError(f"Invalid pitch value: {pitch!r}")

    try:
        value = int(pitch)
    except (TypeError, ValueError):
        raise TypeError(f"Invalid pitch value: {pitch!r}")

    return f"{value:+d}Hz"


async def synthesize(
    text: str,
    output_path,
    voice: str,
    rate=0,
    pitch=0,
):
    if not text or not text.strip():
        raise ValueError("Cannot synthesize empty text.")

    rate_string = _format_rate(rate)
    pitch_string = _format_pitch(pitch)

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate_string,
        pitch=pitch_string,
    )

    await communicate.save(str(output_path))


def synthesize_sync(
    text: str,
    output_path,
    voice: str,
    rate=0,
    pitch=0,
):
    """
    Blocking wrapper around synthesize(), with retries.

    Call it from a worker thread (asyncio.to_thread). It uses asyncio.run(),
    so it must not be called from inside a running event loop.
    """
    output_path = Path(output_path)
    last_error: Exception | None = None

    for attempt in range(1, TTS_ATTEMPTS + 1):
        try:
            asyncio.run(
                synthesize(
                    text=text,
                    output_path=output_path,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                )
            )

            if output_path.exists() and output_path.stat().st_size > 0:
                return

            last_error = RuntimeError("Edge TTS returned no audio.")

        except (ValueError, TypeError):
            # Bad input (empty text, invalid rate or pitch). Retrying cannot help.
            raise

        except Exception as exc:
            # Network drops, websocket errors and service-side rejections are often temporary.
            last_error = exc

        output_path.unlink(missing_ok=True)

        if attempt < TTS_ATTEMPTS:
            time.sleep(TTS_RETRY_DELAY_SECONDS * attempt)

    raise RuntimeError(
        f"Text-to-speech failed after {TTS_ATTEMPTS} attempts: {last_error}"
    ) from last_error
