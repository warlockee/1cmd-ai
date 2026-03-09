"""Tests for onecmd.terminal.scope — detection logic, frozen dataclass."""

from __future__ import annotations

import ctypes
import subprocess
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest

from onecmd.terminal.scope import (
    KNOWN_TERMINALS,
    MAX_ANCESTORS,
    Scope,
    _detect_parent_terminal,
    _detect_tmux_session,
    detect_scope,
)


# ── Frozen Dataclass ────────────────────────────────────────────────


class TestScopeFrozen:
    def test_cannot_modify_use_tmux(self):
        s = Scope(use_tmux=True, session_name="main")
        with pytest.raises(FrozenInstanceError):
            s.use_tmux = False  # type: ignore[misc]

    def test_cannot_modify_session_name(self):
        s = Scope(use_tmux=True, session_name="main")
        with pytest.raises(FrozenInstanceError):
            s.session_name = "other"  # type: ignore[misc]

    def test_cannot_modify_parent_pid(self):
        s = Scope(use_tmux=False, parent_pid=1234)
        with pytest.raises(FrozenInstanceError):
            s.parent_pid = 9999  # type: ignore[misc]

    def test_defaults(self):
        s = Scope(use_tmux=False)
        assert s.session_name is None
        assert s.parent_pid is None


# ── tmux Detection ──────────────────────────────────────────────────


class TestDetectTmuxSession:
    def test_session_found(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="my-session\n", stderr=""
        )
        with patch("onecmd.terminal.scope.subprocess.run", return_value=result):
            assert _detect_tmux_session() == "my-session"

    def test_no_tmux_installed(self):
        with patch(
            "onecmd.terminal.scope.subprocess.run", side_effect=FileNotFoundError
        ):
            assert _detect_tmux_session() is None

    def test_tmux_not_running(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no server running"
        )
        with patch("onecmd.terminal.scope.subprocess.run", return_value=result):
            assert _detect_tmux_session() is None

    def test_tmux_timeout(self):
        with patch(
            "onecmd.terminal.scope.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5),
        ):
            assert _detect_tmux_session() is None

    def test_empty_output(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="  \n", stderr=""
        )
        with patch("onecmd.terminal.scope.subprocess.run", return_value=result):
            assert _detect_tmux_session() is None


# ── macOS PID Walk ──────────────────────────────────────────────────


