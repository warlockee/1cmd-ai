"""Tests for onecmd.bot.api — Telegram message helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from telegram.error import TelegramError

from onecmd.bot.api import (
    MAX_TEXT_LENGTH,
    answer_callback,
    delete_message,
    edit_message,
    html_escape,
    send_message,
)


# ── html_escape ─────────────────────────────────────────────────────


class TestHtmlEscape:
    def test_escapes_ampersand(self):
        assert html_escape("a & b") == "a &amp; b"

    def test_escapes_less_than(self):
        assert html_escape("a < b") == "a &lt; b"

    def test_escapes_greater_than(self):
        assert html_escape("a > b") == "a &gt; b"

    def test_escapes_all_together(self):
        assert html_escape("<b>A & B</b>") == "&lt;b&gt;A &amp; B&lt;/b&gt;"

    def test_no_escaping_needed(self):
        assert html_escape("hello world") == "hello world"

    def test_empty_string(self):
        assert html_escape("") == ""

    def test_ampersand_order_matters(self):
        # & must be escaped first, otherwise &lt; would become &amp;lt;
        result = html_escape("&lt;")
        assert result == "&amp;lt;"


# ── send_message ────────────────────────────────────────────────────


class TestSendMessage:
    def _make_bot(self, message_id: int = 42):
        bot = MagicMock()
        msg = MagicMock()
        type(msg).message_id = PropertyMock(return_value=message_id)
        bot.send_message = AsyncMock(return_value=msg)
        return bot

    @pytest.mark.asyncio
    async def test_returns_message_id(self):
        bot = self._make_bot(99)
        result = await send_message(bot, 123, "hi")
        assert result == 99

    @pytest.mark.asyncio
    async def test_calls_bot_with_correct_args(self):
        bot = self._make_bot()
        await send_message(bot, 123, "hello")
        bot.send_message.assert_called_once_with(
            chat_id=123,
            text="hello",
            parse_mode="HTML",
            reply_markup=None,
            disable_web_page_preview=True,
        )

    @pytest.mark.asyncio
    async def test_long_text_split_no_content_loss(self):
        bot = self._make_bot()
        long_text = "x" * (MAX_TEXT_LENGTH + 100)
        await send_message(bot, 123, long_text)
        assert bot.send_message.await_count >= 2
        sent = "".join(
            call.kwargs["text"] for call in bot.send_message.await_args_list)
        assert sent == long_text
        for call in bot.send_message.await_args_list:
            assert len(call.kwargs["text"]) <= MAX_TEXT_LENGTH

    @pytest.mark.asyncio
    async def test_text_at_limit_single_message(self):
        bot = self._make_bot()
        text = "x" * MAX_TEXT_LENGTH
        await send_message(bot, 123, text)
        bot.send_message.assert_awaited_once()
        assert bot.send_message.call_args.kwargs["text"] == text

    @pytest.mark.asyncio
    async def test_html_failure_falls_back_to_plain(self):
        bot = MagicMock()
        good_msg = MagicMock()
        type(good_msg).message_id = PropertyMock(return_value=42)
        bot.send_message = AsyncMock(
            side_effect=[TelegramError("can't parse entities"), good_msg])
        result = await send_message(bot, 123, "<broken")
        assert result == 42
        assert bot.send_message.await_count == 2
        assert bot.send_message.await_args_list[1].kwargs["parse_mode"] is None

    @pytest.mark.asyncio
    async def test_telegram_error_returns_none(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=TelegramError("fail"))
        result = await send_message(bot, 123, "hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_reply_markup_passed_through(self):
        bot = self._make_bot()
        markup = MagicMock()
        await send_message(bot, 123, "hi", reply_markup=markup)
        assert bot.send_message.call_args.kwargs["reply_markup"] is markup

    @pytest.mark.asyncio
    async def test_custom_parse_mode(self):
        bot = self._make_bot()
        await send_message(bot, 123, "hi", parse_mode="Markdown")
        assert bot.send_message.call_args.kwargs["parse_mode"] == "Markdown"


# ── edit_message ────────────────────────────────────────────────────


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        result = await edit_message(bot, 123, 456, "new text")
        assert result is True

    @pytest.mark.asyncio
    async def test_calls_bot_with_correct_args(self):
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        await edit_message(bot, 123, 456, "updated")
        bot.edit_message_text.assert_called_once_with(
            chat_id=123,
            message_id=456,
            text="updated",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @pytest.mark.asyncio
    async def test_long_text_edits_first_chunk_and_sends_rest(self):
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        sent_msg = MagicMock()
        type(sent_msg).message_id = PropertyMock(return_value=999)
        bot.send_message = AsyncMock(return_value=sent_msg)
        long_text = "y" * (MAX_TEXT_LENGTH + 50)
        result = await edit_message(bot, 123, 456, long_text)
        assert result is True
        bot.edit_message_text.assert_awaited_once()
        first_chunk = bot.edit_message_text.call_args.kwargs["text"]
        assert len(first_chunk) <= MAX_TEXT_LENGTH
        assert bot.send_message.await_count >= 1
        delivered = first_chunk + "".join(
            call.kwargs["text"] for call in bot.send_message.await_args_list)
        assert delivered == long_text

    @pytest.mark.asyncio
    async def test_telegram_error_returns_false(self):
        bot = MagicMock()
        bot.edit_message_text = AsyncMock(side_effect=TelegramError("fail"))
        result = await edit_message(bot, 123, 456, "text")
        assert result is False


# ── delete_message ──────────────────────────────────────────────────


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock()
        result = await delete_message(bot, 123, 456)
        assert result is True

    @pytest.mark.asyncio
    async def test_calls_bot_correctly(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock()
        await delete_message(bot, 123, 456)
        bot.delete_message.assert_called_once_with(chat_id=123, message_id=456)

    @pytest.mark.asyncio
    async def test_telegram_error_returns_false(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock(side_effect=TelegramError("fail"))
        result = await delete_message(bot, 123, 456)
        assert result is False


# ── answer_callback ─────────────────────────────────────────────────


class TestAnswerCallback:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        bot = MagicMock()
        bot.answer_callback_query = AsyncMock()
        result = await answer_callback(bot, "cb_123")
        assert result is True

    @pytest.mark.asyncio
    async def test_telegram_error_returns_false(self):
        bot = MagicMock()
        bot.answer_callback_query = AsyncMock(side_effect=TelegramError("fail"))
        result = await answer_callback(bot, "cb_123")
        assert result is False


# ── chat_id validation ──────────────────────────────────────────────


class TestChatIdValidation:
    @pytest.mark.asyncio
    async def test_send_message_rejects_string_chat_id(self):
        bot = MagicMock()
        with pytest.raises(TypeError, match="chat_id must be int"):
            await send_message(bot, "not_an_int", "hi")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_edit_message_rejects_string_chat_id(self):
        bot = MagicMock()
        with pytest.raises(TypeError, match="chat_id must be int"):
            await edit_message(bot, "bad", 1, "hi")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_delete_message_rejects_string_chat_id(self):
        bot = MagicMock()
        with pytest.raises(TypeError, match="chat_id must be int"):
            await delete_message(bot, None, 1)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_send_message_rejects_float_chat_id(self):
        bot = MagicMock()
        with pytest.raises(TypeError, match="chat_id must be int"):
            await send_message(bot, 3.14, "hi")  # type: ignore[arg-type]
