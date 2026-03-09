"""Terminal text display formatting for Telegram.

Calling spec:
  Inputs:  bot (telegram.Bot), chat_id (int), terminal text (str),
           tracked_msgs (TrackedMessages), split_messages (bool),
           visible_lines (int)
  Outputs: None
  Side effects: sends/deletes Telegram messages, mutates tracked_msgs

Functions:
  last_n_lines(text, n) -> str
      Return the last n lines of text.

  format_chunks(text, split) -> list[str]
      Format escaped terminal text into HTML <pre> chunks respecting
      Telegram's 4096-char limit.

  send_terminal_display(bot, chat_id, text, tracked_msgs, split_messages, visible_lines) -> None
      Delete old tracked messages, format terminal text, send as <pre> blocks
      with a refresh button on the last message.

  delete_tracked_messages(bot, chat_id, tracked_msgs) -> None
      Delete all messages in tracked_msgs and clear the list.

Classes:
  TrackedMessages
      Thread-safe list of sent message IDs for delete-on-refresh.

Guarding:
  - HTML-escapes <, >, & via bot.api.html_escape
  - Splits on line boundaries to avoid breaking lines mid-message
  - Empty text produces a single empty <pre></pre> message
  - Max MAX_TRACKED_MSGS (50) tracked; oldest silently dropped
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from onecmd.bot.api import delete_message, html_escape, send_message

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

# Telegram message limit minus <pre></pre> tag overhead (13 chars)
MAX_MSG_LEN = 4096 - len("<pre></pre>")
MAX_TRACKED_MSGS = 50
REFRESH_BTN_TEXT = "\U0001f504 Refresh"
REFRESH_CALLBACK_DATA = "refresh"


class TrackedMessages:
    """Thread-safe tracker for sent terminal message IDs."""

    def __init__(self) -> None:
        self._ids: list[int] = []
        self._lock = threading.Lock()

    def add(self, msg_id: int) -> None:
        with self._lock:
            if len(self._ids) >= MAX_TRACKED_MSGS:
                self._ids.pop(0)
            self._ids.append(msg_id)

    def pop_all(self) -> list[int]:
        with self._lock:
            ids = self._ids[:]
            self._ids.clear()
            return ids

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._ids)


def last_n_lines(text: str, n: int) -> str:
    """Return the last *n* lines of *text*."""
    lines = text.split("\n")
    if len(lines) <= n:
        return text
    return "\n".join(lines[-n:])


def format_chunks(escaped: str, split: bool) -> list[str]:
    """Break already-escaped text into ``<pre>``-wrapped chunks.

    When *split* is False, the text is truncated from the top to fit a single
    message (keeping the tail).  When *split* is True, the text is split across
    multiple messages on line boundaries.
    """
    if not escaped:
        return ["<pre></pre>"]

    if not split:
        # Truncate mode: keep the tail that fits in one message.
        if len(escaped) > MAX_MSG_LEN:
            start = len(escaped) - MAX_MSG_LEN
            nl = escaped.find("\n", start)
            if 0 <= nl < len(escaped):
                start = nl + 1
            escaped = escaped[start:]
        return [f"<pre>{escaped}</pre>"]

    # Split mode: break into multiple messages on line boundaries.
    chunks: list[str] = []
    while escaped:
        if len(escaped) <= MAX_MSG_LEN:
            chunks.append(f"<pre>{escaped}</pre>")
            break

        # Find last newline within the limit.
        cut = escaped.rfind("\n", 0, MAX_MSG_LEN)
        if cut <= 0:
            # No newline found; hard-cut.
            chunk = escaped[:MAX_MSG_LEN]
            escaped = escaped[MAX_MSG_LEN:]
        else:
            chunk = escaped[:cut]
            escaped = escaped[cut + 1:]  # skip newline

        chunks.append(f"<pre>{chunk}</pre>")

    return chunks or ["<pre></pre>"]


async def delete_tracked_messages(bot: Bot, chat_id: int, tracked_msgs: TrackedMessages) -> None:
    """Delete all tracked messages and clear the list."""
    ids = tracked_msgs.pop_all()
    for msg_id in reversed(ids):
        await delete_message(bot, chat_id, msg_id)


async def send_terminal_display(
    bot: Bot,
    chat_id: int,
    text: str,
    tracked_msgs: TrackedMessages,
    split_messages: bool = False,
    visible_lines: int = 40,
) -> None:
    """Format and send terminal text as HTML ``<pre>`` blocks.

    Deletes previously tracked messages first, then sends new ones.
    The last message includes a refresh inline-keyboard button.
    """
    await delete_tracked_messages(bot, chat_id, tracked_msgs)

    tail = last_n_lines(text, visible_lines)
    escaped = html_escape(tail)
    chunks = format_chunks(escaped, split_messages)

    refresh_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(REFRESH_BTN_TEXT, callback_data=REFRESH_CALLBACK_DATA)]]
    )

    # Send all chunks; attach keyboard only to the last one.
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = refresh_markup if is_last else None
        msg_id = await send_message(bot, chat_id, chunk, reply_markup=markup)
        if msg_id is not None:
            tracked_msgs.add(msg_id)