class TestDetectParentTerminal:
    def test_not_darwin(self):
        with patch("onecmd.terminal.scope.sys.platform", "linux"):
            assert _detect_parent_terminal() is None

    @patch("onecmd.terminal.scope.sys.platform", "darwin")
    @patch("onecmd.terminal.scope.os.getpid", return_value=100)
    def test_finds_terminal_pid(self, _mock_pid):
        """Simulate: PID 100 -> ppid 50 (iTerm2) -> ppid 1. Returns 50."""
        call_count = {"n": 0}

        def fake_sysctl(mib, _count, buf, buf_size_p, _a, _b):
            pid = mib[3]
            # Write ppid at offset 560
            if pid == 100:
                ppid = 50
            elif pid == 50:
                ppid = 1
            else:
                return -1
            ctypes.memmove(
                ctypes.addressof(buf) + 560,
                ctypes.byref(ctypes.c_int(ppid)),
                4,
            )
            call_count["n"] += 1
            return 0

        def fake_proc_name(pid, buf, size):
            if pid == 100:
                name = b"zsh"
            elif pid == 50:
                name = b"iTerm2"
            else:
                name = b"launchd"
            ctypes.memmove(buf, name, len(name))
            return len(name)

        mock_libc = MagicMock()
        mock_libc.sysctl = fake_sysctl

        mock_libproc = MagicMock()
        mock_libproc.proc_name = fake_proc_name

        with patch("onecmd.terminal.scope.ctypes.CDLL", side_effect=[mock_libc, mock_libproc]):
            result = _detect_parent_terminal()
        assert result == 50

    @patch("onecmd.terminal.scope.sys.platform", "darwin")
    @patch("onecmd.terminal.scope.os.getpid", return_value=100)
    def test_no_terminal_found(self, _mock_pid):
        """All ancestors are non-terminal processes."""

        def fake_sysctl(mib, _count, buf, buf_size_p, _a, _b):
            # ppid=1 immediately
            ctypes.memmove(
                ctypes.addressof(buf) + 560,
                ctypes.byref(ctypes.c_int(1)),
                4,
            )
            return 0

        def fake_proc_name(pid, buf, size):
            name = b"bash"
            ctypes.memmove(buf, name, len(name))
            return len(name)

        mock_libc = MagicMock()
        mock_libc.sysctl = fake_sysctl
        mock_libproc = MagicMock()
        mock_libproc.proc_name = fake_proc_name

        with patch("onecmd.terminal.scope.ctypes.CDLL", side_effect=[mock_libc, mock_libproc]):
            assert _detect_parent_terminal() is None

    @patch("onecmd.terminal.scope.sys.platform", "darwin")
    @patch("onecmd.terminal.scope.os.getpid", return_value=100)
    def test_topmost_match_returned(self, _mock_pid):
        """Two terminals in chain: PID 80 (kitty) and PID 50 (Terminal).
        Should return 50 (topmost)."""
        tree = {
            100: (70, b"zsh"),
            70: (80, b"python"),
            80: (50, b"kitty"),
            50: (1, b"Terminal"),
        }

        def fake_sysctl(mib, _count, buf, buf_size_p, _a, _b):
            pid = mib[3]
            if pid not in tree:
                return -1
            ppid, _ = tree[pid]
            ctypes.memmove(
                ctypes.addressof(buf) + 560,
                ctypes.byref(ctypes.c_int(ppid)),
                4,
            )
            return 0

        def fake_proc_name(pid, buf, size):
            if pid in tree:
                _, name = tree[pid]
            else:
                name = b"unknown"
            ctypes.memmove(buf, name, len(name))
            return len(name)

        mock_libc = MagicMock()
        mock_libc.sysctl = fake_sysctl
        mock_libproc = MagicMock()
        mock_libproc.proc_name = fake_proc_name

        with patch("onecmd.terminal.scope.ctypes.CDLL", side_effect=[mock_libc, mock_libproc]):
            result = _detect_parent_terminal()
        assert result == 50

    @patch("onecmd.terminal.scope.sys.platform", "darwin")
    @patch("onecmd.terminal.scope.os.getpid", return_value=100)
    def test_deep_tree_limited(self, _mock_pid):
        """Tree deeper than MAX_ANCESTORS — should stop after limit."""
        call_count = {"n": 0}

        def fake_sysctl(mib, _count, buf, buf_size_p, _a, _b):
            call_count["n"] += 1
            # Always return ppid = current - 1 (never reaches 1)
            pid = mib[3]
            ppid = pid - 1 if pid > 2 else 1
            ctypes.memmove(
                ctypes.addressof(buf) + 560,
                ctypes.byref(ctypes.c_int(ppid)),
                4,
            )
            return 0

        def fake_proc_name(pid, buf, size):
            name = b"bash"
            ctypes.memmove(buf, name, len(name))
            return len(name)

        mock_libc = MagicMock()
        mock_libc.sysctl = fake_sysctl
        mock_libproc = MagicMock()
        mock_libproc.proc_name = fake_proc_name

        with patch("onecmd.terminal.scope.ctypes.CDLL", side_effect=[mock_libc, mock_libproc]):
            _detect_parent_terminal()
        assert call_count["n"] == MAX_ANCESTORS

    @patch("onecmd.terminal.scope.sys.platform", "darwin")
    @patch("onecmd.terminal.scope.os.getpid", return_value=100)
    def test_sysctl_error_stops_walk(self, _mock_pid):
        mock_libc = MagicMock()
        mock_libc.sysctl = MagicMock(return_value=-1)
        mock_libproc = MagicMock()

        with patch("onecmd.terminal.scope.ctypes.CDLL", side_effect=[mock_libc, mock_libproc]):
            assert _detect_parent_terminal() is None


# ── detect_scope Integration ────────────────────────────────────────


class TestDetectScope:
    def test_tmux_found(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="work\n", stderr=""
        )
        with patch("onecmd.terminal.scope.subprocess.run", return_value=result):
            scope = detect_scope()
        assert scope.use_tmux is True
        assert scope.session_name == "work"
        assert scope.parent_pid is None

    def test_no_tmux_fallback_to_macos(self):
        with (
            patch("onecmd.terminal.scope._detect_tmux_session", return_value=None),
            patch(
                "onecmd.terminal.scope._detect_parent_terminal", return_value=42
            ),
        ):
            scope = detect_scope()
        assert scope.use_tmux is False
        assert scope.session_name is None
        assert scope.parent_pid == 42

    def test_no_tmux_no_terminal(self):
        with (
            patch("onecmd.terminal.scope._detect_tmux_session", return_value=None),
            patch(
                "onecmd.terminal.scope._detect_parent_terminal", return_value=None
            ),
        ):
            scope = detect_scope()
        assert scope.use_tmux is False
        assert scope.session_name is None
        assert scope.parent_pid is None
