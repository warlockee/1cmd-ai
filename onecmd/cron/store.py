"""SQLite CRUD for cron job definitions.

Calling spec:
  Inputs: db_path (defaults to cronjobs.sqlite in same dir as this file)
  Outputs: job records (dicts)
  Side effects: SQLite CRUD

Schema:
  CREATE TABLE cronjobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      description TEXT NOT NULL,
      schedule TEXT,
      action_type TEXT NOT NULL DEFAULT 'send_command',
      action_config TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'draft',
      llm_plan TEXT,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      last_run_at REAL,
      last_result TEXT,
      error TEXT
  )

Operations: create, get, list_all, update, delete, list_active
Thread-safe: threading.Lock
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_DB_PATH = str(Path(__file__).parent / "cronjobs.sqlite")

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS cronjobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    schedule TEXT,
    action_type TEXT NOT NULL DEFAULT 'send_command',
    action_config TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    llm_plan TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_run_at REAL,
    last_result TEXT,
    error TEXT
)
"""

_COLUMNS = [
    "id", "description", "schedule", "action_type", "action_config",
    "status", "llm_plan", "created_at", "updated_at", "last_run_at",
    "last_result", "error",
]

_UPDATABLE = {
    "description", "schedule", "action_type", "action_config",
    "status", "llm_plan", "last_run_at", "last_result", "error",
}


class CronStore:
    """Persistent SQLite store for cron job definitions."""

    def __init__(self, db_path: str = _DB_PATH) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return dict(row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, description: str) -> int:
        """Insert a new draft job. Returns the job id."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cronjobs (description, status, created_at, updated_at)"
                " VALUES (?, 'draft', ?, ?)",
                (description, now, now),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get(self, job_id: int) -> dict | None:
        """Return a single job as a dict, or None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cronjobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._row_to_dict(row)

    def list_all(self) -> list[dict]:
        """Return all jobs ordered by id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cronjobs ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_active(self) -> list[dict]:
        """Return jobs where status='active', ordered by id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cronjobs WHERE status = 'active' ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def update(self, job_id: int, **fields: object) -> bool:
        """Update specified fields on a job. Returns True if the row existed."""
        valid = {k: v for k, v in fields.items() if k in _UPDATABLE}
        if not valid:
            return False
        valid["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in valid)
        values = list(valid.values()) + [job_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE cronjobs SET {set_clause} WHERE id = ?",  # noqa: S608
                values,
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete(self, job_id: int) -> bool:
        """Delete a job. Returns True if the row existed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cronjobs WHERE id = ?", (job_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
