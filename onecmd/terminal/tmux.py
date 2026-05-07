"""tmux terminal backend.

Calling spec:
  Inputs:  session_name (str | None) for scoping to a tmux session
  Outputs: TermInfo list, captured text, send/connected results
  Side effects: subprocess calls to tmux binary (shell=False, timeout=15s)

Sealed operations (deterministic):
  - list:      tmux list-panes [-s -t session] -F format_string
  - capture:   tmux capture-pane -t id -p  (trailing blanks stripped, max 64 KB)
  - send:      tmux send-keys -t id [-l] text  (-l for literal, without for special)
  - connected: tmux display-message -t id -p ""  (exit code check)
  - create:    tmux new-window [-t session] -P -F #{pane_id}  (returns new pane ID)

Guarding:
  - All args via subprocess.run([...], shell=False) — NEVER shell=True
  - Terminal IDs validated as tmux pane format (% + digits)
  - subprocess.run timeout=15s on all calls
  - Output length capped (capture returns max 64 KB)
"""

from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUBPROCESS_TIMEOUT = 15  # seconds
_MAX_CAPTURE_BYTES = 65_536  # 64 KB
_PANE_ID_RE = re.compile(r"^%\d+$")

# tmux list-panes format: pane_id \t pane_pid \t pane_current_command \t pane_title
_LIST_FORMAT = "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_title}"


from onecmd.terminal.backend import TermInfo  # noqa: E402

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_pane_id(term_id: str) -> None:
    """Raise ValueError if *term_id* is not a valid tmux pane id (% + digits)."""
    if not _PANE_ID_RE.match(term_id):
        raise ValueError(f"Invalid tmux pane ID: {term_id!r}")


# ---------------------------------------------------------------------------
# TmuxBackend
# ---------------------------------------------------------------------------


