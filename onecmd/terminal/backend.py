"""Backend interface, registry, and validated wrapper.

Calling spec:
  Inputs:  Scope (from scope.py), danger_mode flag
  Outputs: ValidatedBackend wrapping the real backend
  Side effects: lazy import of backend modules via importlib

Interface (Protocol):
  list()                    -> list[TermInfo]
  connected(term_id: str)   -> bool
  capture(term_id: str)     -> str | None
  send_keys(term_id: str, text: str, literal: bool = True) -> bool
  free_list()               -> None

Guarding (ValidatedBackend wrapper):
  - send_keys / capture / connected reject IDs not in last list() result
  - send_keys rate-limited to 10/sec per terminal
  - send_keys rejects text > 10000 chars
"""

from __future__ import annotations

import importlib
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from onecmd.terminal.scope import Scope


# ---------------------------------------------------------------------------
# Shared data type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TermInfo:
    """Immutable snapshot of a single terminal (tmux pane or macOS window)."""

    id: str  # e.g. "%0" (tmux) or window-ID string (macOS)
    pid: int  # child process PID (tmux) or owning process PID (macOS)
    name: str  # pane_current_command (tmux) or app name (macOS)
    title: str  # pane/window title

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    def list(self) -> list[TermInfo]: ...
    def connected(self, term_id: str) -> bool: ...
    def capture(self, term_id: str) -> str | None: ...
    def send_keys(self, term_id: str, text: str, literal: bool = True) -> bool: ...
    def free_list(self) -> None: ...


# ---------------------------------------------------------------------------
# Registry (lazy import via importlib)
# ---------------------------------------------------------------------------

BACKENDS: dict[str, str] = {
    "tmux": "onecmd.terminal.tmux.TmuxBackend",
}
if sys.platform == "darwin":
    BACKENDS["macos"] = "onecmd.terminal.macos.MacOSBackend"

_MAX_SEND_PER_SEC = 10
_MAX_TEXT_LEN = 10_000


# ---------------------------------------------------------------------------
# ValidatedBackend
# ---------------------------------------------------------------------------


class ValidatedBackend:
    """Infra guard: validates all inputs before passing to real backend."""

    def __init__(self, inner: Backend) -> None:
        self._inner = inner
        self._known_ids: set[str] = set()
        self._send_timestamps: dict[str, list[float]] = {}

    def list(self) -> list[TermInfo]:
        result = self._inner.list()
        self._known_ids = {t.id for t in result}
        return result

    def connected(self, term_id: str) -> bool:
        self._validate_id(term_id)
        return self._inner.connected(term_id)

    def capture(self, term_id: str) -> str | None:
        self._validate_id(term_id)
        return self._inner.capture(term_id)

    def send_keys(self, term_id: str, text: str, literal: bool = True) -> bool:
        self._validate_id(term_id)
        self._validate_text(text)
        self._check_rate_limit(term_id)
        return self._inner.send_keys(term_id, text, literal=literal)

    def free_list(self) -> None:
        self._inner.free_list()

    # -- internal --

    def _validate_id(self, term_id: str) -> None:
        if term_id not in self._known_ids:
            raise ValueError(f"Unknown terminal ID: {term_id}")

    def _check_rate_limit(self, term_id: str) -> None:
        now = time.time()
        stamps = self._send_timestamps.setdefault(term_id, [])
        stamps = [t for t in stamps if now - t < 1.0]
        if len(stamps) >= _MAX_SEND_PER_SEC:
            raise RuntimeError(f"Rate limit: >10 sends/sec to {term_id}")
        stamps.append(now)
        self._send_timestamps[term_id] = stamps

    def _validate_text(self, text: str) -> None:
        if len(text) > _MAX_TEXT_LEN:
            raise ValueError(f"Text too long: {len(text)} chars (max {_MAX_TEXT_LEN})")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_backend(scope: Scope, danger_mode: bool = False) -> ValidatedBackend:
    """Create a scoped, validated backend based on the detected scope."""
    key = "tmux" if scope.use_tmux else "macos"
    fqn = BACKENDS[key]
    mod_path, cls_name = fqn.rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)

    if key == "tmux":
        inner = cls(session_name=scope.session_name)
    else:
        inner = cls(parent_pid=scope.parent_pid, danger_mode=danger_mode)

    return ValidatedBackend(inner)
