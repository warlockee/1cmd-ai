"""Tests for CombinedBackend — tmux + macOS union routing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from onecmd.terminal.backend import TermInfo
from onecmd.terminal.combined import CombinedBackend


def _term(id_: str, name: str = "x", title: str = "") -> TermInfo:
    return TermInfo(id=id_, pid=1, name=name, title=title)


@pytest.fixture
def tmux():
    m = MagicMock()
    m.list.return_value = [_term("%0", "bash"), _term("%1", "vim")]
    m.diagnostic.return_value = "tmux diag"
    m.is_danger_mode.return_value = False
    return m


@pytest.fixture
def macos():
    m = MagicMock()
    m.list.return_value = [_term("123", "Terminal"), _term("456", "iTerm2")]
    m.diagnostic.return_value = "macos diag"
    m.is_danger_mode.return_value = False
    return m


@pytest.fixture
def cb(tmux, macos):
    return CombinedBackend(tmux, macos)


class TestList:
    def test_list_concatenates_both(self, cb):
        ids = [t.id for t in cb.list()]
        assert ids == ["%0", "%1", "123", "456"]


class TestRouting:
    def test_connected_routes_tmux(self, cb, tmux, macos):
        cb.connected("%0")
        tmux.connected.assert_called_once_with("%0")
        macos.connected.assert_not_called()

    def test_connected_routes_macos(self, cb, tmux, macos):
        cb.connected("123")
        macos.connected.assert_called_once_with("123")
        tmux.connected.assert_not_called()

    def test_capture_routes_tmux(self, cb, tmux):
        tmux.capture.return_value = "out"
        assert cb.capture("%5") == "out"
        tmux.capture.assert_called_once_with("%5")

    def test_capture_routes_macos(self, cb, macos):
        macos.capture.return_value = "out"
        assert cb.capture("999") == "out"
        macos.capture.assert_called_once_with("999")

    def test_send_keys_routes_tmux(self, cb, tmux):
        tmux.send_keys.return_value = True
        assert cb.send_keys("%0", "hi", literal=True) is True
        tmux.send_keys.assert_called_once_with("%0", "hi", literal=True)

    def test_send_keys_routes_macos(self, cb, macos):
        macos.send_keys.return_value = True
        assert cb.send_keys("12", "hi") is True
        macos.send_keys.assert_called_once_with("12", "hi", literal=True)

    def test_unknown_id_returns_safe_default(self, cb, tmux, macos):
        assert cb.connected("garbage") is False
        assert cb.capture("garbage") is None
        assert cb.send_keys("garbage", "x") is False
        tmux.connected.assert_not_called()
        macos.connected.assert_not_called()

    def test_empty_id_returns_safe_default(self, cb):
        assert cb.connected("") is False


class TestCreate:
    def test_create_prefers_tmux(self, cb, tmux, macos):
        tmux.create.return_value = "%9"
        assert cb.create() == "%9"
        macos.create.assert_not_called()

    def test_create_falls_back_to_macos(self, cb, tmux, macos):
        tmux.create.return_value = None
        macos.create.return_value = "Terminal"
        assert cb.create() == "Terminal"


class TestDangerMode:
    def test_set_propagates_to_both(self, cb, tmux, macos):
        cb.set_danger_mode(True)
        tmux.set_danger_mode.assert_called_once_with(True)
        macos.set_danger_mode.assert_called_once_with(True)

    def test_is_true_if_either_is_true(self, cb, tmux, macos):
        tmux.is_danger_mode.return_value = True
        macos.is_danger_mode.return_value = False
        assert cb.is_danger_mode() is True


class TestDiagnostic:
    def test_diagnostic_combines_both(self, cb):
        d = cb.diagnostic()
        assert "tmux diag" in d
        assert "macos diag" in d


class TestFreeList:
    def test_free_list_propagates(self, cb, tmux, macos):
        cb.free_list()
        tmux.free_list.assert_called_once()
        macos.free_list.assert_called_once()
