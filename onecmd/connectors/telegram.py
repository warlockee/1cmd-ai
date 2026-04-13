"""Telegram connector — wraps python-telegram-bot into the Connector interface.

This module extracts the Telegram-specific transport (sending, editing,
deleting messages, long-polling) from the bot handler into the connector
abstraction.  The bot handler logic remains in onecmd/bot/handler.py but
now communicates through the Connector interface.

Calling spec:
  Inputs:  Telegram bot token (str)
  Outputs: TelegramConnector instance
  Side effects: Telegram API calls, long-polling loop

Usage:
  connector = TelegramConnector(token="BOT_TOKEN")
  await connector.start(message_handler, callback_handler)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler as TGMessageHandler,
    filters,
)

from onecmd.connectors.base import CallbackHandler, Connector, MessageHandler

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 4096


def _split(text: str) -> list[str]:
    """Split text into chunks no larger than Telegram's 4096-char limit.

    Prefers breaking on line boundaries, then spaces, then hard cuts.
    """
    if len(text) <= MAX_TEXT_LENGTH:
        return [text] if text else [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > MAX_TEXT_LENGTH:
        cut = remaining.rfind("\n", 0, MAX_TEXT_LENGTH)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, MAX_TEXT_LENGTH)
        if cut <= 0:
            cut = MAX_TEXT_LENGTH
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n") if remaining[cut:cut+1] == "\n" else remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramConnector(Connector):
    """Connector implementation for Telegram using python-telegram-bot v21+."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._app: Application | None = None
        self._bot = None

    @property
    def platform_name(self) -> str:
        return "telegram"

    async def start(self, message_handler: MessageHandler,
                    callback_handler: CallbackHandler | None = None) -> None:
        """Build the Application, register handlers, and start long-polling.

        This method blocks until the bot is stopped.
        """
        logger.info("Building Telegram connector...")

        self._app = Application.builder().token(self._token).build()
        self._bot = self._app.bot

        # Wrap platform-specific events into the uniform handler signature
        async def _on_message(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_message is None:
                return
            user = update.effective_user
            chat = update.effective_chat
            if user is None or chat is None:
                return
            text = update.effective_message.text or ""
            await message_handler(
                self,
                str(chat.id),
                str(user.id),
                text,
                update,
            )

        async def _on_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.callback_query is None:
                return
            user = update.callback_query.from_user
            msg = update.callback_query.message
            if user is None or msg is None:
                return
            chat_id = str(msg.chat_id)
            data = update.callback_query.data or ""
            cb_id = str(update.callback_query.id)
            if callback_handler:
                await callback_handler(
                    self, chat_id, str(user.id), data, cb_id, update)

        # Register handlers
        self._app.add_handler(CommandHandler("start", _on_message))
        self._app.add_handler(
            TGMessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
        if callback_handler:
            self._app.add_handler(CallbackQueryHandler(_on_callback))

        logger.info("Starting Telegram long-polling...")
        self._app.run_polling(
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
            drop_pending_updates=True,
        )
        logger.info("Telegram polling stopped.")

    async def stop(self) -> None:
        if self._app:
            await self._app.stop()
            await self._app.shutdown()

    async def send_message(self, chat_id: str, text: str, **kwargs: Any) -> str:
        """Send a message.  Returns the last sent message ID as string.

        Long text is split into Telegram-sized chunks. The reply_markup is
        only attached to the final chunk. If HTML parsing fails for a chunk
        (e.g. unbalanced tags from AI-generated markdown), it is retried as
        plain text so the user still receives the content.
        """
        if self._bot is None:
            raise RuntimeError("Connector not started")
        parse_mode = kwargs.get("parse_mode", "HTML")
        reply_markup = kwargs.get("reply_markup")
        chunks = _split(text)
        last_id = ""
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            try:
                msg = await self._bot.send_message(
                    chat_id=int(chat_id),
                    text=chunk,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup if is_last else None,
                    disable_web_page_preview=True,
                )
                last_id = str(msg.message_id)
            except TelegramError as exc:
                logger.warning(
                    "send_message HTML failed chat_id=%s, retrying as plain: %s",
                    chat_id, exc)
                try:
                    msg = await self._bot.send_message(
                        chat_id=int(chat_id),
                        text=chunk,
                        parse_mode=None,
                        reply_markup=reply_markup if is_last else None,
                        disable_web_page_preview=True,
                    )
                    last_id = str(msg.message_id)
                except TelegramError as exc2:
                    logger.error(
                        "send_message failed chat_id=%s: %s", chat_id, exc2)
        return last_id

    async def edit_message(self, chat_id: str, message_id: str,
                           text: str, **kwargs: Any) -> None:
        if self._bot is None:
            return
        parse_mode = kwargs.get("parse_mode", "HTML")
        chunks = _split(text)
        first = chunks[0]
        try:
            await self._bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=first,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning(
                "edit_message HTML failed, retrying as plain: %s", exc)
            try:
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=first,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
            except TelegramError as exc2:
                logger.error("edit_message failed: %s", exc2)
        for chunk in chunks[1:]:
            await self.send_message(chat_id, chunk, parse_mode=parse_mode)

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.delete_message(
                chat_id=int(chat_id), message_id=int(message_id))
        except TelegramError as exc:
            logger.error("delete_message failed: %s", exc)

    async def send_image(self, chat_id: str, image: bytes,
                         caption: str = "") -> str:
        if self._bot is None:
            raise RuntimeError("Connector not started")
        try:
            msg = await self._bot.send_photo(
                chat_id=int(chat_id), photo=image, caption=caption)
            return str(msg.message_id)
        except TelegramError as exc:
            logger.error("send_image failed: %s", exc)
            return ""

    async def answer_callback(self, callback_id: str) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.answer_callback_query(
                callback_query_id=callback_id)
        except TelegramError as exc:
            logger.error("answer_callback failed: %s", exc)

    async def send_chat_action(self, chat_id: str,
                               action: str = "typing") -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send_chat_action(
                chat_id=int(chat_id), action=action)
        except TelegramError as exc:
            logger.error("send_chat_action failed: %s", exc)
