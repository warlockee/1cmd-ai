"""
onecmd mgr — Smart background task with LLM-based judgment loop.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from utils import (
    send_response, _next_task_id, MAX_TASK_ITERATIONS, MAX_TASK_TIMEOUT,
    SMART_TASK_MODEL, SMART_TASK_HISTORY_LIMIT,
)
from tools import capture_terminal, capture_terminal_tail, send_keys
from terminal_queue import _wait_stable, _has_pending_command
import llm as llm_mod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Judgment tools — the LLM uses these to express its decision each iteration
# ---------------------------------------------------------------------------

JUDGMENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "continue_monitoring",
        "description": "Nothing notable happened. Keep watching.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "task_complete",
        "description": "The goal has been achieved. Notify the user and stop monitoring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Brief message describing what completed (sent to user via Telegram)",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "notify_user",
        "description": "Something important happened that the user should know about. Send an alert but keep monitoring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Brief alert message (sent to user via Telegram)",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "send_to_terminal",
        "description": "Send keystrokes to the terminal to recover or progress. Also notifies the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "Keystrokes to send (use \\n for Enter)",
                },
                "message": {
                    "type": "string",
                    "description": "Brief explanation of what you're doing (sent to user via Telegram)",
                },
            },
            "required": ["keys", "message"],
        },
    },
]


class SmartTask:
    """Background task that uses an LLM to judge terminal state each iteration."""

    def __init__(
        self,
        chat_id: int,
        terminal_id: str,
        prompt: str,
        client: Any,
        description: str = "",
        send_text: str = "",
        poll_interval: int = 10,
        max_iterations: int = MAX_TASK_ITERATIONS,
        model: str | None = None,
    ) -> None:
        self.task_id: int = _next_task_id()
        self.chat_id: int = chat_id
        self.terminal_id: str = terminal_id
        self.prompt: str = prompt
        self.description: str = description or prompt[:80]
        self.send_text: str = send_text or ""
        self.poll_interval: int = max(5, poll_interval)
        self.max_iterations: int = min(max_iterations, MAX_TASK_ITERATIONS)
        self.iterations: int = 0
        self.status: str = "running"
        self.started_at: float = time.time()
        self.finished_at: float | None = None

        self.client: Any = client
        self.model: str = model or SMART_TASK_MODEL

        self._history: list[dict[str, Any]] = []
        self._cancel_event: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.status = "cancelled"
        self.finished_at = time.time()

    def _build_system_prompt(self) -> str:
        return (
            f"You are monitoring a terminal. The user's goal: {self.prompt}\n\n"
            "Each message shows BEFORE and AFTER terminal snapshots.\n"
            "Use your tools to decide what to do:\n"
            "- continue_monitoring: nothing notable, keep watching\n"
            "- task_complete: the goal has been achieved\n"
            "- notify_user: something important happened, alert the user\n"
            "- send_to_terminal: send keystrokes to recover or progress\n\n"
            "Keep messages concise — they go to a phone (Telegram). Plain text only, no markdown."
        )

    def _trim_history(self) -> None:
        """Keep only the last N exchanges (user + assistant pairs)."""
        limit: int = SMART_TASK_HISTORY_LIMIT * 2  # each exchange = 2 messages
        if len(self._history) > limit:
            self._history[:] = self._history[-limit:]

    def _run(self) -> None:
        system_prompt: str = self._build_system_prompt()

        try:
            while self.iterations < self.max_iterations:
                if self._cancel_event.is_set():
                    return
                if time.time() - self.started_at > MAX_TASK_TIMEOUT:
                    self.status = "failed"
                    self.finished_at = time.time()
                    logger.warning("SmartTask #%d timed out after %d iterations",
                                   self.task_id, self.iterations)
                    send_response(self.chat_id,
                        f"\U0001f916 Smart task #{self.task_id} timed out after "
                        f"{self.iterations} iterations: {self.description}")
                    return

                # Capture before snapshot (tail only — full scrollback would exhaust tokens)
                before: str = capture_terminal_tail(self.terminal_id)

                # Optionally send text
                if self.send_text:
                    send_keys(self.terminal_id, self.send_text)
                    post_send: str = _wait_stable(
                        self.terminal_id, stable_seconds=5,
                        cancel_event=self._cancel_event,
                        baseline=before)
                    # Auto-enter: if command was typed but Enter wasn't processed
                    if _has_pending_command(before, post_send):
                        logger.info("SmartTask #%d: pending command, sending Enter",
                                    self.task_id)
                        send_keys(self.terminal_id, "\n")
                        _wait_stable(self.terminal_id, stable_seconds=5,
                                     cancel_event=self._cancel_event,
                                     baseline=post_send)
                    if self._cancel_event.is_set():
                        return

                # Capture after snapshot (tail only)
                after: str = capture_terminal_tail(self.terminal_id)
                self.iterations += 1

                # Build user message with both snapshots
                user_msg: str = f"BEFORE:\n{before}\n\nAFTER:\n{after}"
                self._history.append({"role": "user", "content": user_msg})

                # Call LLM with judgment tools
                try:
                    serialized, text_parts, tool_uses, _ = llm_mod.chat(
                        self.client, self.model, system_prompt,
                        JUDGMENT_TOOLS, self._history, max_tokens=512,
                    )
                    self._history.append({"role": "assistant", "content": serialized})
                except Exception as e:
                    logger.error("SmartTask #%d LLM error: %s", self.task_id, e)
                    # Remove the user message we just added so history stays clean
                    self._history.pop()
                    # Wait and retry next iteration
                    self._cancel_event.wait(self.poll_interval)
                    continue

                # Process the judgment
                if tool_uses:
                    # Build tool results for conversation continuity
                    tool_results: list[tuple[str, str, str]] = []
                    action_taken: bool = False

                    for tu_id, tu_name, tu_args in tool_uses:
                        if tu_name == "continue_monitoring":
                            logger.debug("SmartTask #%d: continue", self.task_id)
                            tool_results.append((tu_id, tu_name, "OK, continuing."))

                        elif tu_name == "task_complete":
                            msg: str = tu_args.get("message", "Task complete.")
                            self.status = "completed"
                            self.finished_at = time.time()
                            elapsed: int = int(self.finished_at - self.started_at)
                            logger.info("SmartTask #%d completed: %s", self.task_id, msg)
                            send_response(self.chat_id,
                                f"\U0001f916 Smart task #{self.task_id} complete "
                                f"({self.iterations} iterations, {elapsed}s): {msg}")
                            return

                        elif tu_name == "notify_user":
                            msg = tu_args.get("message", "Alert from smart task.")
                            logger.info("SmartTask #%d notify: %s", self.task_id, msg)
                            send_response(self.chat_id,
                                f"\U0001f916 Smart task #{self.task_id}: {msg}")
                            tool_results.append((tu_id, tu_name, "User notified."))

                        elif tu_name == "send_to_terminal":
                            keys: str = tu_args.get("keys", "")
                            msg = tu_args.get("message", "Sending keys to terminal.")
                            logger.info("SmartTask #%d send_to_terminal: %s", self.task_id, msg)
                            if keys:
                                send_keys(self.terminal_id, keys)
                            send_response(self.chat_id,
                                f"\U0001f916 Smart task #{self.task_id}: {msg}")
                            tool_results.append((tu_id, tu_name, "Keys sent."))
                            action_taken = True

                        else:
                            tool_results.append((tu_id, tu_name, f"Unknown tool: {tu_name}"))

                    # Append tool results to history for context continuity
                    if tool_results:
                        self._history.append(
                            llm_mod.format_tool_results(self.client, tool_results)
                        )

                    # If we sent keys, wait for stability before next iteration
                    if action_taken:
                        pre_action: str = capture_terminal(self.terminal_id)
                        _wait_stable(self.terminal_id, stable_seconds=5,
                                     cancel_event=self._cancel_event,
                                     baseline=pre_action)
                else:
                    # No tool use — treat as continue
                    logger.debug("SmartTask #%d: no tool use, continuing", self.task_id)

                # Trim history to prevent unbounded growth
                self._trim_history()

                # Wait before next iteration
                self._cancel_event.wait(self.poll_interval)

            # Max iterations reached
            self.status = "failed"
            self.finished_at = time.time()
            logger.warning("SmartTask #%d reached max iterations (%d)",
                           self.task_id, self.max_iterations)
            send_response(self.chat_id,
                f"\U0001f916 Smart task #{self.task_id} reached max iterations "
                f"({self.max_iterations}): {self.description}")

        except Exception as e:
            self.status = "failed"
            self.finished_at = time.time()
            logger.error("SmartTask #%d error: %s", self.task_id, e)
            send_response(self.chat_id,
                f"\U0001f916 Smart task #{self.task_id} error: {e}")
