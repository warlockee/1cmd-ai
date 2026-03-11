"""Tests for the connector abstraction layer (P2.1).

Tests cover:
  - base.py: Connector ABC cannot be instantiated directly
  - telegram.py: TelegramConnector implements all required methods
  - slack.py: SlackConnector implements all required methods, HTML-to-Slack conversion
  - handler.py: Unified handler processes commands correctly
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from onecmd.connectors.base import Connector
from onecmd.connectors.telegram import TelegramConnector, _truncate as tg_truncate
from onecmd.connectors.slack import _html_to_slack, _truncate as slack_truncate


# ---------------------------------------------------------------------------
# base.py
# ---------------------------------------------------------------------------


class TestConnectorABC:
    """Connector is abstract and cannot be instantiated."""

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Connector()

    def test_subclass_must_implement_all(self):
        class Incomplete(Connector):
            @property
            def platform_name(self):
                return "test"

        with pytest.raises(TypeError):
            Incomplete()


# ---------------------------------------------------------------------------
# telegram.py
# ---------------------------------------------------------------------------


class TestTelegramConnector:
    def test_platform_name(self):
        c = TelegramConnector(token="test-token")
        assert c.platform_name == "telegram"

    def test_truncate_short(self):
        assert tg_truncate("hello") == "hello"

    def test_truncate_long(self):
        text = "x" * 5000
        result = tg_truncate(text)
        assert len(result) == 4096
        assert result.endswith("...")

    def test_truncate_at_limit(self):
        text = "x" * 4096
        assert tg_truncate(text) == text


# ---------------------------------------------------------------------------
# slack.py
# ---------------------------------------------------------------------------


class TestSlackHTMLConversion:
    def test_bold(self):
        assert _html_to_slack("<b>hello</b>") == "*hello*"

    def test_code(self):
        assert _html_to_slack("<code>foo</code>") == "`foo`"

    def test_pre(self):
        assert _html_to_slack("<pre>bar</pre>") == "```bar```"

    def test_entities(self):
        assert _html_to_slack("&lt;tag&gt;") == "<tag>"
        assert _html_to_slack("&amp;") == "&"

    def test_combined(self):
        text = "<b>Status</b>: <code>.list</code>"
        result = _html_to_slack(text)
        assert result == "*Status*: `.list`"

    def test_truncate(self):
        text = "x" * 5000
        result = slack_truncate(text)
        assert len(result) == 4000
        assert result.endswith("...")


class TestSlackConnector:
    def test_platform_name(self):
        from onecmd.connectors.slack import SlackConnector
        c = SlackConnector(bot_token="xoxb-test", app_token="xapp-test")
        assert c.platform_name == "slack"


# ---------------------------------------------------------------------------
# handler.py — test command dispatch logic
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal backend mock for handler tests."""

    def __init__(self, terminals=None):
        self._terminals = terminals or []

    def list(self):
        return self._terminals

    def connected(self, term_id):
        return any(t.id == term_id for t in self._terminals)

    def capture(self, term_id):
        return "$ prompt"

    def send_keys(self, term_id, text, literal=True):
        return True

    def create(self):
        return "new-id"


class _FakeTermInfo:
    def __init__(self, id, name="term", title="", pid=1):
        self.id = id
        self.name = name
        self.title = title
        self.pid = pid


class _FakeStore:
    def __init__(self):
        self._data = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, expire=0):
        self._data[key] = value

    def delete(self, key):
        self._data.pop(key, None)

    def close(self):
        pass


class _FakeConnector(Connector):
    """In-memory connector for testing."""

    def __init__(self):
        self.sent_messages: list[tuple[str, str]] = []
        self.deleted_messages: list[tuple[str, str]] = []
        self._msg_counter = 0

    @property
    def platform_name(self):
        return "test"

    async def start(self, message_handler, callback_handler=None):
        pass

    async def stop(self):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self._msg_counter += 1
        msg_id = str(self._msg_counter)
        self.sent_messages.append((chat_id, text))
        return msg_id

    async def edit_message(self, chat_id, message_id, text, **kwargs):
        pass

    async def delete_message(self, chat_id, message_id):
        self.deleted_messages.append((chat_id, message_id))

    async def send_image(self, chat_id, image, caption=""):
        self._msg_counter += 1
        return str(self._msg_counter)


class TestConnectorHandler:
    """Test the unified handler with a fake connector."""

    def _make_handler(self, terminals=None, owner_id=12345):
        from pydantic import BaseModel, Field

        class FakeConfig(BaseModel, extra="forbid"):
            apikey: str = "test"
            dbfile: str = "test.sqlite"
            danger_mode: bool = False
            weak_security: bool = True
            enable_otp: bool = False
            verbose: bool = False
            visible_lines: int = 40
            split_messages: bool = False
            mgr_model: str | None = None
            otp_timeout: int = 300
            anthropic_api_key: str | None = None
            google_api_key: str | None = None
            admin_port: int | None = None
            admin_password: str | None = None
            slack_bot_token: str | None = None
            slack_app_token: str | None = None

            @property
            def has_llm_key(self):
                return False

            @property
            def has_slack(self):
                return False

        config = FakeConfig()
        store = _FakeStore()
        # Pre-register owner (matches auth/owner.py OWNER_KEY)
        store.set("owner_id", str(owner_id))
        backend = _FakeBackend(terminals or [])

        from onecmd.connectors.handler import create_connector_handler
        msg_h, cb_h, reg_fn = create_connector_handler(config, store, backend)
        return msg_h, cb_h, reg_fn, config, store, backend

    @pytest.mark.asyncio
    async def test_help_command(self):
        connector = _FakeConnector()
        msg_h, _, reg_fn, *_ = self._make_handler()
        reg_fn(connector)
        await msg_h(connector, "100", "12345", ".help", None)
        assert len(connector.sent_messages) == 1
        assert "Commands" in connector.sent_messages[0][1]

    @pytest.mark.asyncio
    async def test_list_command(self):
        terms = [_FakeTermInfo("t1", "Terminal 1")]
        connector = _FakeConnector()
        msg_h, _, reg_fn, *_ = self._make_handler(terminals=terms)
        reg_fn(connector)
        await msg_h(connector, "100", "12345", ".list", None)
        assert len(connector.sent_messages) == 1
        assert "Terminal 1" in connector.sent_messages[0][1]

    @pytest.mark.asyncio
    async def test_unknown_user_ignored(self):
        connector = _FakeConnector()
        msg_h, _, reg_fn, *_ = self._make_handler(owner_id=12345)
        reg_fn(connector)
        await msg_h(connector, "100", "99999", ".help", None)
        # First message from unknown user registers them as owner
        # (since owner_user_id is already set to 12345, 99999 is not owner)
        assert len(connector.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_health_command(self):
        connector = _FakeConnector()
        msg_h, _, reg_fn, *_ = self._make_handler()
        reg_fn(connector)
        await msg_h(connector, "100", "12345", ".health", None)
        assert len(connector.sent_messages) == 1
        assert "Health Report" in connector.sent_messages[0][1]
        assert "test" in connector.sent_messages[0][1]  # platform name

    @pytest.mark.asyncio
    async def test_exit_not_in_mgr_mode(self):
        connector = _FakeConnector()
        msg_h, _, reg_fn, *_ = self._make_handler()
        reg_fn(connector)
        await msg_h(connector, "100", "12345", ".exit", None)
        assert "Not in manager mode" in connector.sent_messages[0][1]
