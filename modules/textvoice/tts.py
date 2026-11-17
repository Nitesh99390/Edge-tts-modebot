from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import edge_tts

logger = logging.getLogger(__name__)

# Every voice ID below has been verified live against Microsoft Edge TTS.
# Note: Microsoft discontinued non-existent voices like hi-IN-AaravNeural,
# hi-IN-AnanyaNeural, etc., and renamed en-AU-WilliamNeural -> en-AU-WilliamMultilingualNeural.
# Multilingual Neural voices speak both Devanagari Hindi and Hinglish/English seamlessly!
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


# Retries for transient Edge TTS failures (network drops, websocket errors).
TTS_ATTEMPTS = 4
TTS_RETRY_DELAY_SECONDS = 2.0
TTS_CHUNK_TIMEOUT_SECONDS = 90.0

# Match control chars disallowed in XML/SSML (except \t, \n, \r)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Check if chunk actually contains at least one speakable letter or digit
_SPEAKABLE = re.compile(r"[\w\u0900-\u097F]", flags=re.UNICODE)


def get_languages():
    return LANGUAGES


def get_voices(language: str):
    return LANGUAGES[language]["voices"]


def get_styles():
    return STYLES


def get_speeds():
    return SPEEDS


def has_speakable_content(text: str) -> bool:
    """Return True if text has at least one alphanumeric or Devanagari character."""
    if not text:
        return False
    return _SPEAKABLE.search(text) is not None


def sanitize_tts_text(text: str) -> str:
    """Strip invisible control characters that break Edge TTS SSML payloads."""
    if not text:
        return ""
    cleaned = _CONTROL_CHARS.sub(" ", text)
    return cleaned.strip()


def _format_rate(rate) -> str:
    """
    edge-tts expects rate as a string such as:
        '+0%'
        '-3%'
        '+15%'
    """
    if isinstance(rate, bool):
        raise TypeError(f"Invalid rate value: {rate!r}")

    if isinstance(rate, str):
        cleaned = rate.strip()
        if cleaned.endswith("%"):
            num_part = cleaned[:-1].strip()
        else:
            num_part = cleaned

        try:
            value = int(round(float(num_part)))
        except ValueError as exc:
            raise TypeError(f"Invalid rate value: {rate!r}") from exc
    else:
        try:
            value = int(round(float(rate)))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Invalid rate value: {rate!r}") from exc

    value = max(-90, min(100, value))
    return f"{value:+d}%"


def _format_pitch(pitch) -> str:
    """
    edge-tts expects pitch as a string such as:
        '+0Hz'
        '+1Hz'
        '-2Hz'
    """
    if isinstance(pitch, bool):
        raise TypeError(f"Invalid pitch value: {pitch!r}")

    if isinstance(pitch, str):
        cleaned = pitch.strip()
        if cleaned.lower().endswith("hz"):
            num_part = cleaned[:-2].strip()
        else:
            num_part = cleaned

        try:
            value = int(round(float(num_part)))
        except ValueError as exc:
            raise TypeError(f"Invalid pitch value: {pitch!r}") from exc
    else:
        try:
            value = int(round(float(pitch)))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Invalid pitch value: {pitch!r}") from exc

    value = max(-50, min(50, value))
    return f"{value:+d}Hz"


async def synthesize(
    text: str,
    output_path: str | Path,
    voice: str,
    rate=0,
    pitch=0,
    timeout: float = TTS_CHUNK_TIMEOUT_SECONDS,
) -> bool:
    """
    Synthesize text to output_path asynchronously.
    Returns True if audio was written, False if text had no speakable content.
    """
    cleaned = sanitize_tts_text(text)
    if not cleaned:
        raise ValueError("Cannot synthesize empty text.")

    if not has_speakable_content(cleaned):
        logger.debug("Skipping non-speakable chunk: %r", cleaned[:60])
        return False

    rate_string = _format_rate(rate)
    pitch_string = _format_pitch(pitch)
    out = Path(output_path)

    communicate = edge_tts.Communicate(
        text=cleaned,
        voice=voice,
        rate=rate_string,
        pitch=pitch_string,
    )

    await asyncio.wait_for(communicate.save(str(out)), timeout=timeout)
    return True


async def synthesize_with_retry(
    text: str,
    output_path: str | Path,
    voice: str,
    rate=0,
    pitch=0,
    attempts: int = TTS_ATTEMPTS,
) -> bool:
    """
    Async native Edge TTS synthesis with exponential backoff & timeout.
    Does not spawn extra event loops (zero thread overhead on Oracle Cloud).
    Returns True if audio file was created, False if chunk was non-speakable.
    """
    cleaned = sanitize_tts_text(text)
    if not cleaned:
        raise ValueError("Cannot synthesize empty text.")

    if not has_speakable_content(cleaned):
        return False

    output_path = Path(output_path)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            wrote = await synthesize(
                text=cleaned,
                output_path=output_path,
                voice=voice,
                rate=rate,
                pitch=pitch,
            )
            if not wrote:
                return False

            if output_path.exists() and output_path.stat().st_size > 0:
                return True

            last_error = RuntimeError("Edge TTS returned empty audio file.")

        except asyncio.CancelledError:
            output_path.unlink(missing_ok=True)
            raise

        except (ValueError, TypeError):
            output_path.unlink(missing_ok=True)
            raise

        except Exception as exc:
            last_error = exc
            logger.warning(
                "Edge TTS attempt %d/%d failed (%s): %s",
                attempt,
                attempts,
                voice,
                exc,
            )

        output_path.unlink(missing_ok=True)

        if attempt < attempts:
            await asyncio.sleep(TTS_RETRY_DELAY_SECONDS * attempt)

    raise RuntimeError(
        f"Text-to-speech failed after {attempts} attempts: {last_error}"
    ) from last_error


def synthesize_sync(
    text: str,
    output_path: str | Path,
    voice: str,
    rate=0,
    pitch=0,
) -> bool:
    """
    Blocking wrapper around synthesize_with_retry() for backward compatibility.
    Must be called from outside an active event loop (e.g. worker thread).
    """
    output_path = Path(output_path)
    last_error: Exception | None = None

    cleaned = sanitize_tts_text(text)
    if not cleaned:
        raise ValueError("Cannot synthesize empty text.")
    if not has_speakable_content(cleaned):
        return False

    for attempt in range(1, TTS_ATTEMPTS + 1):
        try:
            asyncio.run(
                synthesize(
                    text=cleaned,
                    output_path=output_path,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                )
            )
            if output_path.exists() and output_path.stat().st_size > 0:
                return True
            last_error = RuntimeError("Edge TTS returned no audio.")
        except (ValueError, TypeError):
            output_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_error = exc

        output_path.unlink(missing_ok=True)
        if attempt < TTS_ATTEMPTS:
            time.sleep(TTS_RETRY_DELAY_SECONDS * attempt)

    raise RuntimeError(
        f"Text-to-speech failed after {TTS_ATTEMPTS} attempts: {last_error}"
    ) from last_error
