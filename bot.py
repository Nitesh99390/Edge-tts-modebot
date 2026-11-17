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


def _setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if LOG_FILE:
        try:
            log_path = Path(LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,  # 5 MB per file
                backupCount=3,
                encoding="utf-8",
            )
            handlers.append(file_handler)
        except OSError as exc:
            print(f"[Logging] Could not open log file {LOG_FILE}: {exc}", file=sys.stderr)

    logging.basicConfig(
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        level=level,
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in OWNER_IDS


async def private_notice(update: Update) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "🔒 This bot is private and can only be used by its authorized owner."
        )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        await private_notice(update)
        return

    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "🎙️ Welcome to ModuBot Text-to-Voice (Oracle Server Edition)!\n\n"
        "Send an EPUB or TXT novel directly, or use:\n"
        "• /textvoice — Start a new audiobook session\n"
        "• /status — Check Oracle server & job status\n"
        "• /cancel — Stop an active audiobook conversion\n"
        "• /help — Usage tips & supported voices"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        await private_notice(update)
        return

    message = update.effective_message
    if not message:
        return

    await message.reply_text(
        "📖 How to Convert a Book to Audio:\n\n"
        "1. Send any .epub or .txt file (up to 20 MB).\n"
        "2. Pick Entire Book or a Chapter Range (e.g. 1-5).\n"
        "3. Select Language (English or Hindi/Hinglish).\n"
        "4. Select Voice, Speaking Style, and Speed.\n"
        "5. Wait while the server converts and uploads MP3 part(s) (automatically split under 45 MB for long novels).\n\n"
        "Commands:\n"
        "• /textvoice — Reset & start fresh\n"
        "• /status — Check server disk, FFmpeg & active progress\n"
        "• /cancel — Immediately cancel a running conversion"
    )


async def textvoice_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        await private_notice(update)
        return
    await enter(update, context)


async def cancel_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        await private_notice(update)
        return
    await cancel_command(update, context)


async def status_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        await private_notice(update)
        return
    await status_command(update, context)


async def document_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        await private_notice(update)
        return
    await handle_document(update, context)


async def text_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        await private_notice(update)
        return
    await handle_text(update, context)


async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not is_owner(update):
        query = update.callback_query
        if query:
            await query.answer("This bot is private.", show_alert=True)
        return
    await callback(update, context)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Global error handler so transient Telegram polling hiccups never crash the daemon."""
    logger.error("Unhandled update exception: %s", context.error, exc_info=context.error)


async def _post_init(application: Application) -> None:
    """Register bot commands in Telegram's menu automatically on startup."""
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Welcome & overview"),
                BotCommand("textvoice", "Start converting an EPUB/TXT novel"),
                BotCommand("status", "Check Oracle server & conversion status"),
                BotCommand("cancel", "Cancel active conversion"),
                BotCommand("help", "Usage instructions"),
            ]
        )
    except Exception as exc:
        logger.warning("Could not register bot menu commands: %s", exc)


async def _post_shutdown(_application: Application) -> None:
    """Clean temporary directories on graceful server stop/restart."""
    cleanup_stale_jobs()
    logger.info("Bot shut down cleanly and temporary directories were purged.")


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
    application.add_handler(CommandHandler("textvoice", textvoice_router))
    application.add_handler(CommandHandler("cancel", cancel_router))
    application.add_handler(CommandHandler("status", status_router))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.Document.ALL, document_router))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_router)
    )
    application.add_error_handler(error_handler)

    logger.info("ModuBot TTS Novel (Oracle Server Edition) starting polling...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
