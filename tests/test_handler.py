"""Tests for onecmd.bot.handler — command dispatch, auth gate, keystroke sending."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from onecmd.bot.handler import (
    HELP_TEXT,
    _build_list_text,
    _send_keystrokes,
    create_handler,
)
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
    TermInfo(id="%1", pid=200, name="vim", title="editor"),
]


@pytest.fixture()
def bot():
    """Mock Telegram Bot with async methods."""
    b = MagicMock()
    msg = MagicMock()
    msg.message_id = 42
    b.send_message = AsyncMock(return_value=msg)
    b.edit_message_text = AsyncMock()
    b.delete_message = AsyncMock()
    b.answer_callback_query = AsyncMock()
    b.pin_chat_message = AsyncMock()
    return b


@pytest.fixture()
def store():
    """Mock Store with dict-backed get/set/delete."""
    s = MagicMock()
    _data: dict[str, str] = {}
    s.get.side_effect = lambda k: _data.get(k)
    s.set.side_effect = lambda k, v, *a, **kw: _data.__setitem__(k, v)
    s.delete.side_effect = lambda k: _data.pop(k, None)
    s._data = _data
    return s


@pytest.fixture()
def backend():
    """Mock ValidatedBackend returning fake terminals."""
    b = MagicMock()
    b.list.return_value = list(FAKE_TERMINALS)
    b.connected.return_value = True
    b.capture.return_value = "$ hello"
    b.send_keys.return_value = True
    return b


@pytest.fixture()
def config():
    return _make_config()


def _make_update(text: str, chat_id: int = 111, user_id: int = 1):
    """Build a mock Update with a text message."""
    update = MagicMock()
    update.callback_query = None
    update.effective_message.text = text
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    update.effective_chat.id = chat_id
    return update


def _make_callback_update(data: str = "refresh", chat_id: int = 111, user_id: int = 1):
    """Build a mock Update with a callback query."""
    update = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.from_user.username = "testuser"
    update.callback_query.message.chat_id = chat_id
    update.callback_query.data = data
    update.callback_query.id = "cb-123"
    return update


def _make_context(bot):
    ctx = MagicMock()
    ctx.bot = bot
    return ctx


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ── Helper unit tests ─────────────────────────────────────────────────


class TestBuildListText:
    def test_empty_terminals(self):
        assert _build_list_text([]) == "No terminal sessions found."

    @patch("onecmd.bot.handler._load_aliases", return_value={})
    def test_terminals_listed(self, _mock_aliases):
        result = _build_list_text(FAKE_TERMINALS)
        assert ".1</code> bash" in result
        assert ".2</code> vim" in result

    @patch("onecmd.bot.handler._load_aliases", return_value={"%0": "myterm"})
    def test_alias_shown(self, _mock_aliases):
        result = _build_list_text(FAKE_TERMINALS)
        assert "[myterm]" in result


class TestSendKeystrokes:
    def _all_payloads(self, mock_backend) -> str:
        """Concatenate all text arguments from send_keys calls."""
        return "".join(
            call[0][1] for call in mock_backend.send_keys.call_args_list
        )

    def test_basic_text_sends_with_newline(self):
        be = MagicMock()
        _send_keystrokes(be, "%0", "hello")
        combined = self._all_payloads(be)
        assert combined == "hello\n"
        # Plain text batched in first call, Enter sent separately
        assert be.send_keys.call_count == 2
        assert be.send_keys.call_args_list[0][0][1] == "hello"
        assert be.send_keys.call_args_list[1][0][1] == "\n"

    def test_ctrl_c_via_emoji(self):
        be = MagicMock()
        # ❤️c -> Ctrl+C
        _send_keystrokes(be, "%0", "\u2764\ufe0fc")
        combined = self._all_payloads(be)
        # Ctrl+C is chr(3), plus trailing Enter
        assert "\x03" in combined
        assert combined.endswith("\n")

    def test_suppress_newline_with_purple_heart(self):
        be = MagicMock()
        _send_keystrokes(be, "%0", "ls\U0001f49c")
        combined = self._all_payloads(be)
        assert combined == "ls"  # no trailing \n
        be.send_keys.assert_called_once()

    def test_literal_flag_passed(self):
        be = MagicMock()
        _send_keystrokes(be, "%0", "x\U0001f49c")
        # All calls should pass literal=True
        for call in be.send_keys.call_args_list:
            assert call[1].get("literal", call[0][2] if len(call[0]) > 2 else None) is True


# ── Handler integration tests ─────────────────────────────────────────


class TestOwnerRegistration:
    """First message registers owner."""

    def test_first_message_registers_owner(self, bot, store, backend, config):
        handler = create_handler(config, store, backend)
        update = _make_update("hello", user_id=42)
        _run(handler(update, _make_context(bot)))

        # store.set called with owner_id
        store.set.assert_any_call("owner_id", "42")
        # Welcome message + status pin sent
        assert bot.send_message.call_count >= 1
        text_sent = bot.send_message.call_args_list[0][1]["text"]
        assert "Welcome" in text_sent


class TestNonOwnerIgnored:
    """Non-owner messages silently dropped."""

    def test_non_owner_gets_no_response(self, bot, store, backend, config):
        # Pre-register owner as user 1
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update("hello", user_id=999)
        _run(handler(update, _make_context(bot)))

        bot.send_message.assert_not_called()


class TestListCommand:
    def test_dot_list_shows_terminals(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update(".list", user_id=1)
        _run(handler(update, _make_context(bot)))

        bot.send_message.assert_called()
        text_sent = bot.send_message.call_args[1]["text"]
        assert "bash" in text_sent
        assert "vim" in text_sent


class TestConnectByIndex:
    def test_dot_n_connects(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update(".1", user_id=1)
        _run(handler(update, _make_context(bot)))

        # Should send "Connected to bash" message
        calls = bot.send_message.call_args_list
        texts = [c[1]["text"] for c in calls]
        assert any("Connected to bash" in t for t in texts)

    def test_invalid_index_rejected(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update(".99", user_id=1)
        _run(handler(update, _make_context(bot)))

        text_sent = bot.send_message.call_args[1]["text"]
        assert "Invalid" in text_sent


class TestConnectedModeKeystrokes:
    def test_sends_keystrokes_when_connected(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        # Connect first
        _run(handler(_make_update(".1", user_id=1), _make_context(bot)))
        backend.send_keys.reset_mock()

        # Send text in connected mode
        _run(handler(_make_update("ls -la", user_id=1), _make_context(bot)))

        assert backend.send_keys.call_count >= 1
        # First call sends the literal text, second sends Enter
        term_id, payload = backend.send_keys.call_args_list[0][0]
        assert term_id == "%0"
        assert "ls -la" in payload


class TestHelpCommand:
    def test_dot_help_returns_help(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update(".help", user_id=1)
        _run(handler(update, _make_context(bot)))

        text_sent = bot.send_message.call_args[1]["text"]
        assert text_sent == HELP_TEXT


class TestExitCommand:
    def test_dot_exit_when_not_in_mgr(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update(".exit", user_id=1)
        _run(handler(update, _make_context(bot)))

        text_sent = bot.send_message.call_args[1]["text"]
        assert "Not in" in text_sent


class TestMgrCommand:
    @patch("onecmd.bot.handler.ManagerRouter")
    def test_dot_mgr_activates_manager(self, MockRouter, bot, store, backend, config):
        store._data["owner_id"] = "1"
        # Configure the mock router that create_handler will instantiate
        mock_router = MockRouter.return_value
        mock_router.activate.return_value = "Manager mode ON."
        mock_router.active = True

        handler = create_handler(config, store, backend)

        update = _make_update(".mgr", user_id=1)
        _run(handler(update, _make_context(bot)))

        mock_router.activate.assert_called_once()
        calls = bot.send_message.call_args_list
        texts = [c[1]["text"] for c in calls]
        assert any("Manager mode on" in t or "Manager mode ON" in t for t in texts)

    @patch("onecmd.bot.handler.ManagerRouter")
    def test_dot_mgr_toggle_off(self, MockRouter, bot, store, backend, config):
        store._data["owner_id"] = "1"
        mock_router = MockRouter.return_value
        mock_router.activate.return_value = "Manager mode ON."
        mock_router.active = True

        handler = create_handler(config, store, backend)

        # Activate
        _run(handler(_make_update(".mgr", user_id=1), _make_context(bot)))
        # Deactivate
        _run(handler(_make_update(".mgr", user_id=1), _make_context(bot)))

        calls = bot.send_message.call_args_list
        texts = [c[1]["text"] for c in calls]
        assert any("Manager mode off" in t for t in texts)


class TestRenameCommand:
    @patch("onecmd.bot.handler._save_alias")
    def test_rename_sets_alias(self, mock_save, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update(".rename 1 mybox", user_id=1)
        _run(handler(update, _make_context(bot)))

        mock_save.assert_called_once_with("%0", "mybox")
        text_sent = bot.send_message.call_args[1]["text"]
        assert "renamed" in text_sent

    def test_rename_usage_error(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update(".rename", user_id=1)
        _run(handler(update, _make_context(bot)))

        text_sent = bot.send_message.call_args[1]["text"]
        assert "Usage" in text_sent


class TestCallbackQuery:
    def test_refresh_callback_shows_terminal(self, bot, store, backend, config):
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        # Connect first via text message
        _run(handler(_make_update(".1", user_id=1), _make_context(bot)))

        # Now send a callback query
        cb_update = _make_callback_update("refresh", user_id=1)
        _run(handler(cb_update, _make_context(bot)))

        # answer_callback should be called
        bot.answer_callback_query.assert_called()
        # capture called for the connected terminal
        backend.capture.assert_called()


class TestTOTPGate:
    """TOTP gate blocks unauthenticated users when not weak_security."""

    def test_totp_blocks_unauthenticated(self, bot, backend):
        config = _make_config(weak_security=False)
        store_mock = MagicMock()
        _data: dict[str, str] = {"owner_id": "1", "totp_secret": "aa" * 20}
        store_mock.get.side_effect = lambda k: _data.get(k)
        store_mock.set.side_effect = lambda k, v, *a, **kw: _data.__setitem__(k, v)

        handler = create_handler(config, store_mock, backend)

        update = _make_update("hello", user_id=1)
        _run(handler(update, _make_context(bot)))

        text_sent = bot.send_message.call_args[1]["text"]
        assert "OTP" in text_sent

    @patch("onecmd.bot.handler.totp_verify", return_value=True)
    def test_totp_authenticates_with_valid_code(self, mock_verify, bot, backend):
        config = _make_config(weak_security=False)
        store_mock = MagicMock()
        _data: dict[str, str] = {"owner_id": "1", "totp_secret": "aa" * 20}
        store_mock.get.side_effect = lambda k: _data.get(k)
        store_mock.set.side_effect = lambda k, v, *a, **kw: _data.__setitem__(k, v)

        handler = create_handler(config, store_mock, backend)

        update = _make_update("123456", user_id=1)
        _run(handler(update, _make_context(bot)))

        mock_verify.assert_called_once_with("123456", "aa" * 20)
        text_sent = bot.send_message.call_args[1]["text"]
        assert "Authenticated" in text_sent

    def test_totp_callback_answered_but_blocked(self, bot, backend):
        config = _make_config(weak_security=False)
        store_mock = MagicMock()
        _data: dict[str, str] = {"owner_id": "1", "totp_secret": "aa" * 20}
        store_mock.get.side_effect = lambda k: _data.get(k)
        store_mock.set.side_effect = lambda k, v, *a, **kw: _data.__setitem__(k, v)

        handler = create_handler(config, store_mock, backend)

        cb_update = _make_callback_update("refresh", user_id=1)
        _run(handler(cb_update, _make_context(bot)))

        # Callback answered but no terminal display shown
        bot.answer_callback_query.assert_called()
        # No send_message for terminal content
        bot.send_message.assert_not_called()


class TestEmojiModifiers:
    def _all_payloads(self, mock_backend) -> str:
        """Concatenate all text arguments from send_keys calls."""
        return "".join(
            call[0][1] for call in mock_backend.send_keys.call_args_list
        )

    def test_ctrl_c_emoji(self):
        be = MagicMock()
        # ❤️c = Ctrl+C
        _send_keystrokes(be, "%0", "\u2764\ufe0fc")
        combined = self._all_payloads(be)
        assert "\x03" in combined  # Ctrl+C

    def test_alt_key_emoji(self):
        be = MagicMock()
        # 💙x = Alt+x
        _send_keystrokes(be, "%0", "\U0001f499x")
        combined = self._all_payloads(be)
        assert "\x1bx" in combined  # ESC + x = Alt

    def test_escape_emoji(self):
        be = MagicMock()
        # 💛 = Escape
        _send_keystrokes(be, "%0", "\U0001f49b")
        combined = self._all_payloads(be)
        assert "\x1b" in combined

    def test_enter_emoji(self):
        be = MagicMock()
        # 🧡 = Enter
        _send_keystrokes(be, "%0", "\U0001f9e1")
        combined = self._all_payloads(be)
        assert "\n" in combined


class TestDefaultBehavior:
    def test_unrecognized_text_shows_list(self, bot, store, backend, config):
        """When not connected and text is not a command, show terminal list."""
        store._data["owner_id"] = "1"
        handler = create_handler(config, store, backend)

        update = _make_update("random text", user_id=1)
        _run(handler(update, _make_context(bot)))

        text_sent = bot.send_message.call_args[1]["text"]
        assert "bash" in text_sent or "Terminal" in text_sent

    def test_no_user_returns_early(self, bot, store, backend, config):
        """Update with no user is silently ignored."""
        handler = create_handler(config, store, backend)

        update = MagicMock()
        update.callback_query = None
        update.effective_message.text = "hello"
        update.effective_user = None
        update.effective_chat.id = 111

        _run(handler(update, _make_context(bot)))
        bot.send_message.assert_not_called()

    def test_no_message_returns_early(self, bot, store, backend, config):
        """Update with no effective_message is silently ignored."""
        handler = create_handler(config, store, backend)

        update = MagicMock()
        update.callback_query = None
        update.effective_message = None

        _run(handler(update, _make_context(bot)))
        bot.send_message.assert_not_called()
