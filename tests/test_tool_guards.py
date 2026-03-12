"""Tests for the tool guard system (enforced preconditions at dispatch layer)."""

import threading
from unittest.mock import MagicMock

import pytest

from onecmd.manager import tools


@pytest.fixture(autouse=True)
def _clean_activity():
    """Reset activity tracker between tests."""
    with tools._activity_lock:
        tools._activity.clear()
    yield
    with tools._activity_lock:
        tools._activity.clear()


def _make_ctx():
    """Minimal ctx for dispatch."""
    backend = MagicMock()
    backend.list.return_value = []
    backend.capture.return_value = "$ "
    backend.send_keys.return_value = True
    return {
        "backend": backend,
        "queue_cls": MagicMock(),
        "tasks": {},
        "tasks_lock": threading.Lock(),
        "chat_id": 1,
        "notify": MagicMock(),
        "llm_client": MagicMock(),
        "llm_model": "test",
        "chat_fn": MagicMock(),
        "format_results_fn": MagicMock(),
        "next_task_id": MagicMock(return_value=1),
    }


# ---------------------------------------------------------------------------
# Guard enforcement
# ---------------------------------------------------------------------------


class TestSendCommandGuard:
    def test_blocked_without_read(self):
        ctx = _make_ctx()
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "list files",
        }, ctx)
        assert "BLOCKED" in result
        assert "read_terminal" in result

    def test_passes_after_read(self):
        # Simulate a read by tracking the terminal
        tools._track("%0", "$ some output")

        ctx = _make_ctx()
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "list files",
        }, ctx)
        assert "BLOCKED" not in result
        assert "queued" in result.lower() or "Command" in result

    def test_blocked_message_includes_terminal_id(self):
        ctx = _make_ctx()
        result = tools.dispatch("send_command", {
            "terminal_id": "%5",
            "keys": "test\n",
            "description": "test",
        }, ctx)
        assert "%5" in result


class TestBackgroundTaskGuard:
    def test_blocked_without_read(self):
        ctx = _make_ctx()
        result = tools.dispatch("start_background_task", {
            "terminal_id": "%0",
            "check_contains": "done",
            "description": "wait for build",
        }, ctx)
        assert "BLOCKED" in result

    def test_passes_after_read(self):
        tools._track("%0", "$ building...")
        ctx = _make_ctx()
        result = tools.dispatch("start_background_task", {
            "terminal_id": "%0",
            "check_contains": "done",
            "description": "wait for build",
        }, ctx)
        assert "BLOCKED" not in result


class TestSmartTaskGuard:
    def test_blocked_without_read(self):
        ctx = _make_ctx()
        result = tools.dispatch("start_smart_task", {
            "terminal_id": "%0",
            "goal": "run tests",
            "description": "test runner",
        }, ctx)
        assert "BLOCKED" in result


# ---------------------------------------------------------------------------
# Tools without guards work normally
# ---------------------------------------------------------------------------


class TestUnguardedTools:
    def test_list_terminals_no_guard(self):
        ctx = _make_ctx()
        result = tools.dispatch("list_terminals", {}, ctx)
        assert "BLOCKED" not in result

    def test_read_terminal_no_guard(self):
        ctx = _make_ctx()
        ctx["backend"].capture.return_value = "$ hello"
        result = tools.dispatch("read_terminal", {"terminal_id": "%0"}, ctx)
        assert "BLOCKED" not in result
        assert "hello" in result

    def test_unknown_tool(self):
        ctx = _make_ctx()
        result = tools.dispatch("nonexistent_tool", {}, ctx)
        assert "Unknown tool" in result


# ---------------------------------------------------------------------------
# Guard system internals
# ---------------------------------------------------------------------------


class TestCheckGuards:
    def test_no_guards_returns_none(self):
        assert tools._check_guards("list_terminals", {}, {}) is None

    def test_passing_guard_returns_none(self):
        tools._track("%0", "content")
        result = tools._check_guards("send_command",
                                     {"terminal_id": "%0"}, {})
        assert result is None

    def test_failing_guard_returns_message(self):
        result = tools._check_guards("send_command",
                                     {"terminal_id": "%9"}, {})
        assert result is not None
        assert "BLOCKED" in result
        assert "%9" in result

    def test_multiple_terminals_independent(self):
        # Read %0 but not %1
        tools._track("%0", "content")
        assert tools._check_guards(
            "send_command", {"terminal_id": "%0"}, {}) is None
        assert tools._check_guards(
            "send_command", {"terminal_id": "%1"}, {}) is not None


class TestActivityPollerSatisfiesGuard:
    """The background poller also calls _track(), so it satisfies the guard."""

    def test_poller_track_satisfies_read_guard(self):
        # Simulate what the poller does
        tools._track("%0", "$ output from poller")

        ctx = _make_ctx()
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "test",
        }, ctx)
        assert "BLOCKED" not in result
