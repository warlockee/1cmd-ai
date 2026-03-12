"""Tests for _notify_sync — background notification delivery with escaping and fallback."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from onecmd.bot.handler import create_handler
from onecmd.config import Config
from onecmd.terminal.backend import TermInfo


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_config(**overrides) -> Config:
    defaults = dict(
        apikey="test-token-123",
        dbfile=":memory:",
        weak_security=True,
        danger_mode=False,
        enable_otp=False,
        verbose=False,
        visible_lines=40,
        split_messages=False,
        mgr_model=None,
        otp_timeout=300,
        anthropic_api_key=None,
        google_api_key=None,
    )
    defaults.update(overrides)
    return Config(**defaults)


FAKE_TERMINALS = [
    TermInfo(id="%0", pid=100, name="bash", title="~"),
]


@pytest.fixture()
def backend():
    b = MagicMock()
    b.list.return_value = list(FAKE_TERMINALS)
    b.connected.return_value = True
    b.capture.return_value = "$ "
    b.send_keys.return_value = True
    return b


@pytest.fixture()
def store():
    s = MagicMock()
    _data: dict[str, str] = {}
    s.get.side_effect = lambda k: _data.get(k)
    s.set.side_effect = lambda k, v, *a, **kw: _data.__setitem__(k, v)
    s.delete.side_effect = lambda k: _data.pop(k, None)
    s._data = _data
    return s


def _make_update(text: str):
    update = MagicMock()
    update.callback_query = None
    update.effective_message.text = text
    update.effective_user.id = 1
    update.effective_user.username = "test"
    update.effective_chat.id = 111
    return update


def _setup_handler_with_loop(config, store, backend, bot):
    """Create handler, trigger it once on a persistent loop to set _bot/_loop,
    and return (handler, notify_fn, loop)."""
    with patch("onecmd.bot.handler.ManagerRouter") as MockRouter:
        handler = create_handler(config, store, backend)
        notify_fn = MockRouter.call_args[0][2]

    store._data["owner_id"] = "1"
    ctx = MagicMock()
    ctx.bot = bot

    loop = asyncio.new_event_loop()
    loop.run_until_complete(handler(_make_update(".list"), ctx))
    return handler, notify_fn, loop


# ── Tests ─────────────────────────────────────────────────────────────


class TestNotifySyncHTMLEscape:
    """Notification text containing <, >, & must be escaped for Telegram HTML."""

    def test_html_entities_escaped(self, store, backend):
        config = _make_config()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))

        _, notify_fn, loop = _setup_handler_with_loop(config, store, backend, bot)

        terminal_output = "$ grep -r 'x<y && z>w' /tmp 2>&1"
        notify_fn(111, terminal_output)
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.close()

        # Find the notification call (not the .list response)
        calls = bot.send_message.call_args_list
        notify_calls = [c for c in calls if "grep" in str(c)]
        assert len(notify_calls) == 1
        sent_text = notify_calls[0][1]["text"]
        assert "&lt;" in sent_text
        assert "&gt;" in sent_text
        assert "&amp;" in sent_text


class TestNotifySyncFallback:
    """When HTML-escaped send fails, fall back to plain text."""

    def test_falls_back_to_plain_text(self, store, backend):
        from telegram.error import TelegramError

        config = _make_config()
        msg_ok = MagicMock(message_id=1)
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=msg_ok)

        _, notify_fn, loop = _setup_handler_with_loop(config, store, backend, bot)

        # First call (HTML) raises TelegramError (parsed as bad HTML),
        # second call (plain text) succeeds.
        bot.send_message = AsyncMock(
            side_effect=[TelegramError("can't parse entities"), msg_ok])

        notify_fn(111, "output with <angle> brackets")
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.close()

        assert bot.send_message.call_count == 2
        # Second call should have parse_mode=None (plain text fallback)
        fallback_call = bot.send_message.call_args_list[1]
        assert fallback_call[1].get("parse_mode") is None


class TestNotifySyncBotNone:
    """When bot is not set, notification is dropped with a warning."""

    def test_logs_warning_when_bot_none(self, store, backend, caplog):
        config = _make_config()

        with patch("onecmd.bot.handler.ManagerRouter") as MockRouter:
            create_handler(config, store, backend)
            notify_fn = MockRouter.call_args[0][2]

        # Don't trigger handler — _bot stays None
        with caplog.at_level(logging.WARNING):
            notify_fn(111, "test message")

        assert any("bot is None" in r.message for r in caplog.records)


class TestNotifySyncLoopClosed:
    """When event loop is closed, notification is dropped with a warning."""

    def test_logs_warning_when_loop_closed(self, store, backend, caplog):
        config = _make_config()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))

        _, notify_fn, loop = _setup_handler_with_loop(config, store, backend, bot)
        loop.close()  # Close the loop before calling notify

        with caplog.at_level(logging.WARNING):
            notify_fn(111, "test after loop closed")

        assert any("event loop unavailable" in r.message for r in caplog.records)


class TestNotifySyncFutureException:
    """Unhandled exceptions in the coroutine are logged via done_callback."""

    def test_exception_logged(self, store, backend):
        config = _make_config()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))

        _, notify_fn, loop = _setup_handler_with_loop(config, store, backend, bot)

        # Make send_message raise an unexpected error (not TelegramError)
        bot.send_message = AsyncMock(side_effect=RuntimeError("connection lost"))

        logged_errors: list[str] = []

        def capture_error(msg, *args):
            logged_errors.append(msg % args if args else msg)

        with patch.object(logging.getLogger("onecmd.bot.handler"), "error", capture_error):
            notify_fn(111, "test")
            loop.run_until_complete(asyncio.sleep(0.1))

        loop.close()

        assert any("connection lost" in e for e in logged_errors)
