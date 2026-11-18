from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
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

TEMP_ROOT = Path(__file__).resolve().parents[2] / "temp" / "textvoice"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

PROGRESS_INTERVAL_SECONDS = 8
DEFAULT_TTS_CONCURRENCY = max(1, min(12, int(os.getenv("TTS_CONCURRENCY", "8") or "8")))
UPLOAD_ATTEMPTS = 4

BUSY_TEXT = (
    "⏳ An audiobook is currently being processed.\n\n"
    "Wait for it to finish, or send /cancel (or tap Cancel on the progress message)."
)


# ---------------------------------------------------------------------------
# State & naming helpers
# ---------------------------------------------------------------------------

def _state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("textvoice", {})


def _is_busy(context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get("textvoice") or {}
    return state.get("stage") in ("generating", "reading")


def _remove_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("textvoice") or {}
    job_dir = state.get("job_dir")
    if job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)
    context.user_data.pop("textvoice", None)


def cleanup_stale_jobs() -> None:
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def _safe_name(value: str) -> str:
    value = re.sub(r"[^\w\- ]+", "", str(value), flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:80] or "audiobook"


def _norm_alnum(value: str) -> str:
    collapsed = " ".join(str(value).split()).casefold()
    return re.sub(r"[^\w\u0900-\u097F]+", " ", collapsed, flags=re.UNICODE).strip()


def _format_duration(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    if sec < 60:
        return f"{sec}s"
    mins, rem = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m {rem:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h {mins:02d}m"


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _two_col_rows(pairs: list[tuple[str, str]]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for text, cb in pairs:
        row.append(InlineKeyboardButton(text=text, callback_data=cb))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _language_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=info["label"], callback_data=f"tv:lang:{code}")]
        for code, info in LANGUAGES.items()
    ]
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="tv:back:range"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="tv:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _voice_keyboard(language: str) -> InlineKeyboardMarkup:
    voices = LANGUAGES.get(language, {}).get("voices", {})
    rows = _two_col_rows([(label, f"tv:voice:{vid}") for vid, label in voices.items()])
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="tv:back:language"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="tv:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _style_keyboard() -> InlineKeyboardMarkup:
    rows = _two_col_rows([(info["label"], f"tv:style:{sid}") for sid, info in STYLES.items()])
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="tv:back:voice"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="tv:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _speed_keyboard() -> InlineKeyboardMarkup:
    rows = _two_col_rows([(label, f"tv:speed:{key}") for key, label in SPEEDS.items()])
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data="tv:back:style"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="tv:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _range_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="📖 Entire Book", callback_data="tv:range:all")],
            [InlineKeyboardButton(text="📑 Choose Chapters / Range", callback_data="tv:range:choose")],
            [InlineKeyboardButton(text="📋 View Chapter List", callback_data="tv:range:list")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="tv:cancel")],
        ]
    )


def _range_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="📖 Entire Book", callback_data="tv:range:all")],
            [
                InlineKeyboardButton(text="⬅️ Back", callback_data="tv:back:range"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="tv:cancel"),
            ],
        ]
    )


def _selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text="▶️ Continue", callback_data="tv:selection:continue")],
            [InlineKeyboardButton(text="🔄 Change Chapters", callback_data="tv:selection:change")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="tv:cancel")],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="🛑 Cancel", callback_data="tv:cancel")]]
    )


def _format_chapter_preview(chapters, limit: int = 25) -> str:
    lines = []
    for ch in chapters[:limit]:
        short = (ch.title or f"Chapter {ch.number}").strip()
        if len(short) > 55:
            short = short[:52] + "..."
        lines.append(f"{ch.number}. {short}")
    if len(chapters) > limit:
        lines.append(f"... and {len(chapters) - limit} more chapters.")
    return "\n".join(lines)


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
            logger.debug("Edit skipped: %s", exc)
    except Exception as exc:
        logger.debug("Edit skipped: %s", exc)


