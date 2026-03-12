"""Tests for pre-dispatch hooks (auto-fix preconditions before tool execution)."""

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


def _make_ctx(capture_output="$ "):
    """Minimal ctx for dispatch."""
    backend = MagicMock()
    backend.list.return_value = []
    backend.capture.return_value = capture_output
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
# Auto-read on send_command
# ---------------------------------------------------------------------------


class TestAutoReadOnSend:
    def test_auto_reads_terminal_before_send(self):
        ctx = _make_ctx(capture_output="$ hello world")
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "list files",
        }, ctx)
        # Should succeed (not blocked) and include auto-read content
        assert "auto-read" in result
        assert "hello world" in result
        assert "Command queued" in result or "queued" in result.lower()

    def test_no_auto_read_if_already_read(self):
        tools._track("%0", "$ already seen")
        ctx = _make_ctx()
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "list files",
        }, ctx)
        # No auto-read prefix since terminal was already read
        assert "auto-read" not in result
        assert "queued" in result.lower() or "Command" in result

    def test_auto_read_calls_backend_capture(self):
        ctx = _make_ctx(capture_output="claude> ready")
        tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "stop the server\n",
            "description": "stop server",
        }, ctx)
        ctx["backend"].capture.assert_called_with("%0")

    def test_auto_read_tracks_terminal(self):
        """After auto-read, terminal is tracked — no double-read next time."""
        ctx = _make_ctx(capture_output="$ prompt")
        tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "test",
        }, ctx)
        # Now terminal should be tracked
        with tools._activity_lock:
            assert "%0" in tools._activity

    def test_auto_read_content_visible_to_llm(self):
        """The LLM sees what's running so it can adjust its behavior."""
        ctx = _make_ctx(capture_output="╭─ Claude Code\n│ working...\n╰─")
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "stop the server\n",
            "description": "stop",
        }, ctx)
        assert "Claude Code" in result


# ---------------------------------------------------------------------------
# Auto-read on other guarded tools
# ---------------------------------------------------------------------------


class TestAutoReadOnOtherTools:
    def test_background_task_auto_reads(self):
        ctx = _make_ctx(capture_output="$ building...")
        result = tools.dispatch("start_background_task", {
            "terminal_id": "%0",
            "check_contains": "done",
            "description": "wait for build",
        }, ctx)
        assert "auto-read" in result

    def test_smart_task_auto_reads(self):
        ctx = _make_ctx(capture_output="$ running tests")
        result = tools.dispatch("start_smart_task", {
            "terminal_id": "%0",
            "goal": "run tests",
            "description": "test runner",
        }, ctx)
        assert "auto-read" in result


# ---------------------------------------------------------------------------
# Tools without hooks work normally
# ---------------------------------------------------------------------------


class TestUnhookedTools:
    def test_list_terminals_no_auto_read(self):
        ctx = _make_ctx()
        result = tools.dispatch("list_terminals", {}, ctx)
        assert "auto-read" not in result

    def test_read_terminal_no_auto_read(self):
        ctx = _make_ctx(capture_output="$ hello")
        result = tools.dispatch("read_terminal", {"terminal_id": "%0"}, ctx)
        assert "auto-read" not in result
        assert "hello" in result

    def test_unknown_tool(self):
        ctx = _make_ctx()
        result = tools.dispatch("nonexistent_tool", {}, ctx)
        assert "Unknown tool" in result


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_auto_read_handles_capture_failure(self):
        ctx = _make_ctx()
        ctx["backend"].capture.return_value = None
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "test",
        }, ctx)
        # Should still proceed (no auto-read content, but not blocked)
        assert "auto-read" not in result

    def test_poller_satisfies_hook(self):
        """Background poller calls _track(), so no auto-read needed."""
        tools._track("%0", "$ polled output")
        ctx = _make_ctx()
        result = tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": "ls\n",
            "description": "test",
        }, ctx)
        assert "auto-read" not in result
