"""Telegram long-polling loop using python-telegram-bot v21+.

Calling spec:
  Inputs:  Config (with .apikey), handler_callback (async fn(Update, Context))
  Outputs: None (runs forever until SIGTERM/SIGINT)
  Side effects: Telegram long-poll via python-telegram-bot Application

Handlers registered:
  - CommandHandler("start") -> welcome message
  - MessageHandler(TEXT & ~COMMAND) -> handler_callback
  - CallbackQueryHandler -> handler_callback

Shutdown:
  - Graceful on SIGTERM/SIGINT (handled by Application.run_polling)

Usage:
  from onecmd.bot.poller import run_bot
  run_bot(config, handler_callback)

  handler_callback signature: async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE)
  This callback will be provided by bot/handler.py. It receives both text messages
  and callback queries (button presses).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Coroutine, Any

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from onecmd.config import Config

logger = logging.getLogger(__name__)

# Type alias for the handler callback
HandlerCallback = Callable[
    [Update, ContextTypes.DEFAULT_TYPE],
    Coroutine[Any, Any, None],
]

WELCOME_TEXT = (
    "Welcome to 1cmd! Send <b>.help</b> for available commands.\n"
    "Send <b>.list</b> to see your terminals."
)


async def _start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command with a welcome message."""
    if update.effective_message is not None:
        await update.effective_message.reply_text(WELCOME_TEXT, parse_mode="HTML")
        logger.info(
            "start command from user_id=%s chat_id=%s",
            update.effective_user.id if update.effective_user else "unknown",
            update.effective_chat.id if update.effective_chat else "unknown",
        )


def run_bot(config: Config, handler_callback: HandlerCallback) -> None:
    """Build the Application, register handlers, and start long-polling.

    This function blocks until the bot is stopped by SIGTERM or SIGINT.
    The Application.run_polling() method installs signal handlers and
    performs graceful shutdown automatically.

    Args:
        config: Validated Config with .apikey set.
        handler_callback: Async function(update, context) that processes
            text messages and callback queries. Will be provided by
            bot/handler.py in Wave 3.
    """
    logger.info("Building Telegram bot application...")

    application = (
        Application.builder()
        .token(config.apikey)
        .build()
    )

    # /start -> welcome message
    application.add_handler(CommandHandler("start", _start_command))

    # Text messages (not commands) -> handler_callback
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handler_callback)
    )

    # Button presses (inline keyboard callbacks) -> handler_callback
    application.add_handler(CallbackQueryHandler(handler_callback))

    logger.info("Starting long-polling...")
    application.run_polling(
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        drop_pending_updates=True,
    )
    logger.info("Bot polling stopped.")