async def _safe_query_edit(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        if query.message:
            with contextlib.suppress(Exception):
                await query.message.reply_text(text=text, reply_markup=reply_markup)
    except Exception as exc:
        logger.warning("Callback edit skipped: %s", exc)


# ---------------------------------------------------------------------------
# Commands: /textvoice, /cancel, /status
# ---------------------------------------------------------------------------

async def enter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    if _is_busy(context):
        await message.reply_text(BUSY_TEXT)
        return
    _remove_job(context)
    await message.reply_text(
        "🎙️ Text-to-Voice (Turbo Mode)\n\n"
        "Send an EPUB or TXT book (up to 20 MB).\n"
        "EPUB automatically detects chapters and titles."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    state = context.user_data.get("textvoice")
    if not state:
        await message.reply_text("ℹ️ Nothing is running right now.")
        return

    if state.get("stage") == "generating":
        if state.get("cancel_event"):
            state["cancel_event"].set()
        job_task = state.get("job_task")
        if job_task and not job_task.done():
            job_task.cancel()
        await message.reply_text("🛑 Stopping active conversion and cleaning temp files...")
        return

    _remove_job(context)
    await message.reply_text("❌ Session cancelled.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    ffmpeg_ok = "✅ Installed" if shutil.which("ffmpeg") else "❌ Missing"
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(TEMP_ROOT)
    free_gb = disk.free / (1024 ** 3)
    total_gb = disk.total / (1024 ** 3)

    state = context.user_data.get("textvoice") or {}
    stage = state.get("stage", "idle")
    extra = ""
    if stage == "generating":
        prog = state.get("progress") or {}
        done, tot = prog.get("done", 0), prog.get("total", 0)
        pct = int(done * 100 / tot) if tot else 0
        extra = f"\n📊 Active Job: {pct}% ({done}/{tot} chunks)"

    await message.reply_text(
        "🖥️ Oracle Server Status\n\n"
        f"• State: {stage}\n"
        f"• FFmpeg: {ffmpeg_ok}\n"
        f"• Parallel Workers: {DEFAULT_TTS_CONCURRENCY}\n"
        f"• Disk Free: {free_gb:.2f} GB / {total_gb:.2f} GB{extra}"
    )


# ---------------------------------------------------------------------------
# Document & text handlers
# ---------------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await message.reply_text("❌ Unsupported file. Please send an .epub or .txt file.")
        return

    if (document.file_size or 0) > FileSizeLimit.FILESIZE_DOWNLOAD:
        await message.reply_text("❌ File exceeds Telegram's 20 MB bot download limit.")
        return

    _remove_job(context)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix="job_", dir=str(TEMP_ROOT)))
    input_path = job_dir / f"{_safe_name(Path(filename).stem)}{extension}"

    state = _state(context)
    state.update(
        {
            "job_dir": str(job_dir),
            "filename": filename,
            "stage": "reading",
            "chapters": [],
            "range": None,
        }
    )

    status_msg = await message.reply_text("📥 Downloading book...")
    try:
        tg_file = await message.get_bot().get_file(
            document.file_id, read_timeout=60, connect_timeout=30
        )
        await tg_file.download_to_drive(
            custom_path=str(input_path),
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
        )

        await _safe_edit(
            context.bot, status_msg.chat_id, status_msg.message_id, "📖 Extracting chapters..."
        )
        chapters = await asyncio.to_thread(extract_book, input_path)
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
            "✅ Book loaded!\n\n"
            f"📕 {filename}\n"
            f"📚 Chapters: {len(chapters)}\n"
            f"📝 Characters: {total_chars:,}\n\n"
            "What would you like to convert?",
            reply_markup=_range_keyboard(),
        )
    except Exception as exc:
        logger.exception("Failed reading book %s", filename)
        await _safe_edit(
            context.bot,
            status_msg.chat_id,
            status_msg.message_id,
            f"❌ Could not read this book.\n\nError: {str(exc)[:500]}",
        )
        _remove_job(context)


