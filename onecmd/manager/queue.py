"""Terminal command queue — per-terminal FIFO with stability detection.

Calling spec:
  Inputs:  terminal_id, keys, stable_seconds, on_complete callback
  Outputs: None (async, calls on_complete when done)
  Side effects: sends keys to terminal via Backend, polls for stability

Per-terminal FIFO queue (singleton per terminal_id).
One command at a time per terminal.
Stability detection: output unchanged for N seconds (default 5, max 300).
Auto-enter for pending prompts.  Baseline capture before sending.
Diff ratio check: <5% change -> "no change" error.
Reports last 30 lines of output.

Bounded by infra:
  - send_keys/capture go through ValidatedBackend (rate-limited, ID-validated)
  - MAX_STABILITY_WAIT: 300 seconds
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

DEFAULT_STABLE_SECONDS: float = 5.0
STABILITY_POLL: float = 0.5
MAX_STABILITY_WAIT: float = 300.0
OnComplete = Callable[[str], None]


class _Backend(Protocol):
    def capture(self, term_id: str) -> str | None: ...
    def send_keys(self, term_id: str, text: str) -> bool: ...


def _wait_stable(
    backend: _Backend, terminal_id: str,
    stable_seconds: float = DEFAULT_STABLE_SECONDS,
    cancel_event: threading.Event | None = None,
    max_wait: float = MAX_STABILITY_WAIT,
    baseline: str | None = None,
) -> str:
    """Wait until terminal output stops changing.  Returns final output.

    If *baseline* is provided, output must first differ from it before
    stability timing begins (prevents premature "stable" on laggy terminals).
    """
    start = time.time()
    prev_output: str | None = None
    last_change = start
    saw_change = baseline is None

    while True:
        if cancel_event and cancel_event.is_set():
            return backend.capture(terminal_id) or ""
        if time.time() - start > max_wait:
            return backend.capture(terminal_id) or ""
        output = backend.capture(terminal_id) or ""
        if not saw_change:
            if output != baseline:
                saw_change = True
                last_change = time.time()
                prev_output = output
        elif output != prev_output:
            last_change = time.time()
            prev_output = output
        elif time.time() - last_change >= stable_seconds:
            return output
        if cancel_event:
            cancel_event.wait(STABILITY_POLL)
        else:
            time.sleep(STABILITY_POLL)


def _output_diff_ratio(before: str, after: str) -> float:
    """Rough ratio of how much terminal output changed (0.0-1.0).
    Compares last 20 lines to ignore scrollback noise."""
    def tail(text: str, n: int = 20) -> list[str]:
        lines = text.strip().split("\n")
        return lines[-n:] if len(lines) >= n else lines

    bl, al = tail(before), tail(after)
    if bl == al:
        return 0.0
    mx = max(len(bl), len(al))
    if mx == 0:
        return 0.0
    matches = sum(1 for i in range(min(len(bl), len(al))) if bl[i] == al[i])
    return 1.0 - (matches / mx)


def _has_pending_command(baseline: str, output: str) -> bool:
    """Detect keys typed at a prompt but not submitted (no Enter)."""
    base_lines = baseline.rstrip("\n").split("\n")
    out_lines = output.rstrip("\n").split("\n")
    if not base_lines or not out_lines:
        return False
    if len(base_lines) == len(out_lines):
        bl, ol = base_lines[-1].rstrip(), out_lines[-1].rstrip()
        if len(ol) > len(bl) and ol.startswith(bl):
            return True
    return False


class TerminalQueue:
    """Per-terminal FIFO command queue.  One command at a time; each waits
    for the previous to finish (output stabilizes) before being sent."""

    _queues: dict[str, TerminalQueue] = {}
    _cls_lock = threading.Lock()

    @classmethod
    def get(cls, terminal_id: str, backend: _Backend) -> TerminalQueue:
        """Return the singleton queue for *terminal_id*, creating if needed."""
        with cls._cls_lock:
            if terminal_id not in cls._queues:
                cls._queues[terminal_id] = cls(terminal_id, backend)
            return cls._queues[terminal_id]

    def __init__(self, terminal_id: str, backend: _Backend) -> None:
        self.terminal_id = terminal_id
        self._backend = backend
        self._queue: list[tuple[str, str, float, OnComplete | None]] = []
        self._running = False
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()

    def enqueue(self, keys: str, description: str,
                stable_seconds: float = DEFAULT_STABLE_SECONDS,
                on_complete: OnComplete | None = None) -> None:
        """Add a command to the queue.  Starts execution if idle."""
        with self._lock:
            self._queue.append((keys, description, stable_seconds, on_complete))
            if not self._running:
                self._running = True
                threading.Thread(target=self._drain, daemon=True).start()

    def cancel_all(self) -> None:
        """Drop pending commands and interrupt the active one."""
        with self._lock:
            self._queue.clear()
            self._cancel_event.set()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    return
                keys, desc, stable_s, on_complete = self._queue.pop(0)
            self._cancel_event.clear()
            self._execute_one(keys, desc, stable_s, on_complete)

    def _execute_one(self, keys: str, description: str,
                     stable_seconds: float,
                     on_complete: OnComplete | None) -> None:
        started_at = time.time()
        try:
            baseline = self._backend.capture(self.terminal_id) or ""
            ok = self._backend.send_keys(self.terminal_id, keys)
            if not ok:
                msg = f"Error sending to {self.terminal_id}"
                logger.error(msg)
                if on_complete:
                    on_complete(msg)
                return

            output = _wait_stable(
                self._backend, self.terminal_id,
                stable_seconds=stable_seconds,
                cancel_event=self._cancel_event, baseline=baseline)
            if self._cancel_event.is_set():
                return

            # Auto-enter: pending command detected
            if _has_pending_command(baseline, output):
                logger.info("Auto-enter: pending command on %s", self.terminal_id)
                pre_enter = output
                self._backend.send_keys(self.terminal_id, "\n")
                if not self._cancel_event.is_set():
                    output = _wait_stable(
                        self._backend, self.terminal_id,
                        stable_seconds=stable_seconds,
                        cancel_event=self._cancel_event, baseline=pre_enter)
                if self._cancel_event.is_set():
                    return

            elapsed = int(time.time() - started_at)
            diff = _output_diff_ratio(baseline, output)
            if diff < 0.05:
                result = (f"({elapsed}s) {description}\n\n"
                          "Terminal output barely changed — the command "
                          "may not have been submitted.")
                if on_complete:
                    on_complete(result)
                return

            lines = output.strip().split("\n")
            tail = "\n".join(lines[-30:])
            result = f"Done ({elapsed}s): {description}\n\n{tail}"
            if on_complete:
                on_complete(result)
        except Exception as e:
            logger.error("Command execution error on %s: %s", self.terminal_id, e)
            if on_complete:
                on_complete(f"Command error: {e}")

