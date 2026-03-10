"""Telegram message helper functions.

Calling spec:
  Inputs:  bot (telegram.Bot instance), chat_id (int), text/msg_id/callback_id
  Outputs: msg_id (int) or bool; None/False on failure
  Side effects: Telegram API calls (send, edit, delete, answer callback)

Functions:
  html_escape(text) -> str
      Escapes <, >, & for Telegram HTML.

  send_message(bot, chat_id, text, reply_markup=None, parse_mode="HTML") -> int | None
      Send a text message. Returns message_id on success, None on failure.

  edit_message(bot, chat_id, msg_id, text, parse_mode="HTML") -> bool
      Edit an existing message. Returns True on success, False on failure.

  delete_message(bot, chat_id, msg_id) -> bool
      Delete a message. Returns True on success, False on failure.

  answer_callback(bot, callback_id) -> bool
      Acknowledge a callback query. Returns True on success, False on failure.

Guarding:
  - Text length capped at 4096 chars (Telegram limit), truncated with "..."
  - HTML-escapes user content in <pre> blocks (escape <, >, &)
  - chat_id validated as integer
  - All functions handle telegram.error.TelegramError gracefully (log + return None/False)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.error import TelegramError

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 4096


def html_escape(text: str) -> str:
    """Escape <, >, & for Telegram HTML messages."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(text: str) -> str:
    """Truncate text to Telegram's 4096-char limit, adding '...' if truncated."""
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - 3] + "..."


def _validate_chat_id(chat_id: int) -> int:
    """Validate chat_id is an integer. Raises TypeError if not."""
    if not isinstance(chat_id, int):
        raise TypeError(f"chat_id must be int, got {type(chat_id).__name__}")
    return chat_id


async def send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: object | None = None,
    parse_mode: str = "HTML",
) -> int | None:
    """Send a message via Telegram. Returns message_id or None on failure."""
    try:
        _validate_chat_id(chat_id)
        text = _truncate(text)
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return msg.message_id
    except TypeError:
        raise
    except TelegramError as exc:
        logger.error("send_message failed chat_id=%s: %s", chat_id, exc)
        return None


async def edit_message(
    bot: Bot,
    chat_id: int,
    msg_id: int,
    text: str,
    parse_mode: str = "HTML",
) -> bool:
    """Edit an existing message. Returns True on success, False on failure."""
    try:
        _validate_chat_id(chat_id)
        text = _truncate(text)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return True
    except TypeError:
        raise
    except TelegramError as exc:
        logger.error(
            "edit_message failed chat_id=%s msg_id=%s: %s", chat_id, msg_id, exc
        )
        return False


async def delete_message(bot: Bot, chat_id: int, msg_id: int) -> bool:
    """Delete a message. Returns True on success, False on failure."""
    try:
        _validate_chat_id(chat_id)
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        return True
    except TypeError:
        raise
    except TelegramError as exc:
        logger.error(
            "delete_message failed chat_id=%s msg_id=%s: %s", chat_id, msg_id, exc
        )
        return False


async def send_chat_action(bot: Bot, chat_id: int, action: str = "typing") -> bool:
    """Send a chat action (e.g. 'typing'). Returns True on success."""
    try:
        _validate_chat_id(chat_id)
        await bot.send_chat_action(chat_id=chat_id, action=action)
        return True
    except TypeError:
        raise
    except TelegramError as exc:
        logger.error("send_chat_action failed chat_id=%s: %s", chat_id, exc)
        return False


async def answer_callback(bot: Bot, callback_id: str) -> bool:
    """Acknowledge a callback query. Returns True on success, False on failure."""
    try:
        await bot.answer_callback_query(callback_query_id=callback_id)
        return True
    except TelegramError as exc:
        logger.error("answer_callback failed callback_id=%s: %s", callback_id, exc)
        return False
