from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, LOG_FILE, LOG_LEVEL, OWNER_IDS
from modules.textvoice.handler import (
    callback,
    cancel_command,
    cleanup_stale_jobs,
    enter,
    handle_document,
    handle_text,
    status_command,
)

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        try:
            log_path = Path(LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
                )
            )
        except OSError:
            pass

    logging.basicConfig(
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        level=level,
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _guard(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in OWNER_IDS:
            if update.callback_query:
                await update.callback_query.answer("This bot is private.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🔒 Private bot.")
            return
        await fn(update, context)

    return wrapper


@_guard
async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "⚡ ModuBot Text-to-Voice (Turbo Edition)\n\n"
            "Send an EPUB or TXT book directly, or use:\n"
            "• /textvoice — Start a new audiobook\n"
            "• /status — Check server & job status\n"
            "• /cancel — Stop active conversion\n"
            "• /help — Quick guide"
        )


@_guard
async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "📖 Quick Guide:\n\n"
            "1. Send any .epub or .txt file (up to 20 MB).\n"
            "2. Choose Entire Book or Chapter Range (e.g. 1-5).\n"
            "3. Choose Language (English / Hindi-Hinglish), Voice, Style & Speed.\n"
            "4. Turbo Worker Pool converts ~4 hours of audio in ~3 minutes and "
            "uploads MP3 parts (<45 MB each) automatically."
        )


async def error_handler(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled update exception: %s", context.error, exc_info=context.error)


async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Welcome & overview"),
                BotCommand("textvoice", "Convert EPUB/TXT novel to audio"),
                BotCommand("status", "Server & conversion status"),
                BotCommand("cancel", "Cancel active conversion"),
                BotCommand("help", "Quick usage guide"),
            ]
        )
    except Exception as exc:
        logger.warning("Could not set bot menu commands: %s", exc)


async def _post_shutdown(_application: Application) -> None:
    cleanup_stale_jobs()
    logger.info("Bot stopped cleanly.")


def main() -> None:
    _setup_logging()
    cleanup_stale_jobs()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30.0)
        .read_timeout(60.0)
        .write_timeout(120.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(45.0)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("textvoice", _guard(enter)))
    application.add_handler(CommandHandler("cancel", _guard(cancel_command)))
    application.add_handler(CommandHandler("status", _guard(status_command)))
    application.add_handler(CallbackQueryHandler(_guard(callback)))
    application.add_handler(MessageHandler(filters.Document.ALL, _guard(handle_document)))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _guard(handle_text))
    )
    application.add_error_handler(error_handler)

    logger.info("ModuBot Turbo TTS starting polling...")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
