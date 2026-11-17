from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import FileSizeLimit
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from .audio import JobCancelled, generate_audio_async
from .epub import extract_book
from .tts import LANGUAGES, SPEEDS, STYLES

logger = logging.getLogger(__name__)

# Kept next to the project folder, so the start-up directory does not matter.
TEMP_ROOT = Path(__file__).resolve().parents[2] / "temp" / "textvoice"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

PROGRESS_INTERVAL_SECONDS = 15
DEFAULT_TTS_CONCURRENCY = max(1, min(8, int(os.getenv("TTS_CONCURRENCY", "3") or "3")))
UPLOAD_ATTEMPTS = 3

BUSY_TEXT = (
    "⏳ An audiobook is currently being processed.\n\n"
    "Please wait for it to finish, or send /cancel (or press Cancel on the progress message)."
)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("textvoice", {})


def _clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("textvoice", None)


def _is_busy(context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get("textvoice") or {}
    return state.get("stage") in ("generating", "reading")


def _remove_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the session's temporary files and forget the session."""
    state = context.user_data.get("textvoice") or {}
    job_dir = state.get("job_dir")

    if job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)

    _clear_state(context)


def cleanup_stale_jobs() -> None:
    """Remove leftovers from earlier runs. Call once when the bot starts."""
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def _safe_name(value: str) -> str:
    value = re.sub(r"[^\w\- ]+", "", str(value), flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:80] or "audiobook"


def _label(value, fallback: str = "Option") -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        for key in ("label", "name", "title", "text"):
            if key in value:
                return str(value[key])

    if isinstance(value, (tuple, list)):
        if value:
            return str(value[0])

    return str(value) if value is not None else fallback


def _norm_alnum(value: str) -> str:
    """Normalize text for duplicate-heading comparison across punctuation differences."""
    collapsed = " ".join(str(value).split()).casefold()
    return re.sub(r"[^\w\u0900-\u097F]+", " ", collapsed, flags=re.UNICODE).strip()


# ---------------------------------------------------------------------------
# Style / speed
# ---------------------------------------------------------------------------

def _get_styles() -> dict:
    return STYLES


def _get_speed_items():
    """(callback_id, speed_key, label) per speed."""
    return [(str(key), key, _label(value, str(key))) for key, value in SPEEDS.items()]


def _find_speed(callback_id: str) -> int | None:
    try:
        key = int(callback_id)
    except ValueError:
        return None
    return key if key in SPEEDS else None


def _speed_rate(speed_key: int) -> int:
    return int(speed_key) if speed_key in SPEEDS else 0


def _number(value, default: float = 0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace("%", "").replace("Hz", "").replace("hz", "")
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def _format_rate(value) -> str:
    return f"{int(round(_number(value, 0))):+d}%"


def _format_pitch(value) -> str:
    return f"{int(round(_number(value, 0))):+d}Hz"


def _style_values(style_id: str) -> tuple[float, float]:
    value = _get_styles().get(style_id)
    rate = 0
    pitch = 0
    if isinstance(value, dict):
        rate = value.get("rate", 0)
        pitch = value.get("pitch", 0)
    return _number(rate, 0), _number(pitch, 0)


def _voice_label(language: str, voice: str) -> str:
    language_info = LANGUAGES.get(language, {})
    voices = language_info.get("voices", {}) if isinstance(language_info, dict) else {}
    return _label(voices.get(voice, voice), voice)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _language_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for code, info in LANGUAGES.items():
        label = info.get("label", code) if isinstance(info, dict) else info
        rows.append(
            [
                InlineKeyboardButton(
                    text=_label(label, code),
                    callback_data=f"tv:lang:{code}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="tv:back:range",
            ),
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="tv:cancel",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _voice_keyboard(language: str) -> InlineKeyboardMarkup:
    language_info = LANGUAGES.get(language)
    if not isinstance(language_info, dict):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="❌ Invalid language configuration",
                        callback_data="tv:cancel",
                    )
                ]
            ]
        )

    voices = language_info.get("voices", {})
    rows = []
    row = []

    for voice_id, value in voices.items():
        row.append(
            InlineKeyboardButton(
                text=_label(value, voice_id),
                callback_data=f"tv:voice:{voice_id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="tv:back:language",
            ),
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="tv:cancel",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _style_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for style_id, value in _get_styles().items():
        row.append(
            InlineKeyboardButton(
                text=_label(value, style_id),
                callback_data=f"tv:style:{style_id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="tv:back:voice",
            ),
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="tv:cancel",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _speed_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for callback_id, _speed_key, label in _get_speed_items():
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"tv:speed:{callback_id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="tv:back:style",
            ),
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="tv:cancel",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _range_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="📖 Entire Book",
                    callback_data="tv:range:all",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📑 Choose Chapters / Range",
                    callback_data="tv:range:choose",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 View Chapter List",
                    callback_data="tv:range:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="tv:cancel",
                )
            ],
        ]
    )


