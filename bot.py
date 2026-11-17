import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, OWNER_ID
from modules.textvoice.handler import (
    enter,
    handle_document,
    handle_text,
    callback,
    cleanup_stale_jobs,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == OWNER_ID


async def private_notice(update: Update) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "This bot is private and can only be used by its owner."
        )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        await private_notice(update)
        return

    await update.effective_message.reply_text(
        "🎙️ Welcome to Text-to-Voice!\n\n"
        "Use /textvoice to start creating an audiobook.\n"
        "Supported input files: EPUB and TXT."
    )


async def textvoice_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        await private_notice(update)
        return

    await enter(update, context)


async def document_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        await private_notice(update)
        return

    await handle_document(update, context)


async def text_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        await private_notice(update)
        return

    await handle_text(update, context)


async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        query = update.callback_query
        if query:
            await query.answer(
                "This bot is private.",
                show_alert=True,
            )
        return

    await callback(update, context)


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CommandHandler("textvoice", textvoice_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_router,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    # Remove temporary files left behind by an earlier run that was stopped.
    cleanup_stale_jobs()

    logging.info("Standalone TTS bot is starting.")
    application.run_polling()


if __name__ == "__main__":
    main()
