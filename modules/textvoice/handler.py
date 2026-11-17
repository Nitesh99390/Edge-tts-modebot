from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import FileSizeLimit
from telegram.ext import ContextTypes

from .audio import JobCancelled, generate_audio
from .epub import extract_book
from .tts import LANGUAGES, SPEEDS, STYLES


# Kept next to the project folder, so the start-up directory does not matter.
TEMP_ROOT = Path(__file__).resolve().parents[2] / "temp" / "textvoice"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

PROGRESS_INTERVAL_SECONDS = 20

BUSY_TEXT = (
    "⏳ An audiobook is still being generated.\n\n"
    "Please wait for it to finish, or press Cancel on its progress message."
)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _state(context):
    return context.user_data.setdefault("textvoice", {})


def _clear_state(context):
    context.user_data.pop("textvoice", None)


def _is_busy(context):
    state = context.user_data.get("textvoice") or {}
    return state.get("stage") == "generating"


def _remove_job(context):
    """Delete the session's temporary files and forget the session."""
    state = context.user_data.get("textvoice") or {}
    job_dir = state.get("job_dir")

    if job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)

    _clear_state(context)


def cleanup_stale_jobs():
    """Remove leftovers from earlier runs. Call once when the bot starts."""
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def _safe_name(value: str) -> str:
    value = re.sub(r"[^\w\- ]+", "", str(value), flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:80] or "audiobook"


def _label(value, fallback="Option"):
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


def _norm(value) -> str:
    return " ".join(str(value).split()).casefold()


# ---------------------------------------------------------------------------
# Style / speed
# ---------------------------------------------------------------------------

def _get_styles():
    return STYLES


def _get_speed_items():
    """(callback_id, speed_key, label) per speed. The callback id is the speed key itself."""
    return [(str(key), key, _label(value, str(key))) for key, value in SPEEDS.items()]


def _find_speed(callback_id):
    try:
        key = int(callback_id)
    except ValueError:
        return None

    return key if key in SPEEDS else None


def _speed_rate(speed_key):
    """
    SPEEDS keys are edge-tts rate offsets in percent: -15 is 0.85x and +10 is 1.10x.
    They are added to the rate that the chosen style provides.
    """
    return int(speed_key) if speed_key in SPEEDS else 0


def _number(value, default=0):
    if isinstance(value, bool):
        return default

    if isinstance(value, (int, float)):
        return value

    if isinstance(value, str):
        cleaned = value.strip().replace("%", "").replace("Hz", "").replace("hz", "")

        try:
            return float(cleaned)
        except ValueError:
            return default

    return default


def _format_rate(value):
    return f"{int(_number(value, 0)):+d}%"


def _format_pitch(value):
    return f"{int(_number(value, 0)):+d}Hz"


def _style_values(style_id):
    value = _get_styles().get(style_id)

    rate = 0
    pitch = 0

    if isinstance(value, dict):
        rate = value.get("rate", 0)
        pitch = value.get("pitch", 0)

    return _number(rate, 0), _number(pitch, 0)


def _voice_label(language, voice):
    language_info = LANGUAGES.get(language, {})
    voices = language_info.get("voices", {}) if isinstance(language_info, dict) else {}
    return _label(voices.get(voice, voice), voice)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _language_keyboard():
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
                text="❌ Cancel",
                callback_data="tv:cancel",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def _voice_keyboard(language):
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


def _style_keyboard():
    rows = []

    for style_id, value in _get_styles().items():
        rows.append(
            [
                InlineKeyboardButton(
                    text=_label(value, style_id),
                    callback_data=f"tv:style:{style_id}",
                )
            ]
        )

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


def _speed_keyboard():
    rows = []

    for callback_id, _speed_key, label in _get_speed_items():
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"tv:speed:{callback_id}",
                )
            ]
        )

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


def _range_keyboard():
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
                    text="📑 Choose Chapters",
                    callback_data="tv:range:choose",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔢 Custom Range",
                    callback_data="tv:range:custom",
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


