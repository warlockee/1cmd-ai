"""Background and smart task runners.

Calling spec:
  Inputs:  task config (terminal_id, text, interval, etc.), backend, callbacks
  Outputs: task_id (int)
  Side effects: spawns daemon thread, periodic terminal operations
  TASK_TYPES = {"background": BackgroundTask, "smart": SmartTask}
  MAX_ITERATIONS: 100, MAX_TIMEOUT: 3600s, DEFAULT_POLL_INTERVAL: 10s
  PRUNE_AGE: 3600s, SMART_TASK_HISTORY_LIMIT: 20 exchanges
BackgroundTask: send + check substring.  SmartTask: LLM-judged cycle.
Both use Backend (capture/send_keys).  SmartTask takes a chat callable.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

MAX_ITERATIONS: int = 100
MAX_TIMEOUT: float = 3600.0
DEFAULT_POLL_INTERVAL: int = 10
PRUNE_AGE: float = 3600.0
SMART_TASK_HISTORY_LIMIT: int = 20

_task_counter = itertools.count(1)
def _next_task_id() -> int: return next(_task_counter)


class _Backend(Protocol):
    def capture(self, term_id: str) -> str | None: ...
    def send_keys(self, term_id: str, text: str) -> bool: ...


NotifyFn = Callable[[str], None]

# Chat callable: (system, tools, messages, max_tokens) -> (serialized, texts, tool_uses, stop)
ChatFn = Callable[
    [str, list[dict[str, Any]], list[dict[str, Any]], int],
    tuple[list[dict[str, Any]], list[str],
          list[tuple[str, str, dict[str, Any]]], str | None],
]
# Format tool results: (results) -> message dict
FormatResultsFn = Callable[[list[tuple[str, str, str]]], dict[str, Any]]


def _wait_stable(backend: _Backend, tid: str, cancel: threading.Event | None,
                 baseline: str | None = None) -> str:
    from onecmd.manager.queue import _wait_stable as _ws
    return _ws(backend, tid, stable_seconds=5.0, cancel_event=cancel,
               baseline=baseline)


def _has_pending(baseline: str, output: str) -> bool:
    from onecmd.manager.queue import _has_pending_command
    return _has_pending_command(baseline, output)


def _send_with_enter(backend: _Backend, tid: str, text: str,
                     cancel: threading.Event, task_label: str) -> None:
    """Send text, wait for it to appear, then send Enter separately.

    On laggy terminals, sending text + \\n in one call often loses the Enter.
    Always split: send text → wait → send Enter → wait.
    """
    # Strip any trailing newlines — we'll send Enter separately
    text = text.rstrip("\n")
    if not text:
        # Just an Enter
        pre = backend.capture(tid) or ""
        backend.send_keys(tid, "\n")
        _wait_stable(backend, tid, cancel, baseline=pre)
        return
    pre = backend.capture(tid) or ""
    backend.send_keys(tid, text)
    post = _wait_stable(backend, tid, cancel, baseline=pre)
    if cancel.is_set():
        return
    logger.info("%s: sending Enter separately", task_label)
    backend.send_keys(tid, "\n")
    _wait_stable(backend, tid, cancel, baseline=post)


class BackgroundTask:
    """Periodic task: send keys, check output for a substring match."""

    def __init__(self, terminal_id: str, backend: _Backend, notify: NotifyFn,
                 send_text: str, check_contains: str, description: str,
                 poll_interval: int = DEFAULT_POLL_INTERVAL,
                 max_iterations: int = MAX_ITERATIONS) -> None:
        self.task_id = _next_task_id()
        self.terminal_id, self._backend, self._notify = terminal_id, backend, notify
        self.send_text = send_text or ""
        self.check_contains = check_contains
        self.description = description
        self.poll_interval = max(5, poll_interval)
        self.max_iterations = min(max_iterations, MAX_ITERATIONS)
        self.iterations = 0
        self.status = "running"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        self.status = "cancelled"
        self.finished_at = time.time()

    def _run(self) -> None:
        try:
            while self.iterations < self.max_iterations:
                if self._cancel.is_set():
                    return
                if time.time() - self.started_at > MAX_TIMEOUT:
                    return self._fail(
                        f"Task #{self.task_id} timed out after "
                        f"{self.iterations} iterations: {self.description}")
                if self.send_text:
                    _send_with_enter(self._backend, self.terminal_id,
                                     self.send_text, self._cancel,
                                     f"Task #{self.task_id}")
                output = self._backend.capture(self.terminal_id) or ""
                self.iterations += 1
                if self.check_contains and self.check_contains in output:
                    self.status = "completed"
                    self.finished_at = time.time()
                    elapsed = int(self.finished_at - self.started_at)
                    tail = "\n".join(output.strip().split("\n")[-30:])
                    logger.info("Task #%d completed (%d iters, %ds)",
                                self.task_id, self.iterations, elapsed)
                    self._notify(
                        f"Task #{self.task_id} complete "
                        f"({self.iterations} iterations, {elapsed}s): "
                        f"{self.description}\n\n{tail}")
                    return
                self._cancel.wait(self.poll_interval)
            self._fail(f"Task #{self.task_id} reached max iterations "
                       f"({self.max_iterations}): {self.description}")
        except Exception as e:
            self._fail(f"Task #{self.task_id} error: {e}")

    def _fail(self, msg: str) -> None:
        self.status = "failed"
        self.finished_at = time.time()
        logger.warning(msg)
        self._notify(msg)


# -- Judgment tools for SmartTask LLM calls --
def _tool(name: str, desc: str, props: dict | None = None,
          req: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "description": desc, "input_schema": {
        "type": "object", "properties": props or {}, "required": req or []}}

_MSG = {"type": "string", "description": "Brief message"}
_KEYS = {"type": "string", "description": "Keystrokes to send (\\n for Enter)"}

JUDGMENT_TOOLS: list[dict[str, Any]] = [
    _tool("continue_monitoring", "Nothing notable. Keep watching."),
    _tool("task_complete", "Goal achieved. Notify and stop.",
          {"message": _MSG}, ["message"]),
    _tool("notify_user", "Something important. Alert but keep monitoring.",
          {"message": _MSG}, ["message"]),
    _tool("send_to_terminal", "Send keystrokes to recover or progress.",
          {"keys": _KEYS, "message": _MSG}, ["keys", "message"]),
]


class SmartTask:
    """Background task using an LLM to judge terminal state each iteration."""

    def __init__(self, terminal_id: str, backend: _Backend, notify: NotifyFn,
                 chat_fn: ChatFn, format_results_fn: FormatResultsFn,
                 prompt: str, description: str = "", send_text: str = "",
                 poll_interval: int = DEFAULT_POLL_INTERVAL,
                 max_iterations: int = MAX_ITERATIONS,
                 debug: bool = False,
                 on_complete: Callable[[str], None] | None = None) -> None:
        self.task_id = _next_task_id()
        self.terminal_id, self._backend, self._notify = terminal_id, backend, notify
        self._debug = debug
        self._on_complete = on_complete
        self._chat_fn, self._fmt = chat_fn, format_results_fn
        self.prompt = prompt
        self.description = description or prompt[:80]
        self.send_text = send_text or ""
        self.poll_interval = max(5, poll_interval)
        self.max_iterations = min(max_iterations, MAX_ITERATIONS)
        self.iterations = 0
        self.status = "running"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._history: list[dict[str, Any]] = []
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        self.status = "cancelled"
        self.finished_at = time.time()

    def _system_prompt(self) -> str:
        return (f"You are monitoring a terminal. Goal: {self.prompt}\n\n"
            "Each message has BEFORE/AFTER snapshots. Use tools to decide:\n"
            "continue_monitoring | task_complete | notify_user | send_to_terminal\n\n"
            "STUCK DETECTION: If AFTER shows text typed at a prompt but not "
            "submitted (the prompt line got longer but no new output appeared), "
            "send Enter via send_to_terminal with keys=\"\\n\". This is common "
            "on laggy terminals where the Enter key didn't register.\n\n"
            "Keep messages concise, plain text only.")

    def _trim_history(self) -> None:
        limit = SMART_TASK_HISTORY_LIMIT * 2
        if len(self._history) > limit:
            self._history[:] = self._history[-limit:]

    def _capture_tail(self, n: int = 80) -> str:
        output = self._backend.capture(self.terminal_id) or ""
        lines = output.strip().split("\n")
        return "\n".join(lines[-n:]) if len(lines) > n else output

    def _run(self) -> None:
        system = self._system_prompt()
        try:
            while self.iterations < self.max_iterations:
                if self._cancel.is_set():
                    return
                if time.time() - self.started_at > MAX_TIMEOUT:
                    return self._fail(
                        f"Smart task #{self.task_id} timed out after "
                        f"{self.iterations} iterations: {self.description}")
                before = self._capture_tail()
                if self.send_text and self.iterations == 0:
                    _send_with_enter(self._backend, self.terminal_id,
                                     self.send_text, self._cancel,
                                     f"SmartTask #{self.task_id}")
                    if self._cancel.is_set():
                        return
                after = self._capture_tail()
                self.iterations += 1
                self._history.append({"role": "user",
                    "content": f"BEFORE:\n{before}\n\nAFTER:\n{after}"})
                try:
                    ser, _, tool_uses, _ = self._chat_fn(
                        system, JUDGMENT_TOOLS, self._history, 512)
                    self._history.append({"role": "assistant", "content": ser})
                except Exception as e:
                    logger.error("SmartTask #%d LLM error: %s", self.task_id, e)
                    self._history.pop()
                    self._cancel.wait(self.poll_interval)
                    continue
                if tool_uses:
                    acted = self._process_tools(tool_uses)
                    if self.status != "running":
                        return
                    if acted:
                        pre = self._backend.capture(self.terminal_id) or ""
                        _wait_stable(self._backend, self.terminal_id,
                                     self._cancel, baseline=pre)
                self._trim_history()
                self._cancel.wait(self.poll_interval)
            self._fail(f"Smart task #{self.task_id} reached max iterations "
                       f"({self.max_iterations}): {self.description}")
        except Exception as e:
            self._fail(f"Smart task #{self.task_id} error: {e}")

    def _process_tools(self, tool_uses: list[tuple[str, str, dict[str, Any]]],
                       ) -> bool:
        results: list[tuple[str, str, str]] = []
        acted = False
        for tu_id, name, args in tool_uses:
            if name == "continue_monitoring":
                results.append((tu_id, name, "OK, continuing."))
            elif name == "task_complete":
                msg = args.get("message", "Task complete.")
                self.status = "completed"
                self.finished_at = time.time()
                elapsed = int(self.finished_at - self.started_at)
                result = (f"Smart task #{self.task_id} complete "
                          f"({self.iterations} iterations, {elapsed}s): {msg}")
                logger.info("SmartTask #%d completed: %s", self.task_id, msg)
                if self._on_complete:
                    self._on_complete(result)
                else:
                    self._notify(result)
                return False
            elif name == "notify_user":
                msg = args.get("message", "Alert from smart task.")
                logger.info("SmartTask #%d notify: %s", self.task_id, msg)
                if self._debug:
                    self._notify(f"Smart task #{self.task_id}: {msg}")
                results.append((tu_id, name, "User notified."))
            elif name == "send_to_terminal":
                keys, msg = args.get("keys", ""), args.get("message", "Sending keys.")
                logger.info("SmartTask #%d send_to_terminal: %s", self.task_id, msg)
                if keys:
                    _send_with_enter(self._backend, self.terminal_id,
                                     keys, self._cancel,
                                     f"SmartTask #{self.task_id}")
                if self._debug:
                    self._notify(f"Smart task #{self.task_id}: {msg}")
                results.append((tu_id, name, "Keys sent."))
                acted = True
            else:
                results.append((tu_id, name, f"Unknown tool: {name}"))
        if results:
            self._history.append(self._fmt(results))
        return acted

    def _fail(self, msg: str) -> None:
        self.status = "failed"
        self.finished_at = time.time()
        logger.warning(msg)
        if self._on_complete:
            self._on_complete(msg)
        else:
            self._notify(msg)


TASK_TYPES: dict[str, type] = {"background": BackgroundTask, "smart": SmartTask}
