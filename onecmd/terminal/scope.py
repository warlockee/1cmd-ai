"""Scope detection — determine tmux session or parent terminal PID.

Calling spec:
  Inputs:  None (runs subprocess, walks process tree via ctypes on macOS)
  Outputs: Scope(use_tmux: bool, session_name: str | None, parent_pid: int | None)
  Side effects: subprocess call to tmux display-message; sysctl on macOS
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

KNOWN_TERMINALS = [
    "Terminal", "iTerm2", "iTerm", "Ghostty", "kitty",
    "Alacritty", "Hyper", "Warp", "WezTerm", "Tabby",
]

MAX_ANCESTORS = 32


@dataclass(frozen=True)
class Scope:
    use_tmux: bool
    session_name: str | None = None
    parent_pid: int | None = None


def _detect_tmux_session() -> str | None:
    """Run tmux display-message to get the current session name."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        name = result.stdout.strip()
        return name if result.returncode == 0 and name else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _detect_parent_terminal() -> int | None:
    """Walk process tree on macOS via sysctl; return topmost terminal PID."""
    if sys.platform != "darwin":
        return None

    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libproc = ctypes.CDLL(ctypes.util.find_library("proc"))

    pid = os.getpid()
    found: int | None = None

    for _ in range(MAX_ANCESTORS):
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buf = ctypes.create_string_buffer(648)  # sizeof(kinfo_proc)
        buf_size = ctypes.c_size_t(648)
        if libc.sysctl(mib, 4, buf, ctypes.byref(buf_size), None, 0) != 0:
            break

        # Get process name via proc_name
        name_buf = ctypes.create_string_buffer(256)
        libproc.proc_name(pid, name_buf, 256)
        name = name_buf.value.decode("utf-8", errors="replace")

        for term in KNOWN_TERMINALS:
            if term.lower() in name.lower():
                found = pid  # Keep walking — use topmost match
                break

        # Extract parent PID: kp_eproc.e_ppid at offset 560
        ppid = ctypes.c_int.from_buffer_copy(buf, 560).value
        if ppid <= 1:
            break
        pid = ppid

    return found


def detect_scope() -> Scope:
    """Detect the terminal scope at startup. Returns a frozen Scope."""
    session = _detect_tmux_session()
    if session:
        log.info("Scope: tmux session '%s'", session)
        return Scope(use_tmux=True, session_name=session)

    parent = _detect_parent_terminal()
    if parent:
        log.info("Scope: macOS terminal PID %d", parent)
    else:
        log.info("Scope: no tmux session or terminal PID detected")
    return Scope(use_tmux=False, parent_pid=parent)
