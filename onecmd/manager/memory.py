"""
onecmd.manager.memory — Long-term memory (SQLite).

Calling spec:
  Inputs:  chat_id, content, category
  Outputs: memory list or bool
  Side effects: SQLite read/write to memory.sqlite

Operations: save, delete, list_for_chat
Limit: 100 memories per chat (oldest pruned)
Categories: rule, knowledge, preference
Thread-safe: threading.Lock around all DB operations
Parameterized queries only
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

_DB_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "memory.sqlite"
)
_lock = threading.Lock()
_MAX: int = 100
_MAX_CONTENT_CHARS: int = 500  # per-memory content cap at save time


def _connect() -> sqlite3.Connection:
    """Open DB and ensure schema exists."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  chat_id INTEGER NOT NULL,"
        "  content TEXT NOT NULL,"
        "  category TEXT NOT NULL DEFAULT 'general',"
        "  created_at REAL NOT NULL,"
        "  updated_at REAL NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mem_chat ON memories(chat_id)"
    )
    conn.commit()
    return conn


def list_for_chat(chat_id: int) -> list[tuple[int, str, str]]:
    """Return all memories for *chat_id* as [(id, content, category), ...]."""
    with _lock:
        conn = _connect()
        try:
            return conn.execute(
                "SELECT id, content, category FROM memories "
                "WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        finally:
            conn.close()


def save(chat_id: int, content: str, category: str = "general") -> int | None:
    """Insert a memory and prune oldest beyond the per-chat cap. Returns row id."""
    # Enforce content length at save time — LLM should be concise but enforce it
    if len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS].rstrip() + "..."
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO memories "
                "(chat_id, content, category, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, content, category, now, now),
            )
            conn.execute(
                "DELETE FROM memories WHERE chat_id = ? AND id NOT IN "
                "(SELECT id FROM memories WHERE chat_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (chat_id, chat_id, _MAX),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def delete(chat_id: int, memory_id: int) -> bool:
    """Delete a memory by id (scoped to *chat_id*). Returns True if removed."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM memories WHERE id = ? AND chat_id = ?",
                (memory_id, chat_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def list_all() -> list[tuple[int, int, str, str, float]]:
    """Return all memories as [(id, chat_id, content, category, created_at), ...]."""
    with _lock:
        conn = _connect()
        try:
            return conn.execute(
                "SELECT id, chat_id, content, category, created_at "
                "FROM memories ORDER BY id"
            ).fetchall()
        finally:
            conn.close()


def delete_by_id(memory_id: int) -> bool:
    """Delete a memory by id (admin, no chat_id scope). Returns True if removed."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def update(memory_id: int, content: str, category: str | None = None) -> bool:
    """Update a memory's content (and optionally category). Returns True if updated."""
    with _lock:
        conn = _connect()
        try:
            if category is not None:
                cur = conn.execute(
                    "UPDATE memories SET content = ?, category = ?, "
                    "updated_at = ? WHERE id = ?",
                    (content, category, time.time(), memory_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? "
                    "WHERE id = ?",
                    (content, time.time(), memory_id),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
