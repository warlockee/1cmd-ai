"""
onecmd mgr — Terminal command queue and output stability detection.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from utils import send_response, DEFAULT_STABLE_SECONDS, STABILITY_POLL, MAX_STABILITY_WAIT
from tools import capture_terminal, send_keys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output stability detection
# ---------------------------------------------------------------------------

def _wait_stable(terminal_id: str, stable_seconds: float = DEFAULT_STABLE_SECONDS,
                 cancel_event: threading.Event | None = None,
                 max_wait: float = MAX_STABILITY_WAIT,
                 baseline: str | None = None) -> str:
    """
    Wait until terminal output stops changing. Returns the final output.
    Captures output repeatedly; when two consecutive captures are identical
    and stable_seconds have passed since the last change, returns.

    If baseline is provided, output must first differ from it before
    stability timing begins. This prevents premature "stable" detection
    on laggy/remote terminals where the command hasn't arrived yet.
    """
    start: float = time.time()
    prev_output: str | None = None
    last_change: float = time.time()
    saw_change: bool = baseline is None  # if no baseline, any state counts

    while True:
        if cancel_event and cancel_event.is_set():
            return capture_terminal(terminal_id)
        if time.time() - start > max_wait:
            return capture_terminal(terminal_id)

        output: str = capture_terminal(terminal_id)

        # If we have a baseline, wait until output actually differs from it
        if not saw_change:
            if output != baseline:
                saw_change = True
                last_change = time.time()
                prev_output = output
            # Still matches baseline — keep waiting, don't start stability timer
        elif output != prev_output:
            last_change = time.time()
            prev_output = output
        elif time.time() - last_change >= stable_seconds:
            # Output has been the same for stable_seconds
            return output

        # Wait a bit before next capture
        if cancel_event:
            cancel_event.wait(STABILITY_POLL)
        else:
            time.sleep(STABILITY_POLL)


def _output_diff_ratio(before: str, after: str) -> float:
    """
    Return a rough ratio of how much the terminal output changed.
    0.0 = identical, 1.0 = completely different.
    Compares the last N lines to ignore scrollback noise.
    """
    def tail(text: str, n: int = 20) -> list[str]:
        lines = text.strip().split('\n')
        return lines[-n:] if len(lines) >= n else lines

    before_lines: list[str] = tail(before)
    after_lines: list[str] = tail(after)

    if before_lines == after_lines:
        return 0.0

    # Count lines that differ
    max_len: int = max(len(before_lines), len(after_lines))
    if max_len == 0:
        return 0.0

    matches: int = 0
    for i in range(min(len(before_lines), len(after_lines))):
        if before_lines[i] == after_lines[i]:
            matches += 1

    return 1.0 - (matches / max_len)


def _has_pending_command(baseline: str, output: str) -> bool:
    """Detect if keys were typed at a prompt but not submitted (no Enter).

    Compares the last line of baseline vs output. If the only change is
    that text was appended to the same last line, the command was typed
    but not executed — needs an Enter.
    """
    base_lines = baseline.rstrip('\n').split('\n')
    out_lines = output.rstrip('\n').split('\n')
    if not base_lines or not out_lines:
        return False
    # Same number of lines, and last line got longer — text was typed, not submitted
    if len(base_lines) == len(out_lines):
        base_last = base_lines[-1].rstrip()
        out_last = out_lines[-1].rstrip()
        if len(out_last) > len(base_last) and out_last.startswith(base_last):
            return True
    return False


class TerminalQueue:
    """
    Per-terminal command queue. Ensures commands are sent one at a time —
    each command waits for the previous one to finish (output stabilizes)
    before being sent. Prevents flooding the terminal with overlapping commands.
    """
    _queues: dict[str, TerminalQueue] = {}   # terminal_id -> TerminalQueue
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get(cls, terminal_id: str) -> TerminalQueue:
        with cls._lock:
            if terminal_id not in cls._queues:
                cls._queues[terminal_id] = cls(terminal_id)
            return cls._queues[terminal_id]

    def __init__(self, terminal_id: str) -> None:
        self.terminal_id: str = terminal_id
        self._queue: list[tuple[str, str, float, int, int, dict[int, Any], threading.Lock]] = []
        self._running: bool = False
        self._lock: threading.Lock = threading.Lock()
        self._cancel_event: threading.Event = threading.Event()

    def enqueue(self, keys: str, description: str, stable_seconds: float,
                chat_id: int, task_id: int,
                tasks_dict: dict[int, Any], global_lock: threading.Lock,
                on_complete: Any = None) -> None:
        """Add a command to the queue. Starts execution if idle.
        on_complete(chat_id, result_text) is called when the command finishes,
        allowing the manager LLM to process the result."""
        with self._lock:
            self._queue.append((keys, description, stable_seconds,
                                chat_id, task_id, tasks_dict, global_lock,
                                on_complete))
            if not self._running:
                self._running = True
                threading.Thread(target=self._drain, daemon=True).start()

    def cancel_all(self) -> None:
        with self._lock:
            self._queue.clear()
            self._cancel_event.set()

    def _drain(self) -> None:
        """Process queued commands one at a time."""
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    return
                (keys, description, stable_seconds,
                 chat_id, task_id, tasks_dict, global_lock,
                 on_complete) = self._queue.pop(0)

            self._cancel_event.clear()
            self._execute_one(keys, description, stable_seconds,
                              chat_id, task_id, tasks_dict, global_lock,
                              on_complete)

    def _execute_one(self, keys: str, description: str, stable_seconds: float,
                     chat_id: int, task_id: int,
                     tasks_dict: dict[int, Any], global_lock: threading.Lock,
                     on_complete: Any = None) -> None:
        """Send one command, wait for stability, notify."""
        started_at: float = time.time()
        try:
            # Capture baseline BEFORE sending
            baseline: str = capture_terminal(self.terminal_id)

            # Send keys
            send_result: str = send_keys(self.terminal_id, keys)
            if "Error" in send_result:
                send_response(chat_id,
                    f"\U0001f916 Error sending #{task_id}: {send_result}")
                self._update_task(tasks_dict, global_lock, task_id, "error")
                return

            # Wait for output to stabilize (baseline ensures we wait for
            # the terminal to actually react before starting stability timer)
            output: str = _wait_stable(
                self.terminal_id,
                stable_seconds=stable_seconds,
                cancel_event=self._cancel_event,
                max_wait=MAX_STABILITY_WAIT,
                baseline=baseline,
            )

            if self._cancel_event.is_set():
                return

            # Auto-enter: if keys were typed at a prompt but not submitted
            # (last line grew but line count didn't change), press Enter.
            if _has_pending_command(baseline, output):
                logger.info("Auto-enter: pending command detected on %s, sending newline",
                            self.terminal_id)
                pre_enter: str = output
                send_keys(self.terminal_id, "\n")
                if not self._cancel_event.is_set():
                    output = _wait_stable(
                        self.terminal_id,
                        stable_seconds=stable_seconds,
                        cancel_event=self._cancel_event,
                        max_wait=MAX_STABILITY_WAIT,
                        baseline=pre_enter,
                    )
                if self._cancel_event.is_set():
                    return

            elapsed: int = int(time.time() - started_at)

            # Check if command actually executed
            diff: float = _output_diff_ratio(baseline, output)
            if diff < 0.05:
                self._update_task(tasks_dict, global_lock, task_id, "no_change")
                result_msg: str = (f"({elapsed}s) {description}\n\n"
                    f"Terminal output barely changed \u2014 the command "
                    f"may not have been submitted.")
                send_response(chat_id, f"\U0001f916 {result_msg}")
                if on_complete:
                    on_complete(chat_id, result_msg)
                return

            self._update_task(tasks_dict, global_lock, task_id, "done")

            # Show last 30 lines
            lines: list[str] = output.strip().split('\n')
            tail: str = '\n'.join(lines[-30:])
            result_msg = f"Done ({elapsed}s): {description}\n\n{tail}"
            send_response(chat_id, f"\U0001f916 {result_msg}")

            # Feed result back to manager LLM for follow-up
            if on_complete:
                on_complete(chat_id, result_msg)

        except Exception as e:
            logger.error("Command execution error for task #%d: %s", task_id, e)
            self._update_task(tasks_dict, global_lock, task_id, "error")
            send_response(chat_id, f"\U0001f916 Command error: {e}")

    def _update_task(self, tasks_dict: dict[int, Any], global_lock: threading.Lock,
                     task_id: int, status: str) -> None:
        with global_lock:
            if task_id in tasks_dict:
                tasks_dict[task_id].status = status
                tasks_dict[task_id].finished_at = time.time()


class QueuedCommand:
    """Thin wrapper so queued commands appear in list_tasks."""
    def __init__(self, task_id: int, terminal_id: str,
                 description: str, chat_id: int) -> None:
        self.task_id: int = task_id
        self.terminal_id: str = terminal_id
        self.description: str = description
        self.chat_id: int = chat_id
        self.status: str = "queued"
        self.started_at: float = time.time()
        self.finished_at: float | None = None
        self.iterations: str = "-"

    def cancel(self) -> None:
        self.status = "cancelled"
        self.finished_at = time.time()
