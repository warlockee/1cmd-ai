"""Tests for onecmd.terminal.tmux — command construction and output parsing.

100% coverage on command construction.  subprocess.run is mocked throughout;
no real tmux process is ever started.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from onecmd.terminal.tmux import (
    _LIST_FORMAT,
    _MAX_CAPTURE_BYTES,
    _SUBPROCESS_TIMEOUT,
    TermInfo,
    TmuxBackend,
    _validate_pane_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    """Build a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


def _fail(returncode: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr="error"
    )


# ---------------------------------------------------------------------------
# _validate_pane_id
# ---------------------------------------------------------------------------


class TestValidatePaneId:
    def test_valid_ids(self) -> None:
        for pane_id in ("%0", "%1", "%42", "%99999"):
            _validate_pane_id(pane_id)  # should not raise

    def test_invalid_ids(self) -> None:
        for bad in ("", "0", "abc", "%", "% 1", "%abc", "1%2", "$0", "%1 "):
            with pytest.raises(ValueError, match="Invalid tmux pane ID"):
                _validate_pane_id(bad)


# ---------------------------------------------------------------------------
# list — command construction
# ---------------------------------------------------------------------------


class TestList:
    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_without_session(self, mock_run: MagicMock) -> None:
        """No session_name => tmux list-panes -a -F <format>."""
        mock_run.return_value = _ok(
            "%0\t12345\tbash\tmy title\n%1\t12346\tvim\teditor\n"
        )
        backend = TmuxBackend(session_name=None)
        panes = backend.list()

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "list-panes", "-a", "-F", _LIST_FORMAT]
        # Verify shell=False
        assert mock_run.call_args[1]["shell"] is False
        assert mock_run.call_args[1]["timeout"] == _SUBPROCESS_TIMEOUT

        assert len(panes) == 2
        assert panes[0] == TermInfo(id="%0", pid=12345, name="bash", title="my title")
        assert panes[1] == TermInfo(id="%1", pid=12346, name="vim", title="editor")

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_with_session(self, mock_run: MagicMock) -> None:
        """session_name given => tmux list-panes -s -t <session> -F <format>."""
        mock_run.return_value = _ok("%5\t100\tzsh\ttest\n")
        backend = TmuxBackend(session_name="work")
        panes = backend.list()

        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "list-panes", "-s", "-t", "work", "-F", _LIST_FORMAT]
        assert len(panes) == 1
        assert panes[0].id == "%5"

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_session_with_special_chars(self, mock_run: MagicMock) -> None:
        """Session name with special characters is passed as a list element (no shell)."""
        mock_run.return_value = _ok("")
        backend = TmuxBackend(session_name="my session")
        backend.list()

        cmd = mock_run.call_args[0][0]
        # The session name is a raw string in the list — shell=False handles it safely
        assert "-t" in cmd
        idx = cmd.index("-t")
        assert cmd[idx + 1] == "my session"

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_empty_output(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("")
        backend = TmuxBackend()
        assert backend.list() == []

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_subprocess_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _fail()
        backend = TmuxBackend()
        assert backend.list() == []

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_subprocess_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=15)
        backend = TmuxBackend()
        assert backend.list() == []

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_tmux_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("tmux")
        backend = TmuxBackend()
        assert backend.list() == []

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_malformed_lines_skipped(self, mock_run: MagicMock) -> None:
        """Lines with fewer than 4 tab-separated fields are skipped."""
        mock_run.return_value = _ok(
            "%0\t12345\tbash\tok\nbadline\n%1\t111\n\n%2\t200\tzsh\tgood\n"
        )
        backend = TmuxBackend()
        panes = backend.list()
        assert len(panes) == 2
        assert panes[0].id == "%0"
        assert panes[1].id == "%2"

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_invalid_pid_skipped(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("%0\tnotanumber\tbash\ttitle\n")
        backend = TmuxBackend()
        assert backend.list() == []

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_title_with_tabs(self, mock_run: MagicMock) -> None:
        """Title containing tabs: split with maxsplit=3 preserves them."""
        mock_run.return_value = _ok("%0\t999\tbash\ttitle\twith\ttabs\n")
        backend = TmuxBackend()
        panes = backend.list()
        assert len(panes) == 1
        assert panes[0].title == "title\twith\ttabs"


# ---------------------------------------------------------------------------
# connected — command construction
# ---------------------------------------------------------------------------


class TestConnected:
    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_connected_alive(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("")
        backend = TmuxBackend()
        assert backend.connected("%0") is True

        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "display-message", "-t", "%0", "-p", ""]
        assert mock_run.call_args[1]["shell"] is False

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_connected_dead(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _fail()
        backend = TmuxBackend()
        assert backend.connected("%0") is False

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_connected_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=15)
        backend = TmuxBackend()
        assert backend.connected("%0") is False

    def test_connected_invalid_id(self) -> None:
        backend = TmuxBackend()
        with pytest.raises(ValueError):
            backend.connected("bad_id")


# ---------------------------------------------------------------------------
# capture — command construction and output handling
# ---------------------------------------------------------------------------


class TestCapture:
    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_capture_basic(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("$ hello\n$ world\n\n\n")
        backend = TmuxBackend()
        text = backend.capture("%3")

        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "capture-pane", "-t", "%3", "-p"]
        assert mock_run.call_args[1]["shell"] is False
        assert text == "$ hello\n$ world"

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_capture_strips_trailing_blanks(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("content\n   \n\n \n")
        backend = TmuxBackend()
        assert backend.capture("%0") == "content"

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_capture_empty_returns_none(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("\n\n  \n")
        backend = TmuxBackend()
        assert backend.capture("%0") is None

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_capture_failure_returns_none(self, mock_run: MagicMock) -> None:
        # capture uses allow_nonzero=True, but _run returns None on exception
        mock_run.side_effect = FileNotFoundError("tmux")
        backend = TmuxBackend()
        assert backend.capture("%0") is None

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_capture_output_capped(self, mock_run: MagicMock) -> None:
        """Output exceeding 64 KB is truncated."""
        big = "x" * (_MAX_CAPTURE_BYTES + 1000)
        mock_run.return_value = _ok(big)
        backend = TmuxBackend()
        result = backend.capture("%0")
        assert result is not None
        assert len(result) <= _MAX_CAPTURE_BYTES

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_capture_allows_nonzero_exit(self, mock_run: MagicMock) -> None:
        """capture-pane may return content even on nonzero exit code."""
        mock_run.return_value = _ok("some output\n", returncode=1)
        backend = TmuxBackend()
        assert backend.capture("%0") == "some output"

    def test_capture_invalid_id(self) -> None:
        backend = TmuxBackend()
        with pytest.raises(ValueError):
            backend.capture("invalid")


# ---------------------------------------------------------------------------
# send_keys — command construction
# ---------------------------------------------------------------------------


class TestSendKeys:
    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_literal_text(self, mock_run: MagicMock) -> None:
        """Default: literal=True sends with -l flag."""
        mock_run.return_value = _ok()
        backend = TmuxBackend()
        result = backend.send_keys("%0", "ls -la")

        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "send-keys", "-t", "%0", "-l", "ls -la"]
        assert mock_run.call_args[1]["shell"] is False
        assert result is True

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_special_key(self, mock_run: MagicMock) -> None:
        """literal=False sends without -l (for key names like Enter, C-c)."""
        mock_run.return_value = _ok()
        backend = TmuxBackend()
        result = backend.send_keys("%0", "Enter", literal=False)

        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "send-keys", "-t", "%0", "Enter"]
        assert "-l" not in cmd
        assert result is True

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_ctrl_c(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok()
        backend = TmuxBackend()
        backend.send_keys("%5", "C-c", literal=False)

        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "send-keys", "-t", "%5", "C-c"]

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_escape(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok()
        backend = TmuxBackend()
        backend.send_keys("%0", "Escape", literal=False)

        cmd = mock_run.call_args[0][0]
        assert cmd == ["tmux", "send-keys", "-t", "%0", "Escape"]

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _fail()
        backend = TmuxBackend()
        assert backend.send_keys("%0", "text") is False

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=15)
        backend = TmuxBackend()
        assert backend.send_keys("%0", "text") is False

    def test_send_invalid_id(self) -> None:
        backend = TmuxBackend()
        with pytest.raises(ValueError):
            backend.send_keys("bad", "text")

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_text_with_special_chars(self, mock_run: MagicMock) -> None:
        """Text with quotes/spaces is safe because shell=False."""
        mock_run.return_value = _ok()
        backend = TmuxBackend()
        backend.send_keys("%0", "echo 'hello world' && rm -rf /")

        cmd = mock_run.call_args[0][0]
        assert cmd[-1] == "echo 'hello world' && rm -rf /"
        assert mock_run.call_args[1]["shell"] is False


# ---------------------------------------------------------------------------
# free_list
# ---------------------------------------------------------------------------


class TestFreeList:
    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_free_list_clears_cached_panes(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("%0\t100\tbash\ttitle\n")
        backend = TmuxBackend()
        panes = backend.list()
        assert len(panes) == 1

        backend.free_list()
        assert backend._panes == []


# ---------------------------------------------------------------------------
# Shell safety enforcement
# ---------------------------------------------------------------------------


class TestShellSafety:
    """Verify that every subprocess call uses shell=False."""

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_list_shell_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("")
        TmuxBackend().list()
        assert mock_run.call_args[1]["shell"] is False

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_connected_shell_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("")
        TmuxBackend().connected("%0")
        assert mock_run.call_args[1]["shell"] is False

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_capture_shell_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("x")
        TmuxBackend().capture("%0")
        assert mock_run.call_args[1]["shell"] is False

    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_send_keys_shell_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("")
        TmuxBackend().send_keys("%0", "text")
        assert mock_run.call_args[1]["shell"] is False


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------


class TestTimeoutEnforcement:
    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_all_calls_use_timeout(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok("")
        backend = TmuxBackend()

        backend.list()
        assert mock_run.call_args[1]["timeout"] == _SUBPROCESS_TIMEOUT

        backend.connected("%0")
        assert mock_run.call_args[1]["timeout"] == _SUBPROCESS_TIMEOUT

        mock_run.return_value = _ok("content")
        backend.capture("%0")
        assert mock_run.call_args[1]["timeout"] == _SUBPROCESS_TIMEOUT

        mock_run.return_value = _ok("")
        backend.send_keys("%0", "x")
        assert mock_run.call_args[1]["timeout"] == _SUBPROCESS_TIMEOUT


# ---------------------------------------------------------------------------
# TermInfo dataclass
# ---------------------------------------------------------------------------


class TestTermInfo:
    def test_frozen(self) -> None:
        t = TermInfo(id="%0", pid=100, name="bash", title="test")
        with pytest.raises(AttributeError):
            t.id = "%1"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = TermInfo(id="%0", pid=100, name="bash", title="test")
        b = TermInfo(id="%0", pid=100, name="bash", title="test")
        assert a == b

    def test_fields(self) -> None:
        t = TermInfo(id="%5", pid=999, name="zsh", title="hello")
        assert t.id == "%5"
        assert t.pid == 999
        assert t.name == "zsh"
        assert t.title == "hello"


# ---------------------------------------------------------------------------
# OSError handling
# ---------------------------------------------------------------------------


class TestOSError:
    @patch("onecmd.terminal.tmux.subprocess.run")
    def test_oserror_returns_none(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = OSError("permission denied")
        backend = TmuxBackend()
        assert backend.list() == []
        assert backend.connected("%0") is False
        assert backend.capture("%0") is None
        assert backend.send_keys("%0", "x") is False
