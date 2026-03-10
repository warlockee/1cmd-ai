"""Cron scheduler engine — runs active jobs on schedule.

Calling spec:
  Inputs: backend, config
  Outputs: None (runs in background)
  Side effects: executes scheduled jobs, updates job records

Lifecycle: start() -> daemon thread -> tick every 30s -> check due jobs -> execute
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol

from onecmd.cron.store import CronStore

if TYPE_CHECKING:
    from onecmd.config import Config

logger = logging.getLogger(__name__)

TICK_INTERVAL = 30  # seconds between scheduler checks


# ---------------------------------------------------------------------------
# Backend protocol (matches terminal.backend.ValidatedBackend)
# ---------------------------------------------------------------------------

class _Backend(Protocol):
    def capture(self, term_id: str) -> str | None: ...
    def send_keys(self, term_id: str, text: str) -> bool: ...


# ---------------------------------------------------------------------------
# Minimal cron expression matcher
# ---------------------------------------------------------------------------

_ALIASES: dict[str, str] = {
    "@yearly":   "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly":  "0 0 1 * *",
    "@weekly":   "0 0 * * 0",
    "@daily":    "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly":   "0 * * * *",
}


def _parse_field(field: str, current: int, max_val: int) -> bool:
    """Check if *current* matches a single cron field expression.

    Supported patterns:
      *        — match any
      N        — exact match
      */N      — every N (divisible)
      N-M      — range inclusive
      N,M,O    — list of values
    """
    if field == "*":
        return True

    # Handle comma-separated lists
    if "," in field:
        return any(_parse_field(part.strip(), current, max_val) for part in field.split(","))

    # Handle */N
    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step > 0 and current % step == 0
        except ValueError:
            return False

    # Handle N-M range
    if "-" in field:
        parts = field.split("-", 1)
        try:
            lo, hi = int(parts[0]), int(parts[1])
            return lo <= current <= hi
        except ValueError:
            return False

    # Exact value
    try:
        return current == int(field)
    except ValueError:
        return False


def cron_matches(expression: str, now: time.struct_time | None = None) -> bool:
    """Check if a cron expression matches the current (or given) time.

    Supports: ``minute hour day month weekday`` (5 fields) and aliases
    like ``@hourly``, ``@daily``, etc.
    """
    if not expression:
        return False

    expression = expression.strip()

    # Resolve aliases
    if expression.startswith("@"):
        expression = _ALIASES.get(expression.lower(), "")
        if not expression:
            return False

    fields = expression.split()
    if len(fields) != 5:
        return False

    if now is None:
        now = time.localtime()

    minute, hour, day, month, weekday = fields
    return (
        _parse_field(minute,  now.tm_min,  59)
        and _parse_field(hour,    now.tm_hour, 23)
        and _parse_field(day,     now.tm_mday, 31)
        and _parse_field(month,   now.tm_mon,  12)
        and _parse_field(weekday, now.tm_wday, 6)  # 0=Monday in Python
    )


# ---------------------------------------------------------------------------
# CronEngine
# ---------------------------------------------------------------------------

class CronEngine:
    """Background scheduler that ticks every 30 seconds and runs due jobs."""

    def __init__(
        self,
        store: CronStore,
        backend: _Backend | None = None,
        config: Config | None = None,
    ) -> None:
        self._store = store
        self._backend = backend
        self._config = config
        self._active_ids: set[int] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Track last run minute per job to avoid running twice in same minute
        self._last_run_minute: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load active jobs from the store and start the scheduler loop."""
        active = self._store.list_active()
        with self._lock:
            self._active_ids = {j["id"] for j in active}
        logger.info("CronEngine starting with %d active jobs", len(self._active_ids))
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="cron-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the scheduler loop to stop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("CronEngine stopped")

    def add_job(self, job_id: int) -> None:
        """Register a job as active so the scheduler will check it."""
        with self._lock:
            self._active_ids.add(job_id)
        logger.info("CronEngine: added job %d", job_id)

    def remove_job(self, job_id: int) -> None:
        """Unregister a job from the active set."""
        with self._lock:
            self._active_ids.discard(job_id)
            self._last_run_minute.pop(job_id, None)
        logger.info("CronEngine: removed job %d", job_id)

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Tick every TICK_INTERVAL seconds, checking for due jobs."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("CronEngine tick error")
            self._stop_event.wait(TICK_INTERVAL)

    def _tick(self) -> None:
        """Check each active job against the current time and execute if due."""
        now = time.localtime()
        # Unique minute key: YYYYMMDDHHMM
        minute_key = now.tm_year * 100_000_000 + now.tm_mon * 1_000_000 + \
            now.tm_mday * 10_000 + now.tm_hour * 100 + now.tm_min

        with self._lock:
            job_ids = list(self._active_ids)

        for job_id in job_ids:
            # Skip if already ran this minute
            if self._last_run_minute.get(job_id) == minute_key:
                continue

            job = self._store.get(job_id)
            if job is None or job["status"] != "active":
                with self._lock:
                    self._active_ids.discard(job_id)
                continue

            if not job.get("schedule"):
                continue

            if cron_matches(job["schedule"], now):
                self._last_run_minute[job_id] = minute_key
                self._execute(job)

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    def _execute(self, job: dict) -> None:
        """Dispatch a job by its action_type."""
        job_id = job["id"]
        action_type = job.get("action_type", "send_command")
        try:
            config_str = job.get("action_config", "{}")
            action_config: dict[str, Any] = json.loads(config_str) if isinstance(config_str, str) else config_str
        except (json.JSONDecodeError, TypeError):
            action_config = {}

        logger.info("CronEngine: executing job %d (%s)", job_id, action_type)

        try:
            if action_type == "send_command":
                result = self._exec_send_command(action_config)
            elif action_type == "notify":
                result = self._exec_notify(action_config)
            elif action_type == "smart_task":
                result = self._exec_smart_task(action_config)
            else:
                result = f"Unknown action_type: {action_type}"

            self._store.update(
                job_id,
                last_run_at=time.time(),
                last_result=result,
                error=None,
            )
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("CronEngine: job %d failed: %s", job_id, error_msg)
            self._store.update(
                job_id,
                status="error",
                last_run_at=time.time(),
                last_result=None,
                error=error_msg,
            )

    def _exec_send_command(self, config: dict[str, Any]) -> str:
        """Send keys to a terminal."""
        if self._backend is None:
            return "No backend available"
        terminal_id = config.get("terminal_id", "")
        text = config.get("text", "")
        if not terminal_id or not text:
            return "Missing terminal_id or text in action_config"
        ok = self._backend.send_keys(terminal_id, text)
        return f"send_keys({'ok' if ok else 'failed'}): {text!r} -> {terminal_id}"

    def _exec_notify(self, config: dict[str, Any]) -> str:
        """Log a notification message (placeholder)."""
        message = config.get("message", "No message")
        logger.info("CronEngine notify: %s", message)
        return f"Notified: {message}"

    def _exec_smart_task(self, config: dict[str, Any]) -> str:
        """Placeholder for future LLM-based smart tasks."""
        return "smart_task: not yet implemented"
