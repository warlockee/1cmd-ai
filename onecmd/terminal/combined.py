"""Combined tmux + macOS backend.

Calling spec:
  Inputs:  TmuxBackend instance, MacOSBackend instance
  Outputs: union of both backends' TermInfo lists
  Side effects: delegates to inner backends

Routing:
  Terminal IDs do not collide between the two backends:
    - tmux pane IDs always start with '%' followed by digits (e.g. '%0', '%42')
    - macOS window IDs are pure decimal strings (e.g. '12345')
  Each operation routes by prefix:
    - id starts with '%'  -> TmuxBackend
    - id is all digits    -> MacOSBackend

list() returns the concatenation of tmux panes followed by macOS windows.
"""

from __future__ import annotations

from onecmd.terminal.backend import TermInfo


class CombinedBackend:
    """Backend that lists/drives both tmux panes and macOS terminal windows."""

    def __init__(self, tmux_backend, macos_backend) -> None:
        self._tmux = tmux_backend
        self._macos = macos_backend

    # ---- list -------------------------------------------------------------

    def list(self) -> list[TermInfo]:
        return list(self._tmux.list()) + list(self._macos.list())

    # ---- routing helpers --------------------------------------------------

    def _route(self, term_id: str):
        """Return the inner backend that owns *term_id*, or None."""
        if not term_id:
            return None
        if term_id.startswith("%") and term_id[1:].isdigit():
            return self._tmux
        if term_id.isdigit():
            return self._macos
        return None

    # ---- per-id operations ------------------------------------------------

    def connected(self, term_id: str) -> bool:
        b = self._route(term_id)
        return b.connected(term_id) if b is not None else False

    def capture(self, term_id: str) -> str | None:
        b = self._route(term_id)
        return b.capture(term_id) if b is not None else None

    def send_keys(self, term_id: str, text: str, literal: bool = True) -> bool:
        b = self._route(term_id)
        return b.send_keys(term_id, text, literal=literal) if b is not None else False

    # ---- create -----------------------------------------------------------

    def create(self) -> str | None:
        """Create a new terminal — prefer tmux (cheap, deterministic)."""
        new_id = self._tmux.create()
        if new_id is not None:
            return new_id
        return self._macos.create()

    # ---- free_list / danger_mode -----------------------------------------

    def free_list(self) -> None:
        self._tmux.free_list()
        self._macos.free_list()

    def set_danger_mode(self, enabled: bool) -> None:
        self._tmux.set_danger_mode(enabled)
        self._macos.set_danger_mode(enabled)

    def is_danger_mode(self) -> bool:
        return self._tmux.is_danger_mode() or self._macos.is_danger_mode()

    # ---- diagnostic -------------------------------------------------------

    def diagnostic(self) -> str:
        return f"{self._tmux.diagnostic()} | {self._macos.diagnostic()}"