class TmuxBackend:
    """Backend that drives terminals via the ``tmux`` CLI.

    Parameters
    ----------
    session_name:
        If given, ``list()`` is scoped to panes within that tmux session.
        If ``None``, all panes across all sessions are returned.
    self_pane_id:
        The pane id this process is running in. Filtered out of ``list()``
        so the agent does not see (or attempt to drive) itself.
    danger_mode:
        If True, list panes across the entire tmux server (``-a``) and do
        not filter self. Mirrors the macOS backend's danger_mode.
    """

    def __init__(
        self,
        session_name: str | None = None,
        self_pane_id: str | None = None,
        danger_mode: bool = False,
    ) -> None:
        self._session_name = session_name
        self._self_pane_id = self_pane_id
        self._danger_mode = danger_mode
        self._panes: list[TermInfo] = []
        # Set on every list() call so diagnostic() reflects the latest state
        self._last_raw_count = 0

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list(self) -> list[TermInfo]:
        """Return tmux panes in scope, with the agent's own pane filtered out.

        Scope:
          - danger_mode=True       => all panes on the tmux server (``-a``)
          - session_name set       => panes within that session (``-s -t``)
          - otherwise              => all panes (``-a``)
        """
        cmd: list[str] = ["tmux", "list-panes"]

        if not self._danger_mode and self._session_name is not None:
            cmd += ["-s", "-t", self._session_name]
        else:
            cmd.append("-a")

        cmd += ["-F", _LIST_FORMAT]

        result = _run(cmd)
        if result is None:
            self._panes = []
            self._last_raw_count = 0
            return []

        panes: list[TermInfo] = []
        raw_count = 0
        for line in result.splitlines():
            if not line:
                continue
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            pane_id, pid_str, name, title = parts
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            raw_count += 1
            # Hide the agent's own pane unless danger_mode is set
            if not self._danger_mode and pane_id == self._self_pane_id:
                continue
            panes.append(TermInfo(id=pane_id, pid=pid, name=name, title=title))

        self._panes = panes
        self._last_raw_count = raw_count
        return list(panes)

    # ------------------------------------------------------------------
    # danger_mode runtime toggle
    # ------------------------------------------------------------------

    def set_danger_mode(self, enabled: bool) -> None:
        """Toggle danger_mode at runtime; resets cached state."""
        self._danger_mode = bool(enabled)
        self._panes = []
        self._last_raw_count = 0

    def is_danger_mode(self) -> bool:
        return self._danger_mode

    # ------------------------------------------------------------------
    # diagnostic
    # ------------------------------------------------------------------

    def diagnostic(self) -> str:
        """Human-readable explanation of why ``list()`` returned what it did.

        Intended for surfacing in UIs when the list is empty or unexpectedly
        short ("you only see one terminal because...").
        """
        parts: list[str] = []
        if self._danger_mode:
            parts.append("tmux scope: entire server (danger_mode)")
        elif self._session_name is not None:
            parts.append(f"tmux scope: session '{self._session_name}' only")
            parts.append("→ open more panes/windows in this session, or set "
                         "danger_mode=true to see other tmux sessions")
        else:
            parts.append("tmux scope: all sessions on this server")
        if self._self_pane_id and not self._danger_mode:
            parts.append(f"(own pane {self._self_pane_id} hidden)")
        if self._last_raw_count == 0:
            parts.append("tmux returned 0 panes — is tmux running?")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # connected
    # ------------------------------------------------------------------

    def connected(self, term_id: str) -> bool:
        """Return True if the pane *term_id* still exists."""
        _validate_pane_id(term_id)
        cmd = ["tmux", "display-message", "-t", term_id, "-p", ""]
        result = _run(cmd)
        return result is not None

    # ------------------------------------------------------------------
    # capture
    # ------------------------------------------------------------------

    def capture(self, term_id: str) -> str | None:
        """Capture the visible content of pane *term_id*.

        Trailing blank lines are stripped.  Output is capped at 64 KB.
        Returns ``None`` on failure or empty capture.
        """
        _validate_pane_id(term_id)
        cmd = ["tmux", "capture-pane", "-t", term_id, "-p"]
        result = _run(cmd, allow_nonzero=True)
        if result is None:
            return None

        # Cap output length
        if len(result) > _MAX_CAPTURE_BYTES:
            result = result[:_MAX_CAPTURE_BYTES]

        # Strip trailing blank lines and spaces (keep content)
        result = result.rstrip("\n ")

        return result if result else None

    # ------------------------------------------------------------------
    # send_keys
    # ------------------------------------------------------------------

    def send_keys(self, term_id: str, text: str, literal: bool = True) -> bool:
        """Send keystrokes to pane *term_id*.

        Parameters
        ----------
        text:
            The text to send.
        literal:
            If True, use ``-l`` flag (literal text, no tmux key-name parsing).
            If False, *text* is treated as a tmux key name (e.g. "Enter",
            "C-c", "Escape").
        """
        _validate_pane_id(term_id)
        cmd = ["tmux", "send-keys", "-t", term_id]
        if literal:
            cmd.append("-l")
        cmd.append(text)
        result = _run(cmd)
        return result is not None

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self) -> str | None:
        """Create a new tmux window and return the pane ID."""
        cmd = ["tmux", "new-window"]
        if self._session_name is not None:
            cmd += ["-t", self._session_name]
        cmd += ["-P", "-F", "#{pane_id}"]
        result = _run(cmd)
        if result is None:
            return None
        pane_id = result.strip()
        return pane_id if _PANE_ID_RE.match(pane_id) else None

    # ------------------------------------------------------------------
    # free_list
    # ------------------------------------------------------------------

    def free_list(self) -> None:
        """Clear the cached pane list."""
        self._panes = []


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    *,
    allow_nonzero: bool = False,
) -> str | None:
    """Run *cmd* and return stdout as a string, or ``None`` on failure.

    All calls use ``shell=False`` and ``timeout=15s``.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            shell=False,  # explicit: NEVER shell=True
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.warning("tmux command failed: %s — %s", cmd, exc)
        return None

    if proc.returncode != 0 and not allow_nonzero:
        log.debug("tmux returned %d for %s", proc.returncode, cmd)
        return None

    return proc.stdout