def _range_input_keyboard() -> InlineKeyboardMarkup:
    """Shown while the user types a chapter range, so they can still go back or cancel."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="📖 Entire Book",
                    callback_data="tv:range:all",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="tv:back:range",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="tv:cancel",
                ),
            ],
        ]
    )


def _selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="▶️ Continue",
                    callback_data="tv:selection:continue",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Change Chapters",
                    callback_data="tv:selection:change",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="tv:cancel",
                )
            ],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🛑 Cancel",
                    callback_data="tv:cancel",
                )
            ]
        ]
    )


def _format_chapter_preview(chapters, limit: int = 25) -> str:
    lines = []
    for ch in chapters[:limit]:
        short_title = (ch.title or f"Chapter {ch.number}").strip()
        if len(short_title) > 55:
            short_title = short_title[:52] + "..."
        lines.append(f"{ch.number}. {short_title}")
    if len(chapters) > limit:
        lines.append(f"... and {len(chapters) - limit} more chapters.")
    return "\n".join(lines)


def _chapter_selection_text(chapters) -> str:
    total = len(chapters)
    preview = _format_chapter_preview(chapters, limit=15)
    return (
        "📑 Choose chapters\n\n"
        f"This book contains {total} chapters:\n"
        f"{preview}\n\n"
        "Send a chapter number or range:\n"
        "• 1\n"
        "• 1-5\n"
        "• 10-20\n"
        "• all"
    )


async def _safe_query_edit(query, text: str, reply_markup=None) -> None:
    """Safely edit a callback message without crashing on 'Message is not modified' or deleted messages."""
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return
        if query.message:
            with contextlib.suppress(Exception):
                await query.message.reply_text(text=text, reply_markup=reply_markup)
    except Exception as exc:
        logger.warning("Could not edit callback message: %s", exc)


# ---------------------------------------------------------------------------
# Enter / Cancel / Status commands
# ---------------------------------------------------------------------------

async def enter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if not message:
        return

    if _is_busy(context):
        await message.reply_text(BUSY_TEXT)
        return

    _remove_job(context)

    await message.reply_text(
        "🎙️ Text-to-Voice\n\n"
        "Send me an EPUB or TXT book (up to 20 MB).\n\n"
        "EPUB is recommended because chapter titles and boundaries "
        "are detected automatically."
    )


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Allow cancelling via /cancel command even if progress message scrolled up."""
    message = update.effective_message
    if not message:
        return

    state = context.user_data.get("textvoice")
    if not state:
        await message.reply_text("ℹ️ Nothing is running right now.")
        return

    if state.get("stage") == "generating":
        cancel_ev = state.get("cancel_event")
        if cancel_ev:
            cancel_ev.set()
        job_task = state.get("job_task")
        if job_task and not job_task.done():
            job_task.cancel()
        await message.reply_text(
            "🛑 Stopping active audiobook generation and cleaning temporary files..."
        )
        return

    _remove_job(context)
    await message.reply_text("❌ Current Text-to-Voice session cancelled.")


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show server health, disk free space, FFmpeg status, and current job status."""
    message = update.effective_message
    if not message:
        return

    ffmpeg_ok = "✅ Installed" if shutil.which("ffmpeg") else "❌ Missing"
    disk = shutil.disk_usage(TEMP_ROOT)
    free_gb = disk.free / (1024 ** 3)
    total_gb = disk.total / (1024 ** 3)

    state = context.user_data.get("textvoice") or {}
    stage = state.get("stage", "idle")
    progress_str = ""
    if stage == "generating":
        prog = state.get("progress") or {}
        done = prog.get("done", 0)
        tot = prog.get("total", 0)
        pct = int(done * 100 / tot) if tot else 0
        progress_str = f"\n📊 Active Job Progress: {pct}% ({done}/{tot} chunks)"

    await message.reply_text(
        "🖥️ Oracle Server Bot Status\n\n"
        f"• Bot State: {stage}\n"
        f"• FFmpeg: {ffmpeg_ok}\n"
        f"• TTS Concurrency: {DEFAULT_TTS_CONCURRENCY} streams\n"
        f"• Disk Free: {free_gb:.2f} GB / {total_gb:.2f} GB"
        f"{progress_str}"
    )


# ---------------------------------------------------------------------------
# Document handling
# ---------------------------------------------------------------------------

async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if not message or not message.document:
        return

    if _is_busy(context):
        await message.reply_text(BUSY_TEXT)
        return

    document = message.document
    filename = document.file_name or "book"
    lower_name = filename.lower()

    if lower_name.endswith(".epub"):
        extension = ".epub"
    elif lower_name.endswith(".txt"):
        extension = ".txt"
    else:
        await message.reply_text(
            "❌ Unsupported file format.\n\n"
            "Please send an .epub or .txt file."
        )
        return

    if (document.file_size or 0) > FileSizeLimit.FILESIZE_DOWNLOAD:
        await message.reply_text(
            "❌ This file is larger than 20 MB.\n"
            "Standard Telegram Bot API allows downloading files up to 20 MB."
        )
        return

    # Remove any previous idle session files first.
    _remove_job(context)

    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    job_dir = Path(
        tempfile.mkdtemp(
            prefix="job_",
            dir=str(TEMP_ROOT),
        )
    )

    input_path = job_dir / f"{_safe_name(Path(filename).stem)}{extension}"
    state = _state(context)

    state.update(
        {
            "job_dir": str(job_dir),
            "input_path": str(input_path),
            "filename": filename,
            "stage": "reading",
            "chapters": [],
            "range": None,
        }
    )

    status_msg = await message.reply_text("📥 Downloading your book...")

    try:
        telegram_file = await message.get_bot().get_file(
            document.file_id,
            read_timeout=60,
            connect_timeout=30,
        )

        await telegram_file.download_to_drive(
            custom_path=str(input_path),
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
        )

        await _safe_edit(
            context.bot,
            status_msg.chat_id,
            status_msg.message_id,
            "📖 Reading and extracting chapters...",
        )

        chapters = await asyncio.to_thread(
            extract_book,
            input_path,
        )

        # Delete the raw book archive once chapters are extracted in memory to save server disk
        input_path.unlink(missing_ok=True)

        if not chapters:
            raise RuntimeError("No readable chapters were found.")

        total_chars = sum(len(ch.text) for ch in chapters)
        state["chapters"] = chapters
        state["stage"] = "range"

        await _safe_edit(
            context.bot,
            status_msg.chat_id,
            status_msg.message_id,
            "✅ Book loaded successfully!\n\n"
            f"📕 File: {filename}\n"
            f"📚 Chapters found: {len(chapters)}\n"
            f"📝 Total characters: {total_chars:,}\n\n"
            "What would you like to convert?",
            reply_markup=_range_keyboard(),
        )

    except Exception as exc:
        logger.exception("Document processing error for %s", filename)

        await _safe_edit(
            context.bot,
            status_msg.chat_id,
            status_msg.message_id,
            "❌ I couldn't read this book.\n\n"
            f"Error: {str(exc)[:500]}",
        )
        _remove_job(context)


# ---------------------------------------------------------------------------
# Text input
# ---------------------------------------------------------------------------

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if not message:
        return

    state = context.user_data.get("textvoice")
    if not state:
        await message.reply_text(
            "Send an EPUB or TXT file directly, or use /textvoice to begin."
        )
        return

    if state.get("stage") == "generating":
        await message.reply_text(BUSY_TEXT)
        return

    if state.get("stage") == "range_input":
        text = (message.text or "").strip()

        try:
            start, end = _parse_range(
                text,
                len(state.get("chapters", [])),
            )
        except ValueError as exc:
            await message.reply_text(
                f"❌ {exc}\n\n"
                "Examples:\n"
                "• 1\n"
                "• 1-5\n"
                "• 10-20\n"
                "• all",
                reply_markup=_range_input_keyboard(),
            )
            return

        chapters = state["chapters"]
        selected_chars = sum(len(ch.text) for ch in chapters[start - 1 : end])
        state["range"] = (start, end)
        state["stage"] = "range_confirm"

        await message.reply_text(
            "📚 Chapter selection\n\n"
            f"Selected: {start}–{end} ({end - start + 1} chapter(s))\n"
            f"Characters: {selected_chars:,}\n"
            f"Total chapters in book: {len(chapters)}\n\n"
            "Continue with this selection?",
            reply_markup=_selection_keyboard(),
        )
        return

    await message.reply_text("Please use the inline buttons above (or /cancel to reset).")


# ---------------------------------------------------------------------------
# Range parser
# ---------------------------------------------------------------------------

def _parse_range(value: str, total: int) -> tuple[int, int]:
    value = value.strip().lower()
    if value in ("all", "*"):
        if total < 1:
            raise ValueError("No chapters available.")
        return 1, total

    if not value:
        raise ValueError("Please enter a chapter number or range.")

    # Normalize en-dash, em-dash, and "to" into "-"
    normalized = re.sub(r"\s*(?:–|—|\bto\b)\s*", "-", value)

    if "-" in normalized:
        parts = normalized.split("-", 1)
        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip())
        except ValueError as exc:
            raise ValueError("Invalid chapter range. Use format like 1-5.") from exc
    else:
        try:
            start = int(normalized)
            end = start
        except ValueError as exc:
            raise ValueError("Invalid chapter number.") from exc

    if start < 1 or end < 1:
        raise ValueError("Chapter numbers must be 1 or greater.")

    if start > end:
        raise ValueError("Start chapter must not be greater than end chapter.")

    if end > total:
        raise ValueError(f"This book only has {total} chapters.")

    return start, end


# ---------------------------------------------------------------------------
# Callback handling
# ---------------------------------------------------------------------------

async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return

    with contextlib.suppress(Exception):
        await query.answer()

    data = query.data or ""
    state = context.user_data.get("textvoice")

    # Cancel button
    if data == "tv:cancel":
        if state and state.get("stage") == "generating":
            cancel_ev = state.get("cancel_event")
            if cancel_ev:
                cancel_ev.set()
            job_task = state.get("job_task")
            if job_task and not job_task.done():
                job_task.cancel()

            await _safe_query_edit(
                query,
                "🛑 Cancelling immediately... temporary files are being removed.",
            )
            return

        await _safe_query_edit(query, "❌ Text-to-Voice cancelled.")
        _remove_job(context)
        return

    if not state:
        await _safe_query_edit(
            query,
            "⚠️ This Text-to-Voice session has expired.\n\n"
            "Please send your EPUB or TXT book again.",
        )
        return

    if state.get("stage") == "generating":
        if query.message:
            with contextlib.suppress(Exception):
                await query.message.reply_text(BUSY_TEXT)
        return

    # Range: entire book
    if data == "tv:range:all":
        chapters = state.get("chapters", [])
        if not chapters:
            await _safe_query_edit(query, "❌ No chapters are loaded.")
            return

        state["range"] = (1, len(chapters))
        state["stage"] = "language"

        await _safe_query_edit(
            query,
            "📖 Entire book selected.\n\n"
            f"📚 Chapters: 1–{len(chapters)}\n\n"
            "🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    # Range: list chapters
    if data == "tv:range:list":
        chapters = state.get("chapters", [])
        if not chapters:
            await _safe_query_edit(query, "❌ No chapters are loaded.")
            return

        preview = _format_chapter_preview(chapters, limit=50)
        state["stage"] = "range_input"
        await _safe_query_edit(
            query,
            f"📋 Chapters ({len(chapters)} total):\n\n{preview}\n\n"
            "Reply with a chapter number or range (e.g. 1-5), or press Entire Book:",
            reply_markup=_range_input_keyboard(),
        )
        return

    # Range: typed chapter numbers
    if data in ("tv:range:choose", "tv:range:custom"):
        chapters = state.get("chapters", [])
        if not chapters:
            await _safe_query_edit(query, "❌ No chapters are loaded.")
            return

        state["stage"] = "range_input"
        await _safe_query_edit(
            query,
            _chapter_selection_text(chapters),
            reply_markup=_range_input_keyboard(),
        )
        return

    # Back to range options
    if data in ("tv:back:range", "tv:selection:change"):
        state["stage"] = "range"
        await _safe_query_edit(
            query,
            "📚 Choose what you want to convert:",
            reply_markup=_range_keyboard(),
        )
        return

    if data == "tv:selection:continue":
        selected_range = state.get("range")
        if not selected_range:
            await _safe_query_edit(query, "❌ No chapters selected.")
            return

        state["stage"] = "language"
        start, end = selected_range

        await _safe_query_edit(
            query,
            "✅ Chapter selection confirmed.\n\n"
            f"📚 Chapters: {start}–{end}\n\n"
            "🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    # Language
    if data.startswith("tv:lang:"):
        language = data.split(":", 2)[2]
        if language not in LANGUAGES:
            await _safe_query_edit(query, "❌ Unknown language.")
            return

        state["language"] = language
        state["stage"] = "voice"

        await _safe_query_edit(
            query,
            "🎙️ Choose a voice:",
            reply_markup=_voice_keyboard(language),
        )
        return

    # Voice
    if data.startswith("tv:voice:"):
        voice = data.split(":", 2)[2]
        language = state.get("language")
        if not language:
            await _safe_query_edit(query, "❌ Please choose a language first.")
            return

        language_info = LANGUAGES.get(language, {})
        voices = language_info.get("voices", {}) if isinstance(language_info, dict) else {}

        if voice not in voices:
            await _safe_query_edit(query, "❌ Invalid voice.")
            return

        state["voice"] = voice
        state["stage"] = "style"

        await _safe_query_edit(
            query,
            "🎭 Choose a speaking style:",
            reply_markup=_style_keyboard(),
        )
        return

    # Style
    if data.startswith("tv:style:"):
        style = data.split(":", 2)[2]
        if style not in _get_styles():
            await _safe_query_edit(query, "❌ Invalid style.")
            return

        state["style"] = style
        state["stage"] = "speed"

        await _safe_query_edit(
            query,
            "⚡ Choose speaking speed:",
            reply_markup=_speed_keyboard(),
        )
        return

    # Speed -> starts generation
    if data.startswith("tv:speed:"):
        speed_key = _find_speed(data.split(":", 2)[2])
        if speed_key is None:
            await _safe_query_edit(
                query,
                "❌ Invalid speed.\n\nPlease go back and choose the speed again.",
            )
            return

        state["speed"] = speed_key
        await _start_generation(query, context)
        return

    # Back navigation
    if data == "tv:back:language":
        state["stage"] = "language"
        await _safe_query_edit(
            query,
            "🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    if data == "tv:back:voice":
        language = state.get("language")
        if not language:
            state["stage"] = "language"
            await _safe_query_edit(
                query,
                "🌐 Choose a language:",
                reply_markup=_language_keyboard(),
            )
            return

        state["stage"] = "voice"
        await _safe_query_edit(
            query,
            "🎙️ Choose a voice:",
            reply_markup=_voice_keyboard(language),
        )
        return

    if data == "tv:back:style":
        state["stage"] = "style"
        await _safe_query_edit(
            query,
            "🎭 Choose a speaking style:",
            reply_markup=_style_keyboard(),
        )
        return


# ---------------------------------------------------------------------------
# Book text assembly
# ---------------------------------------------------------------------------

def _starts_with_heading(body: str, title: str) -> bool:
    heading_norm = _norm_alnum(title)
    if not heading_norm:
        return False

    # Only compare against the opening lines of the body for O(1) speed on huge novels
    head_slice = body[: max(500, len(title) * 4)]
    body_norm = _norm_alnum(head_slice)
    if not body_norm:
        return False

    return re.match(re.escape(heading_norm) + r"(?!\w)", body_norm) is not None


def _build_book_text(chapters) -> str:
    """Join the chapters into one text. Each chapter heading is spoken only once."""
    blocks = []

    for chapter in chapters:
        body = (getattr(chapter, "text", "") or "").strip()
        if not body:
            continue

        title = (getattr(chapter, "title", "") or "").strip()
        if title and not _starts_with_heading(body, title):
            body = f"{title}.\n\n{body}"

        blocks.append(body)

    return "\n\n".join(blocks).strip()


# ---------------------------------------------------------------------------
# Generation (runs asynchronously in background task)
# ---------------------------------------------------------------------------

def _progress_text(info: dict, done: int, total: int) -> str:
    percent = min(99, int(done * 100 / total)) if total > 0 else 0
    chunk_line = f"{done}/{total} chunks ({percent}%)" if total > 0 else "Preparing chunks..."
    return (
        "🎙️ Generating audiobook...\n\n"
        f"📚 Chapters: {info['start']}–{info['end']}\n"
        f"📝 Characters: {info['chars']:,}\n"
        f"🎙️ Voice: {info['voice']}\n"
        f"🎭 Style: {info['style']}\n"
        f"⚡ Speed: {info['speed']}\n"
        f"📊 Progress: {chunk_line}\n\n"
        "Press Cancel (or send /cancel) anytime to stop."
    )


async def _safe_edit(bot, chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Status message edit skipped: %s", exc)
    except Exception as exc:
        logger.debug("Status message edit skipped: %s", exc)


async def _progress_ticker(bot, chat_id: int, message_id: int, info: dict, progress: dict) -> None:
    last_done = -1

    while True:
        await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
        total = progress["total"]
        done = progress["done"]

        if total <= 0 or done == last_done:
            continue

        last_done = done
        await _safe_edit(
            bot,
            chat_id,
            message_id,
            _progress_text(info, done, total),
            _cancel_keyboard(),
        )


async def _stop_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _send_audio_with_retry(
    bot,
    chat_id: int,
    part_path: Path,
    title: str,
    caption: str | None,
) -> None:
    """Upload MP3 to Telegram with automatic FloodWait (RetryAfter) & transient network retries."""
    last_exc: Exception | None = None

    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            with part_path.open("rb") as audio_file:
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    filename=part_path.name,
                    title=title,
                    performer="ModuBot Text-to-Voice",
                    caption=caption,
                    read_timeout=180,
                    write_timeout=360,
                    connect_timeout=45,
                    pool_timeout=45,
                )
            return
        except RetryAfter as exc:
            wait_s = float(exc.retry_after) + 1.0
            logger.warning("Telegram flood control during upload; sleeping %.1fs", wait_s)
            await asyncio.sleep(wait_s)
            last_exc = exc
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            logger.warning(
                "Upload attempt %d/%d timed out/network error: %s",
                attempt,
                UPLOAD_ATTEMPTS,
                exc,
            )
            if attempt < UPLOAD_ATTEMPTS:
                await asyncio.sleep(3.0 * attempt)

    raise RuntimeError(f"Failed to upload audio after {UPLOAD_ATTEMPTS} attempts: {last_exc}") from last_exc


async def _start_generation(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("textvoice") or {}

    chapters = state.get("chapters", [])
    selected_range = state.get("range")
    language = state.get("language")
    voice = state.get("voice")

    if not chapters or not selected_range or not language or not voice:
        await _safe_query_edit(
            query,
            "❌ Your selection is incomplete. Please send the book again.",
        )
        return

    if not query.message:
        return

    start, end = selected_range
    text = _build_book_text(chapters[start - 1 : end])

    if not text:
        await _safe_query_edit(
            query,
            "❌ The selected chapters contain no readable text.",
        )
        return

    styles = _get_styles()
    style_id = state.get("style", "normal")
    if style_id not in styles:
        style_id = "normal"

    speed_key = state.get("speed", 0)
    style_rate, style_pitch = _style_values(style_id)

    final_rate = max(-90, min(90, int(round(style_rate)) + _speed_rate(speed_key)))
    final_pitch = max(-50, min(50, int(round(style_pitch))))

    job_id = uuid.uuid4().hex[:12]
    job_dir = Path(state["job_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)
    title = Path(state.get("filename", "book")).stem or "Audiobook"
    cancel_event = threading.Event()
    progress = {"done": 0, "total": 0}

    info = {
        "start": start,
        "end": end,
        "chars": len(text),
        "voice": _voice_label(language, voice),
        "style": _label(styles[style_id], style_id),
        "speed": SPEEDS.get(speed_key, "1.00×"),
    }

    job = {
        "id": job_id,
        "job_dir": job_dir,
        "text": text,
        "output": job_dir / f"{_safe_name(title)}_audiobook.mp3",
        "work_dir": job_dir / "audio_parts",
        "title": title,
        "voice": voice,
        "rate": _format_rate(final_rate),
        "pitch": _format_pitch(final_pitch),
        "cancel": cancel_event,
        "progress": progress,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }

    # Free parsed chapter objects from RAM while generating audio (crucial on 1 GB Oracle VMs)
    state["chapters"] = []

    logger.info(
        "Starting job %s: %d chars, chapters %d-%d, voice=%s, rate=%s, pitch=%s",
        job_id,
        len(text),
        start,
        end,
        voice,
        job["rate"],
        job["pitch"],
    )

    await _safe_query_edit(
        query,
        _progress_text(info, 0, 0),
        reply_markup=_cancel_keyboard(),
    )

    task = context.application.create_task(
        _run_job(context.bot, context.user_data, job, info)
    )

    state.update(
        {
            "stage": "generating",
            "job_id": job_id,
            "cancel_event": cancel_event,
            "job_task": task,
            "progress": progress,
        }
    )


async def _run_job(bot, user_data: dict, job: dict, info: dict) -> None:
    chat_id = job["chat_id"]
    message_id = job["message_id"]
    progress = job["progress"]

    ticker = asyncio.create_task(
        _progress_ticker(bot, chat_id, message_id, info, progress)
    )

    try:
        try:
            parts = await generate_audio_async(
                text=job["text"],
                output_path=job["output"],
                voice=job["voice"],
                rate=job["rate"],
                pitch=job["pitch"],
                work_dir=job["work_dir"],
                progress=lambda done, total: progress.update(done=done, total=total),
                should_stop=job["cancel"].is_set,
                concurrency=DEFAULT_TTS_CONCURRENCY,
            )
            # Release full book string from memory before uploading MP3 parts
            job["text"] = ""
        finally:
            await _stop_task(ticker)

        total_parts = len(parts)

        for number, part in enumerate(parts, start=1):
            if job["cancel"].is_set():
                raise JobCancelled()

            size = part.stat().st_size
            if size > FileSizeLimit.FILESIZE_UPLOAD:
                raise RuntimeError(
                    "An audio part exceeded Telegram's 50 MB upload limit."
                )

            await _safe_edit(
                bot,
                chat_id,
                message_id,
                f"📤 Uploading part {number}/{total_parts} "
                f"({size / (1024 * 1024):.1f} MB)...",
            )

            title = f"{job['title']} Audiobook"
            caption = None

            if total_parts > 1:
                title += f" (Part {number}/{total_parts})"
                caption = f"🎧 {job['title']} — Part {number} of {total_parts}"

            await _send_audio_with_retry(
                bot=bot,
                chat_id=chat_id,
                part_path=part,
                title=title,
                caption=caption,
            )

            part.unlink(missing_ok=True)

        done_text = (
            "✅ Audiobook generated and uploaded successfully!\n\n"
            f"📕 {job['title']} (Chapters {info['start']}–{info['end']})\n"
            f"🎙️ {info['voice']} • {info['speed']}"
        )
        if total_parts > 1:
            done_text += f"\n📦 Sent as {total_parts} parts."

        await _safe_edit(bot, chat_id, message_id, done_text)
        logger.info("Job %s completed (%d part(s) uploaded).", job["id"], total_parts)

    except (JobCancelled, asyncio.CancelledError):
        logger.info("Job %s was cancelled.", job["id"])
        with contextlib.suppress(Exception):
            await _safe_edit(
                bot,
                chat_id,
                message_id,
                "🛑 Cancelled. Temporary files were removed.",
            )

    except Exception as exc:
        logger.exception("Job %s failed", job["id"])
        await _safe_edit(
            bot,
            chat_id,
            message_id,
            "❌ Audiobook generation failed.\n\n"
            f"Error: {str(exc)[:600]}\n\n"
            "You can send the book again to retry.",
        )

    finally:
        shutil.rmtree(job["job_dir"], ignore_errors=True)
        state = user_data.get("textvoice")
        if state and state.get("job_id") == job["id"]:
            user_data.pop("textvoice", None)
