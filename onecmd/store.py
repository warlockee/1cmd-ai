"""SQLite key-value store.

Calling spec:
  Inputs:  db path (str)
  Outputs: Store instance
  Side effects: creates SQLite DB and KeyValue table if not present

Operations:
  get(key) -> str | None   — returns value or None if missing/expired
  set(key, value, expire=0) — upsert; expire=0 means no expiry,
                               otherwise epoch timestamp after which key expires
  delete(key) -> None       — remove key (no error if missing)

Schema: CREATE TABLE IF NOT EXISTS KeyValue(
            expire INT, key TEXT UNIQUE, value BLOB)
Thread-safe: sqlite3 with check_same_thread=False
Compatible with existing mybot.sqlite from C version.

Guarding:
  - Parameterized queries only (no string interpolation in SQL)
  - Key length capped at 256 chars
  - Value length capped at 64 KB (65 536 bytes)
"""

from __future__ import annotations

import sqlite3
import threading
import time

MAX_KEY_LENGTH = 256
MAX_VALUE_LENGTH = 65_536  # 64 KB


class Store:
    """Persistent SQLite key-value store."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS KeyValue"
            "(expire INT, key TEXT UNIQUE, value BLOB)"
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Return the value for *key*, or ``None`` if missing or expired."""
        _validate_key(key)
        with self._lock:
            row = self._conn.execute(
                "SELECT expire, value FROM KeyValue WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            expire, value = row
            if expire and time.time() > expire:
                self._conn.execute(
                    "DELETE FROM KeyValue WHERE key = ?", (key,)
                )
                self._conn.commit()
                return None
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value

    def set(self, key: str, value: str, expire: int = 0) -> None:  # noqa: A003
        """Insert or update *key*.

        *expire* is a Unix epoch timestamp.  ``0`` means no expiry.
        """
        _validate_key(key)
        _validate_value(value)
        with self._lock:
            self._conn.execute(
                "INSERT INTO KeyValue (expire, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET expire = excluded.expire,"
                " value = excluded.value",
                (expire, key, value),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        """Remove *key* (no-op if it does not exist)."""
        _validate_key(key)
        with self._lock:
            self._conn.execute(
                "DELETE FROM KeyValue WHERE key = ?", (key,)
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()


# ------------------------------------------------------------------
# Validation helpers
# ------------------------------------------------------------------


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("Key must be a non-empty string")
    if len(key) > MAX_KEY_LENGTH:
        raise ValueError(
            f"Key too long: {len(key)} chars (max {MAX_KEY_LENGTH})"
        )


def _validate_value(value: str) -> None:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if len(encoded) > MAX_VALUE_LENGTH:
        raise ValueError(
            f"Value too large: {len(encoded)} bytes (max {MAX_VALUE_LENGTH})"
        )
