"""
onecmd mgr — Background task runner (repeating tasks).
"""
from __future__ import annotations

import logging
import threading
import time

from utils import send_response, _next_task_id, MAX_TASK_ITERATIONS, MAX_TASK_TIMEOUT
from tools import capture_terminal, send_keys
from terminal_queue import _wait_stable, _has_pending_command

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Background task runner (repeating tasks)
# ---------------------------------------------------------------------------

class BackgroundTask:

    def __init__(self, chat_id: int, terminal_id: str, send_text: str,
                 check_contains: str, description: str,
                 poll_interval: int = 10,
                 max_iterations: int = MAX_TASK_ITERATIONS) -> None:
        self.task_id: int = _next_task_id()
        self.chat_id: int = chat_id
        self.terminal_id: str = terminal_id
        self.send_text: str = send_text or ""
        self.check_contains: str = check_contains  # plain text substring match
        self.description: str = description
        self.poll_interval: int = max(5, poll_interval)
        self.max_iterations: int = min(max_iterations, MAX_TASK_ITERATIONS)
        self.iterations: int = 0
        self.status: str = "running"  # running, completed, cancelled, failed
        self.started_at: float = time.time()
        self.finished_at: float | None = None
        self._cancel_event: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.status = "cancelled"
        self.finished_at = time.time()

    def _run(self) -> None:
        try:
            while self.iterations < self.max_iterations:
                if self._cancel_event.is_set():
                    return
                if time.time() - self.started_at > MAX_TASK_TIMEOUT:
                    self.status = "failed"
                    self.finished_at = time.time()
                    logger.warning("Task #%d timed out after %d iterations",
                                   self.task_id, self.iterations)
                    send_response(self.chat_id,
                        f"\U0001f916 Task #{self.task_id} timed out after "
                        f"{self.iterations} iterations: {self.description}")
                    return

                # Send keys if specified
                if self.send_text:
                    pre_send: str = capture_terminal(self.terminal_id)
                    send_keys(self.terminal_id, self.send_text)
                    # Wait for output to stabilize after sending
                    post_send: str = _wait_stable(
                        self.terminal_id, stable_seconds=5,
                        cancel_event=self._cancel_event,
                        baseline=pre_send)
                    # Auto-enter: if command was typed but Enter wasn't processed
                    if _has_pending_command(pre_send, post_send):
                        logger.info("Task #%d: pending command detected, sending Enter",
                                    self.task_id)
                        send_keys(self.terminal_id, "\n")
                        _wait_stable(self.terminal_id, stable_seconds=5,
                                     cancel_event=self._cancel_event,
                                     baseline=post_send)

                # Capture and check for the target text
                output: str = capture_terminal(self.terminal_id)
                self.iterations += 1

                if self.check_contains and self.check_contains in output:
                    self.status = "completed"
                    self.finished_at = time.time()
                    elapsed: int = int(self.finished_at - self.started_at)
                    lines: list[str] = output.strip().split('\n')
                    tail: str = '\n'.join(lines[-30:])
                    logger.info("Task #%d completed after %d iterations (%ds)",
                                self.task_id, self.iterations, elapsed)
                    send_response(self.chat_id,
                        f"\U0001f916 Task #{self.task_id} complete "
                        f"({self.iterations} iterations, {elapsed}s): "
                        f"{self.description}\n\n{tail}")
                    return

                # Wait before next iteration
                self._cancel_event.wait(self.poll_interval)

            # Max iterations reached
            self.status = "failed"
            self.finished_at = time.time()
            logger.warning("Task #%d reached max iterations (%d)",
                           self.task_id, self.max_iterations)
            send_response(self.chat_id,
                f"\U0001f916 Task #{self.task_id} reached max iterations "
                f"({self.max_iterations}): {self.description}")

        except Exception as e:
            self.status = "failed"
            self.finished_at = time.time()
            logger.error("Task #%d error: %s", self.task_id, e)
            send_response(self.chat_id,
                f"\U0001f916 Task #{self.task_id} error: {e}")