def _range_input_keyboard():
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


def _selection_keyboard():
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


def _cancel_keyboard():
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


def _chapter_selection_text(total):
    return (
        "📑 Choose chapters\n\n"
        f"This book contains {total} chapters.\n\n"
        "Send a chapter number or range.\n\n"
        "Examples:\n"
        "• 1\n"
        "• 1-5\n"
        "• 10-20\n\n"
        "The selected chapters will be converted."
    )


# ---------------------------------------------------------------------------
# Enter module
# ---------------------------------------------------------------------------

async def enter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    if _is_busy(context):
        await message.reply_text(BUSY_TEXT)
        return

    _remove_job(context)

    await message.reply_text(
        "🎙️ Text-to-Voice\n\n"
        "Send me an EPUB or TXT book.\n\n"
        "EPUB is recommended because chapter information "
        "can be detected automatically."
    )


# ---------------------------------------------------------------------------
# Document handling
# ---------------------------------------------------------------------------

async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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
            "❌ Unsupported file.\n\n"
            "Please send an EPUB or TXT file."
        )
        return

    if (document.file_size or 0) > FileSizeLimit.FILESIZE_DOWNLOAD:
        await message.reply_text(
            "❌ This file is larger than 20 MB. "
            "Telegram bots cannot download files that big."
        )
        return

    # A new book replaces the previous one, so remove the old files first.
    _remove_job(context)

    job_dir = Path(
        tempfile.mkdtemp(
            prefix="job_",
            dir=str(TEMP_ROOT),
        )
    )

    # A sanitised name means a strange file name cannot point outside job_dir.
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

    try:
        await message.reply_text("📥 Downloading your book...")

        telegram_file = await message.get_bot().get_file(document.file_id)

        await telegram_file.download_to_drive(
            custom_path=str(input_path),
        )

        await message.reply_text("📖 Reading the book...")

        chapters = await asyncio.to_thread(
            extract_book,
            input_path,
        )

        if not chapters:
            raise RuntimeError("No readable chapters were found.")

        state["chapters"] = chapters
        state["stage"] = "range"

        await message.reply_text(
            "✅ Book loaded successfully.\n\n"
            f"📚 Chapters found: {len(chapters)}\n\n"
            "What would you like to convert?",
            reply_markup=_range_keyboard(),
        )

    except Exception as exc:
        print(
            "[TextVoice] Document processing error:",
            repr(exc),
        )

        await message.reply_text(
            "❌ I couldn't read this book.\n\n"
            f"Error: {str(exc)[:500]}"
        )

        _remove_job(context)


# ---------------------------------------------------------------------------
# Text input
# ---------------------------------------------------------------------------

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    state = context.user_data.get("textvoice")

    if not state:
        await message.reply_text(
            "Please choose the Text-to-Voice module first."
        )
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
                "1\n"
                "1-5\n"
                "10-20"
            )
            return

        state["range"] = (start, end)
        state["stage"] = "range_confirm"

        await message.reply_text(
            "📚 Chapter selection\n\n"
            f"Selected: {start}-{end}\n"
            f"Total chapters: {len(state['chapters'])}\n\n"
            "Continue with this selection?",
            reply_markup=_selection_keyboard(),
        )

        return

    await message.reply_text(
        "Please use the buttons to continue."
    )


# ---------------------------------------------------------------------------
# Range parser
# ---------------------------------------------------------------------------

def _parse_range(value: str, total: int):
    value = value.strip().lower()

    if value == "all":
        return 1, total

    if not value:
        raise ValueError(
            "Please enter a chapter number."
        )

    if "-" in value:
        parts = value.split("-", 1)

        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip())

        except ValueError:
            raise ValueError(
                "Invalid chapter range."
            )

    else:
        try:
            start = int(value)
            end = start

        except ValueError:
            raise ValueError(
                "Invalid chapter number."
            )

    if start < 1 or end < 1:
        raise ValueError(
            "Chapter numbers must be positive."
        )

    if start > end:
        raise ValueError(
            "The first chapter must not be greater than the second."
        )

    if end > total:
        raise ValueError(
            f"This book only has {total} chapters."
        )

    return start, end


