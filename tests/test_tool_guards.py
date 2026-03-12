"""Tests for pre-dispatch hooks (auto-fix preconditions before tool execution)."""

import threading
from unittest.mock import MagicMock

import pytest

from onecmd.manager import tools


@pytest.fixture(autouse=True)
def _clean_activity():
    """Reset activity tracker and poll state between tests."""
    with tools._activity_lock:
        tools._activity.clear()
    tools._poll_started = False
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
# Auto-read all terminals on list_terminals
# ---------------------------------------------------------------------------


def _make_terminal(tid, name="bash", title=""):
    t = MagicMock()
    t.id = tid
    t.name = name
    t.title = title
    return t


class TestListTerminalsAutoRead:
    def test_auto_reads_all_unread_terminals(self):
        t0 = _make_terminal("%0", "bash")
        t1 = _make_terminal("%1", "zsh")
        ctx = _make_ctx(capture_output="$ prompt")
        ctx["backend"].list.return_value = [t0, t1]
        result = tools.dispatch("list_terminals", {}, ctx)
        assert "auto-read 2 terminal(s)" in result
        assert "Content:" in result

    def test_skips_already_read_terminals(self):
        tools._track("%0", "$ already seen")
        t0 = _make_terminal("%0", "bash")
        t1 = _make_terminal("%1", "zsh")
        ctx = _make_ctx(capture_output="$ new prompt")
        ctx["backend"].list.return_value = [t0, t1]
        result = tools.dispatch("list_terminals", {}, ctx)
        # Only %1 should be auto-read (1, not 2)
        assert "auto-read 1 terminal(s)" in result

    def test_no_auto_read_when_all_tracked(self):
        tools._track("%0", "$ seen")
        tools._track("%1", "$ seen")
        t0 = _make_terminal("%0", "bash")
        t1 = _make_terminal("%1", "zsh")
        ctx = _make_ctx()
        ctx["backend"].list.return_value = [t0, t1]
        result = tools.dispatch("list_terminals", {}, ctx)
        assert "auto-read" not in result

    def test_preview_content_included(self):
        t0 = _make_terminal("%0", "bash")
        ctx = _make_ctx(capture_output="line1\nline2\n$ ready")
        ctx["backend"].list.return_value = [t0]
        result = tools.dispatch("list_terminals", {}, ctx)
        assert "ready" in result

    def test_capture_failure_skipped_gracefully(self):
        t0 = _make_terminal("%0", "bash")
        ctx = _make_ctx()
        ctx["backend"].list.return_value = [t0]
        ctx["backend"].capture.return_value = None
        result = tools.dispatch("list_terminals", {}, ctx)
        assert "auto-read" not in result
        assert "%0" in result  # terminal still listed

    def test_tracks_terminals_after_auto_read(self):
        t0 = _make_terminal("%0", "bash")
        ctx = _make_ctx(capture_output="$ hello")
        ctx["backend"].list.return_value = [t0]
        tools.dispatch("list_terminals", {}, ctx)
        with tools._activity_lock:
            assert "%0" in tools._activity


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


# ---------------------------------------------------------------------------
# Dangerous command detection
# ---------------------------------------------------------------------------


class TestDangerousCommandDetection:
    def _send(self, keys, description="test"):
        tools._track("%0", "$ ready")
        ctx = _make_ctx()
        return tools.dispatch("send_command", {
            "terminal_id": "%0",
            "keys": keys,
            "description": description,
        }, ctx)

    def test_blocks_rm_rf(self):
        result = self._send("rm -rf /tmp/stuff\n")
        assert "BLOCKED" in result

    def test_blocks_rm_f(self):
        result = self._send("rm -f important.txt\n")
        assert "BLOCKED" in result

    def test_blocks_rm_r(self):
        result = self._send("rm -r mydir/\n")
        assert "BLOCKED" in result

    def test_blocks_drop_table(self):
        result = self._send("DROP TABLE users;\n")
        assert "BLOCKED" in result

    def test_blocks_delete_from(self):
        result = self._send("DELETE FROM sessions;\n")
        assert "BLOCKED" in result

    def test_blocks_kill_9(self):
        result = self._send("kill -9 1234\n")
        assert "BLOCKED" in result

    def test_blocks_git_push_force(self):
        result = self._send("git push --force origin main\n")
        assert "BLOCKED" in result

    def test_blocks_git_push_f(self):
        result = self._send("git push -f origin main\n")
        assert "BLOCKED" in result

    def test_blocks_git_reset_hard(self):
        result = self._send("git reset --hard HEAD~3\n")
        assert "BLOCKED" in result

    def test_blocks_shutdown(self):
        result = self._send("shutdown -h now\n")
        assert "BLOCKED" in result

    def test_blocks_reboot(self):
        result = self._send("reboot\n")
        assert "BLOCKED" in result

    def test_blocks_killall(self):
        result = self._send("killall node\n")
        assert "BLOCKED" in result

    def test_blocks_systemctl_stop(self):
        result = self._send("systemctl stop nginx\n")
        assert "BLOCKED" in result

    def test_allows_safe_commands(self):
        result = self._send("ls -la\n")
        assert "BLOCKED" not in result
        assert "queued" in result.lower()

    def test_allows_npm_install(self):
        result = self._send("npm install\n")
        assert "BLOCKED" not in result

    def test_allows_git_push(self):
        result = self._send("git push origin main\n")
        assert "BLOCKED" not in result

    def test_allows_git_status(self):
        result = self._send("git status\n")
        assert "BLOCKED" not in result

    def test_blocked_includes_command(self):
        result = self._send("rm -rf /\n", "clean everything")
        assert "rm -rf /" in result
        assert "clean everything" in result

    def test_blocks_truncate(self):
        result = self._send("TRUNCATE TABLE logs;\n")
        assert "BLOCKED" in result

    def test_blocks_git_clean_f(self):
        result = self._send("git clean -fd\n")
        assert "BLOCKED" in result
