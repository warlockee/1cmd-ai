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
    parse_mode: str | None = "HTML",
) -> int | None:
    """Send a message via Telegram. Returns last message_id or None on failure.

    Long text is split into Telegram-sized chunks. The reply_markup is only
    attached to the final chunk. If HTML parsing fails for a chunk, it is
    retried as plain text so content is still delivered.
    """
    _validate_chat_id(chat_id)
    chunks = _split(text)
    last_id: int | None = None
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                reply_markup=reply_markup if is_last else None,
                disable_web_page_preview=True,
            )
            last_id = msg.message_id
        except TelegramError as exc:
            logger.warning(
                "send_message HTML failed chat_id=%s, retrying as plain: %s",
                chat_id, exc)
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=None,
                    reply_markup=reply_markup if is_last else None,
                    disable_web_page_preview=True,
                )
                last_id = msg.message_id
            except TelegramError as exc2:
                logger.error(
                    "send_message failed chat_id=%s: %s", chat_id, exc2)
    return last_id


async def edit_message(
    bot: Bot,
    chat_id: int,
    msg_id: int,
    text: str,
    parse_mode: str | None = "HTML",
) -> bool:
    """Edit an existing message. Returns True on success, False on failure.

    If text exceeds Telegram's limit, the first chunk replaces the original
    message and remaining chunks are sent as new messages.
    """
    _validate_chat_id(chat_id)
    chunks = _split(text)
    first = chunks[0]
    ok = False
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=first,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        ok = True
    except TelegramError as exc:
        logger.warning(
            "edit_message HTML failed chat_id=%s msg_id=%s, retrying as plain: %s",
            chat_id, msg_id, exc)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=first,
                parse_mode=None,
                disable_web_page_preview=True,
            )
            ok = True
        except TelegramError as exc2:
            logger.error(
                "edit_message failed chat_id=%s msg_id=%s: %s",
                chat_id, msg_id, exc2)
    for chunk in chunks[1:]:
        await send_message(bot, chat_id, chunk, parse_mode=parse_mode)
    return ok


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


async def pin_message(bot: Bot, chat_id: int, msg_id: int) -> bool:
    """Pin a message silently. Returns True on success."""
    try:
        _validate_chat_id(chat_id)
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg_id,
            disable_notification=True,
        )
        return True
    except TypeError:
        raise
    except TelegramError as exc:
        logger.error("pin_message failed chat_id=%s msg_id=%s: %s", chat_id, msg_id, exc)
        return False
