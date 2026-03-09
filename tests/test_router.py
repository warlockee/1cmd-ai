"""Tests for manager/router.py — ManagerRouter toggle and message routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from onecmd.manager.router import ManagerRouter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _config_with_key():
    cfg = MagicMock()
    cfg.has_llm_key = True
    return cfg


@pytest.fixture()
def _config_no_key():
    cfg = MagicMock()
    cfg.has_llm_key = False
    return cfg


@pytest.fixture()
def _backend():
    return MagicMock()


@pytest.fixture()
def _notify():
    return MagicMock()


# ---------------------------------------------------------------------------
# activate / deactivate
# ---------------------------------------------------------------------------


class TestActivateDeactivate:
    def test_activate_returns_status_message(self, _backend, _config_with_key, _notify):
        with patch("onecmd.manager.agent.Agent") as MockAgent:
            MockAgent.return_value = MagicMock()
            router = ManagerRouter(_backend, _config_with_key, _notify)
            msg = router.activate()
            assert "Manager mode ON" in msg

    def test_activate_without_llm_key_returns_unavailable(
        self, _backend, _config_no_key, _notify
    ):
        router = ManagerRouter(_backend, _config_no_key, _notify)
        msg = router.activate()
        assert "unavailable" in msg.lower()

    def test_deactivate_returns_status_message(self, _backend, _config_with_key, _notify):
        router = ManagerRouter(_backend, _config_with_key, _notify)
        msg = router.deactivate()
        assert "OFF" in msg

    def test_active_property_tracks_state(self, _backend, _config_with_key, _notify):
        with patch("onecmd.manager.agent.Agent") as MockAgent:
            MockAgent.return_value = MagicMock()
            router = ManagerRouter(_backend, _config_with_key, _notify)
            assert router.active is False
            router.activate()
            assert router.active is True
            router.deactivate()
            assert router.active is False


# ---------------------------------------------------------------------------
# handle
# ---------------------------------------------------------------------------


class TestHandle:
    def test_handle_returns_none_when_not_active(
        self, _backend, _config_with_key, _notify
    ):
        router = ManagerRouter(_backend, _config_with_key, _notify)
        result = router.handle(1, "hello")
        assert result is None

    def test_handle_routes_to_agent_when_active(
        self, _backend, _config_with_key, _notify
    ):
        with patch("onecmd.manager.agent.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.handle_message.return_value = "agent reply"
            MockAgent.return_value = mock_agent

            router = ManagerRouter(_backend, _config_with_key, _notify)
            router.activate()
            result = router.handle(42, "do something")

            assert result == "agent reply"
            mock_agent.handle_message.assert_called_once_with(42, "do something")

    def test_handle_returns_none_for_empty_text(
        self, _backend, _config_with_key, _notify
    ):
        with patch("onecmd.manager.agent.Agent") as MockAgent:
            MockAgent.return_value = MagicMock()
            router = ManagerRouter(_backend, _config_with_key, _notify)
            router.activate()
            assert router.handle(1, "") is None
            assert router.handle(1, "   ") is None

    def test_handle_returns_error_on_agent_exception(
        self, _backend, _config_with_key, _notify
    ):
        with patch("onecmd.manager.agent.Agent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.handle_message.side_effect = RuntimeError("boom")
            MockAgent.return_value = mock_agent

            router = ManagerRouter(_backend, _config_with_key, _notify)
            router.activate()
            result = router.handle(1, "test")
            assert "Error" in result