# ---------------------------------------------------------------------------
# Callback handling
# ---------------------------------------------------------------------------

async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    # Answer once, straight away, so Telegram stops the button spinner.
    await query.answer()

    data = query.data or ""
    state = context.user_data.get("textvoice")

    # ---------------------------------------------------------------
    # Cancel. During generation it stops the worker; otherwise it resets the session.
    # ---------------------------------------------------------------

    if data == "tv:cancel":

        if state and state.get("stage") == "generating":
            state["cancel_event"].set()

            await query.edit_message_text(
                "🛑 Cancelling... temporary files will be removed."
            )
            return

        await query.edit_message_text(
            "❌ Text-to-Voice cancelled."
        )

        _remove_job(context)
        return

    if not state:
        await query.edit_message_text(
            "This Text-to-Voice session has expired.\n\n"
            "Please send the book again."
        )
        return

    # While a conversion runs, ignore every other button. This also stops a double tap from starting a second job.
    if state.get("stage") == "generating":
        await query.message.reply_text(BUSY_TEXT)
        return

    # ---------------------------------------------------------------
    # Range: entire book
    # ---------------------------------------------------------------

    if data == "tv:range:all":

        chapters = state.get("chapters", [])

        if not chapters:
            await query.edit_message_text(
                "❌ No chapters are loaded."
            )
            return

        state["range"] = (1, len(chapters))
        state["stage"] = "language"

        await query.edit_message_text(
            "📖 Entire book selected.\n\n"
            f"📚 Chapters: 1-{len(chapters)}\n\n"
            "🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    # ---------------------------------------------------------------
    # Range: typed chapter numbers
    # ---------------------------------------------------------------

    if data in ("tv:range:choose", "tv:range:custom"):

        total = len(state.get("chapters", []))

        if total <= 0:
            await query.edit_message_text(
                "❌ No chapters are loaded."
            )
            return

        state["stage"] = "range_input"

        await query.edit_message_text(
            _chapter_selection_text(total),
            reply_markup=_range_input_keyboard(),
        )
        return

    # ---------------------------------------------------------------
    # Back to the range options
    # ---------------------------------------------------------------

    if data in ("tv:back:range", "tv:selection:change"):

        state["stage"] = "range"

        await query.edit_message_text(
            "📚 Choose what you want to convert:",
            reply_markup=_range_keyboard(),
        )
        return

    if data == "tv:selection:continue":

        selected_range = state.get("range")

        if not selected_range:
            await query.edit_message_text(
                "❌ No chapters selected."
            )
            return

        state["stage"] = "language"

        start, end = selected_range

        await query.edit_message_text(
            "✅ Chapter selection confirmed.\n\n"
            f"📚 Chapters: {start}-{end}\n\n"
            "🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    # ---------------------------------------------------------------
    # Language
    # ---------------------------------------------------------------

    if data.startswith("tv:lang:"):

        language = data.split(":", 2)[2]

        if language not in LANGUAGES:
            await query.edit_message_text(
                "❌ Unknown language."
            )
            return

        state["language"] = language
        state["stage"] = "voice"

        await query.edit_message_text(
            "🎙️ Choose a voice:",
            reply_markup=_voice_keyboard(language),
        )
        return

    # ---------------------------------------------------------------
    # Voice
    # ---------------------------------------------------------------

    if data.startswith("tv:voice:"):

        voice = data.split(":", 2)[2]

        language = state.get("language")

        if not language:
            await query.edit_message_text(
                "❌ Please choose a language first."
            )
            return

        language_info = LANGUAGES.get(language, {})

        voices = (
            language_info.get("voices", {})
            if isinstance(language_info, dict)
            else {}
        )

        if voice not in voices:
            await query.edit_message_text(
                "❌ Invalid voice."
            )
            return

        state["voice"] = voice
        state["stage"] = "style"

        await query.edit_message_text(
            "🎭 Choose a speaking style:",
            reply_markup=_style_keyboard(),
        )
        return

    # ---------------------------------------------------------------
    # Style
    # ---------------------------------------------------------------

    if data.startswith("tv:style:"):

        style = data.split(":", 2)[2]

        if style not in _get_styles():
            await query.edit_message_text(
                "❌ Invalid style."
            )
            return

        state["style"] = style
        state["stage"] = "speed"

        await query.edit_message_text(
            "⚡ Choose speaking speed:",
            reply_markup=_speed_keyboard(),
        )
        return

    # ---------------------------------------------------------------
    # Speed: the last step, so it starts the conversion
    # ---------------------------------------------------------------

    if data.startswith("tv:speed:"):

        speed_key = _find_speed(data.split(":", 2)[2])

        if speed_key is None:
            await query.edit_message_text(
                "❌ Invalid speed.\n\n"
                "Please go back and choose the speed again."
            )
            return

        state["speed"] = speed_key

        await _start_generation(query, context)
        return

    # ---------------------------------------------------------------
    # Back: language / voice / style
    # ---------------------------------------------------------------

    if data == "tv:back:language":

        state["stage"] = "language"

        await query.edit_message_text(
            "🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    if data == "tv:back:voice":

        language = state.get("language")

        if not language:
            state["stage"] = "language"

            await query.edit_message_text(
                "🌐 Choose a language:",
                reply_markup=_language_keyboard(),
            )
            return

        state["stage"] = "voice"

        await query.edit_message_text(
            "🎙️ Choose a voice:",
            reply_markup=_voice_keyboard(language),
        )
        return

    if data == "tv:back:style":

        state["stage"] = "style"

        await query.edit_message_text(
            "🎭 Choose a speaking style:",
            reply_markup=_style_keyboard(),
        )
        return

    # Unknown callbacks were already answered at the top of this function.


# ---------------------------------------------------------------------------
# Book text
# ---------------------------------------------------------------------------

def _starts_with_heading(body, title):
    heading = _norm(title)

    if not heading:
        return False

    return re.match(re.escape(heading) + r"(?!\w)", _norm(body)) is not None


def _build_book_text(chapters):
    """Join the chapters into one text. Each chapter heading is spoken only once."""
    blocks = []

    for chapter in chapters:
        body = (getattr(chapter, "text", "") or "").strip()

        if not body:
            continue

        title = (getattr(chapter, "title", "") or "").strip()

        # EPUB chapters often start with their own heading. Do not read it twice.
        if title and not _starts_with_heading(body, title):
            body = f"{title}\n\n{body}"

        blocks.append(body)

    return "\n\n".join(blocks).strip()


# ---------------------------------------------------------------------------
# Generation (runs in the background, so the bot keeps responding)
# ---------------------------------------------------------------------------

def _progress_text(info, percent):
    return (
        "🎙️ Generating audiobook...\n\n"
        f"📚 Chapters: {info['start']}-{info['end']}\n"
        f"📝 Characters: {info['chars']:,}\n"
        f"🎙️ Voice: {info['voice']}\n"
        f"🎭 Style: {info['style']}\n"
        f"⚡ Speed: {info['speed']}\n"
        f"📊 Progress: {percent}%\n\n"
        "This can take a long time for a big book. Press Cancel to stop."
    )


async def _safe_edit(bot, chat_id, message_id, text, reply_markup=None):
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        # Usually "message is not modified" or a rate limit. Never let it break the job.
        print("[TextVoice] Status update skipped:", repr(exc))


async def _progress_ticker(bot, chat_id, message_id, info, progress):
    shown = None

    while True:
        await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)

        total = progress["total"]

        if total <= 0:
            continue

        percent = min(99, int(progress["done"] * 100 / total))

        if percent != shown:
            shown = percent

            await _safe_edit(
                bot,
                chat_id,
                message_id,
                _progress_text(info, percent),
                _cancel_keyboard(),
            )


async def _stop_task(task):
    task.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _start_generation(query, context):
    state = context.user_data.get("textvoice") or {}

    chapters = state.get("chapters", [])
    selected_range = state.get("range")
    language = state.get("language")
    voice = state.get("voice")

    if not chapters or not selected_range or not language or not voice:
        await query.edit_message_text(
            "❌ Your selection is incomplete. Please send the book again."
        )
        return

    start, end = selected_range
    text = _build_book_text(chapters[start - 1:end])

    if not text:
        await query.edit_message_text(
            "❌ The selected chapters contain no readable text."
        )
        return

    styles = _get_styles()

    style_id = state.get("style", "normal")

    if style_id not in styles:
        style_id = "normal"

    speed_key = state.get("speed", 0)

    # The style sets the base rate and pitch. The speed is added on top of the rate.
    style_rate, style_pitch = _style_values(style_id)

    final_rate = max(-90, min(90, int(style_rate) + _speed_rate(speed_key)))
    final_pitch = max(-50, min(50, int(style_pitch)))

    job_id = uuid.uuid4().hex
    job_dir = Path(state["job_dir"])
    title = Path(state.get("filename", "book")).stem or "Audiobook"
    cancel_event = threading.Event()

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
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }

    state.update(
        {
            "stage": "generating",
            "job_id": job_id,
            "cancel_event": cancel_event,
        }
    )

    print(
        f"[TextVoice] Job {job_id}: {len(text)} chars, "
        f"voice={voice}, rate={job['rate']}, pitch={job['pitch']}"
    )

    try:
        await query.edit_message_text(
            _progress_text(info, 0),
            reply_markup=_cancel_keyboard(),
        )
    except Exception as exc:
        # The progress message is cosmetic. The conversion should run either way.
        print("[TextVoice] Could not show progress message:", repr(exc))

    context.application.create_task(
        _run_job(context.bot, context.user_data, job, info)
    )


async def _run_job(bot, user_data, job, info):
    chat_id = job["chat_id"]
    message_id = job["message_id"]
    progress = {"done": 0, "total": 0}

    ticker = asyncio.create_task(
        _progress_ticker(bot, chat_id, message_id, info, progress)
    )

    try:
        try:
            parts = await asyncio.to_thread(
                generate_audio,
                text=job["text"],
                output_path=job["output"],
                voice=job["voice"],
                rate=job["rate"],
                pitch=job["pitch"],
                work_dir=job["work_dir"],
                progress=lambda done, total: progress.update(done=done, total=total),
                should_stop=job["cancel"].is_set,
            )
        finally:
            await _stop_task(ticker)

        total_parts = len(parts)

        for number, part in enumerate(parts, start=1):

            if job["cancel"].is_set():
                raise JobCancelled()

            size = part.stat().st_size

            if size > FileSizeLimit.FILESIZE_UPLOAD:
                raise RuntimeError(
                    "An audio part is larger than Telegram's 50 MB upload limit."
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
                caption = f"Part {number} of {total_parts}"

            with part.open("rb") as audio_file:
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=title,
                    performer="ModuBot Text-to-Voice",
                    caption=caption,
                    read_timeout=120,
                    write_timeout=300,
                    connect_timeout=30,
                    pool_timeout=30,
                )

            part.unlink(missing_ok=True)

        done_text = "✅ Audiobook generated and uploaded successfully."

        if total_parts > 1:
            done_text += f"\n\n📦 Sent as {total_parts} files."

        await _safe_edit(bot, chat_id, message_id, done_text)

    except JobCancelled:
        await _safe_edit(
            bot,
            chat_id,
            message_id,
            "🛑 Cancelled. Temporary files were removed.",
        )

    except Exception as exc:
        print("[TextVoice] Generation/upload error:", repr(exc))

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