def _parse_range(value: str, total: int) -> tuple[int, int]:
    value = value.strip().lower()
    if value in ("all", "*"):
        if total < 1:
            raise ValueError("No chapters available.")
        return 1, total
    if not value:
        raise ValueError("Please enter a chapter number or range.")

    normalized = re.sub(r"\s*(?:–|—|\bto\b)\s*", "-", value)
    if "-" in normalized:
        parts = normalized.split("-", 1)
        try:
            start, end = int(parts[0].strip()), int(parts[1].strip())
        except ValueError as exc:
            raise ValueError("Invalid range. Use format like 1-5.") from exc
    else:
        try:
            start = end = int(normalized)
        except ValueError as exc:
            raise ValueError("Invalid chapter number.") from exc

    if start < 1 or end < 1:
        raise ValueError("Chapter numbers must be 1 or greater.")
    if start > end:
        raise ValueError("Start chapter must not exceed end chapter.")
    if end > total:
        raise ValueError(f"This book only has {total} chapters.")
    return start, end


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    state = context.user_data.get("textvoice")
    if not state:
        await message.reply_text("Send an EPUB or TXT file directly, or use /textvoice.")
        return
    if state.get("stage") == "generating":
        await message.reply_text(BUSY_TEXT)
        return

    if state.get("stage") == "range_input":
        try:
            start, end = _parse_range(message.text or "", len(state.get("chapters", [])))
        except ValueError as exc:
            await message.reply_text(
                f"❌ {exc}\n\nExamples: 1, 1-5, 10-20, all",
                reply_markup=_range_input_keyboard(),
            )
            return

        chapters = state["chapters"]
        chars = sum(len(ch.text) for ch in chapters[start - 1 : end])
        state["range"] = (start, end)
        state["stage"] = "range_confirm"

        await message.reply_text(
            f"📚 Selected chapters: {start}–{end} ({end - start + 1} ch, {chars:,} chars)\n\n"
            "Continue with this selection?",
            reply_markup=_selection_keyboard(),
        )
        return

    await message.reply_text("Use the buttons above, or send /cancel to reset.")


# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    with contextlib.suppress(Exception):
        await query.answer()

    data = query.data or ""
    state = context.user_data.get("textvoice")

    if data == "tv:cancel":
        if state and state.get("stage") == "generating":
            if state.get("cancel_event"):
                state["cancel_event"].set()
            job_task = state.get("job_task")
            if job_task and not job_task.done():
                job_task.cancel()
            await _safe_query_edit(query, "🛑 Cancelling immediately and cleaning files...")
            return
        await _safe_query_edit(query, "❌ Cancelled.")
        _remove_job(context)
        return

    if not state:
        await _safe_query_edit(query, "⚠️ Session expired. Please send the book again.")
        return

    if state.get("stage") == "generating":
        if query.message:
            with contextlib.suppress(Exception):
                await query.message.reply_text(BUSY_TEXT)
        return

    chapters = state.get("chapters", [])

    if data == "tv:range:all":
        if not chapters:
            await _safe_query_edit(query, "❌ No chapters loaded.")
            return
        state["range"] = (1, len(chapters))
        state["stage"] = "language"
        await _safe_query_edit(
            query,
            f"📖 Entire book selected (1–{len(chapters)}).\n\n🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    if data in ("tv:range:choose", "tv:range:custom", "tv:range:list"):
        if not chapters:
            await _safe_query_edit(query, "❌ No chapters loaded.")
            return
        limit = 50 if data == "tv:range:list" else 15
        preview = _format_chapter_preview(chapters, limit=limit)
        state["stage"] = "range_input"
        await _safe_query_edit(
            query,
            f"📑 Chapters ({len(chapters)} total):\n\n{preview}\n\n"
            "Send a chapter number or range (e.g. 1, 1-5, 10-20, all):",
            reply_markup=_range_input_keyboard(),
        )
        return

    if data in ("tv:back:range", "tv:selection:change"):
        state["stage"] = "range"
        await _safe_query_edit(
            query, "📚 Choose what you want to convert:", reply_markup=_range_keyboard()
        )
        return

    if data == "tv:selection:continue":
        selected = state.get("range")
        if not selected:
            await _safe_query_edit(query, "❌ No chapters selected.")
            return
        state["stage"] = "language"
        await _safe_query_edit(
            query,
            f"✅ Chapters {selected[0]}–{selected[1]} confirmed.\n\n🌐 Choose a language:",
            reply_markup=_language_keyboard(),
        )
        return

    if data.startswith("tv:lang:"):
        lang = data.split(":", 2)[2]
        if lang not in LANGUAGES:
            await _safe_query_edit(query, "❌ Unknown language.")
            return
        state["language"] = lang
        state["stage"] = "voice"
        await _safe_query_edit(query, "🎙️ Choose a voice:", reply_markup=_voice_keyboard(lang))
        return

    if data.startswith("tv:voice:"):
        voice = data.split(":", 2)[2]
        lang = state.get("language", "")
        if voice not in LANGUAGES.get(lang, {}).get("voices", {}):
            await _safe_query_edit(query, "❌ Invalid voice.")
            return
        state["voice"] = voice
        state["stage"] = "style"
        await _safe_query_edit(
            query, "🎭 Choose a speaking style:", reply_markup=_style_keyboard()
        )
        return

    if data.startswith("tv:style:"):
        style = data.split(":", 2)[2]
        if style not in STYLES:
            await _safe_query_edit(query, "❌ Invalid style.")
            return
        state["style"] = style
        state["stage"] = "speed"
        await _safe_query_edit(
            query, "⚡ Choose speaking speed:", reply_markup=_speed_keyboard()
        )
        return

    if data.startswith("tv:speed:"):
        try:
            speed_key = int(data.split(":", 2)[2])
        except ValueError:
            speed_key = None
        if speed_key not in SPEEDS:
            await _safe_query_edit(query, "❌ Invalid speed.")
            return
        state["speed"] = speed_key
        await _start_generation(query, context)
        return

    if data == "tv:back:language":
        state["stage"] = "language"
        await _safe_query_edit(query, "🌐 Choose a language:", reply_markup=_language_keyboard())
        return

    if data == "tv:back:voice":
        lang = state.get("language")
        if not lang:
            state["stage"] = "language"
            await _safe_query_edit(
                query, "🌐 Choose a language:", reply_markup=_language_keyboard()
            )
            return
        state["stage"] = "voice"
        await _safe_query_edit(query, "🎙️ Choose a voice:", reply_markup=_voice_keyboard(lang))
        return

    if data == "tv:back:style":
        state["stage"] = "style"
        await _safe_query_edit(
            query, "🎭 Choose a speaking style:", reply_markup=_style_keyboard()
        )


# ---------------------------------------------------------------------------
# Book text assembly & background Turbo generation
# ---------------------------------------------------------------------------

def _starts_with_heading(body: str, title: str) -> bool:
    heading_norm = _norm_alnum(title)
    if not heading_norm:
        return False
    body_norm = _norm_alnum(body[: max(500, len(title) * 4)])
    return bool(body_norm and re.match(re.escape(heading_norm) + r"(?!\w)", body_norm))


def _build_book_text(chapters) -> str:
    blocks = []
    for ch in chapters:
        body = (getattr(ch, "text", "") or "").strip()
        if not body:
            continue
        title = (getattr(ch, "title", "") or "").strip()
        if title and not _starts_with_heading(body, title):
            body = f"{title}.\n\n{body}"
        blocks.append(body)
    return "\n\n".join(blocks).strip()


def _progress_text(info: dict, done: int, total: int, started_at: float) -> str:
    elapsed = max(0.1, time.monotonic() - started_at)
    if total > 0 and done > 0:
        pct = min(99, int(done * 100 / total))
        eta = _format_duration((elapsed / done) * max(0, total - done))
        status_line = f"{done}/{total} chunks ({pct}%) • ETA ~{eta}"
    else:
        status_line = f"Starting {DEFAULT_TTS_CONCURRENCY} parallel workers..."

    return (
        "⚡ Generating audiobook (Turbo Mode)...\n\n"
        f"📚 Chapters: {info['start']}–{info['end']}\n"
        f"📝 Characters: {info['chars']:,}\n"
        f"🎙️ Voice: {info['voice']}\n"
        f"🎭 Style: {info['style']} • {info['speed']}\n"
        f"📊 Progress: {status_line}\n"
        f"⏱️ Elapsed: {_format_duration(elapsed)}"
    )


async def _progress_ticker(
    bot, chat_id: int, message_id: int, info: dict, progress: dict, started_at: float
) -> None:
    last_done = -1
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
        done, total = progress["done"], progress["total"]
        if total <= 0 or done == last_done:
            continue
        last_done = done
        await _safe_edit(
            bot,
            chat_id,
            message_id,
            _progress_text(info, done, total, started_at),
            _cancel_keyboard(),
        )


async def _send_audio_with_retry(
    bot, chat_id: int, part_path: Path, title: str, caption: str | None
) -> None:
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
            await asyncio.sleep(float(exc.retry_after) + 1.0)
            last_exc = exc
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            if attempt < UPLOAD_ATTEMPTS:
                await asyncio.sleep(3.0 * attempt)

    raise RuntimeError(f"Audio upload failed after {UPLOAD_ATTEMPTS} retries: {last_exc}") from last_exc


async def _start_generation(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("textvoice") or {}
    chapters = state.get("chapters", [])
    selected_range = state.get("range")
    language = state.get("language")
    voice = state.get("voice")

    if not chapters or not selected_range or not language or not voice or not query.message:
        await _safe_query_edit(query, "❌ Incomplete selection. Please send the book again.")
        return

    start, end = selected_range
    text = _build_book_text(chapters[start - 1 : end])
    if not text:
        await _safe_query_edit(query, "❌ Selected chapters have no readable text.")
        return

    style_id = state.get("style", "normal")
    if style_id not in STYLES:
        style_id = "normal"
    speed_key = state.get("speed", 0)

    final_rate = max(-90, min(100, STYLES[style_id]["rate"] + int(speed_key)))
    final_pitch = max(-50, min(50, STYLES[style_id]["pitch"]))

    job_id = uuid.uuid4().hex[:12]
    job_dir = Path(state["job_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)
    title = Path(state.get("filename", "book")).stem or "Audiobook"
    cancel_event = threading.Event()
    progress = {"done": 0, "total": 0}
    started_at = time.monotonic()

    info = {
        "start": start,
        "end": end,
        "chars": len(text),
        "voice": LANGUAGES[language]["voices"].get(voice, voice),
        "style": STYLES[style_id]["label"],
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
        "rate": f"{final_rate:+d}%",
        "pitch": f"{final_pitch:+d}Hz",
        "cancel": cancel_event,
        "progress": progress,
        "started_at": started_at,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }

    # Release parsed chapters immediately to save RAM on Oracle VMs
    state["chapters"] = []

    logger.info(
        "Job %s started: %d chars, ch %d-%d, voice=%s, rate=%s, workers=%d",
        job_id,
        len(text),
        start,
        end,
        voice,
        job["rate"],
        DEFAULT_TTS_CONCURRENCY,
    )

    await _safe_query_edit(
        query,
        _progress_text(info, 0, 0, started_at),
        reply_markup=_cancel_keyboard(),
    )

    task = context.application.create_task(_run_job(context.bot, context.user_data, job, info))
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
    chat_id, message_id = job["chat_id"], job["message_id"]
    started_at = job["started_at"]
    ticker = asyncio.create_task(
        _progress_ticker(bot, chat_id, message_id, info, job["progress"], started_at)
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
                progress=lambda done, total: job["progress"].update(done=done, total=total),
                should_stop=job["cancel"].is_set,
                concurrency=DEFAULT_TTS_CONCURRENCY,
            )
            job["text"] = ""
        finally:
            if not ticker.done():
                ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ticker

        synth_elapsed = _format_duration(time.monotonic() - started_at)
        total_parts = len(parts)

        for number, part in enumerate(parts, start=1):
            if job["cancel"].is_set():
                raise JobCancelled()

            size = part.stat().st_size
            if size > FileSizeLimit.FILESIZE_UPLOAD:
                raise RuntimeError("Audio part exceeded Telegram's 50 MB upload limit.")

            await _safe_edit(
                bot,
                chat_id,
                message_id,
                f"📤 Uploading part {number}/{total_parts} ({size / (1024 * 1024):.1f} MB)...",
            )

            audio_title = f"{job['title']} Audiobook"
            caption = None
            if total_parts > 1:
                audio_title += f" (Part {number}/{total_parts})"
                caption = f"🎧 {job['title']} — Part {number} of {total_parts}"

            await _send_audio_with_retry(bot, chat_id, part, audio_title, caption)
            part.unlink(missing_ok=True)

        total_elapsed = _format_duration(time.monotonic() - started_at)
        done_text = (
            f"✅ Audiobook ready in {total_elapsed} (TTS: {synth_elapsed})!\n\n"
            f"📕 {job['title']} (Ch {info['start']}–{info['end']})\n"
            f"🎙️ {info['voice']} • {info['speed']}"
        )
        if total_parts > 1:
            done_text += f"\n📦 Sent as {total_parts} parts."

        await _safe_edit(bot, chat_id, message_id, done_text)
        logger.info("Job %s finished in %s (%d part(s)).", job["id"], total_elapsed, total_parts)

    except (JobCancelled, asyncio.CancelledError):
        logger.info("Job %s cancelled.", job["id"])
        with contextlib.suppress(Exception):
            await _safe_edit(bot, chat_id, message_id, "🛑 Cancelled. Temporary files removed.")

    except Exception as exc:
        logger.exception("Job %s failed", job["id"])
        await _safe_edit(
            bot,
            chat_id,
            message_id,
            f"❌ Generation failed:\n{str(exc)[:600]}\n\nSend the book again to retry.",
        )

    finally:
        shutil.rmtree(job["job_dir"], ignore_errors=True)
        state = user_data.get("textvoice")
        if state and state.get("job_id") == job["id"]:
            user_data.pop("textvoice", None)
