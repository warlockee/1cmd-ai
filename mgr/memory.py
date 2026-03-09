"""
onecmd mgr — Long-term memory system (SQLite).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Long-term memory (SQLite)
# ---------------------------------------------------------------------------

MEMORY_DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.sqlite")
_memory_db_lock: threading.Lock = threading.Lock()

def _init_memory_db() -> sqlite3.Connection:
    """Create memory table if it doesn't exist. Returns a connection."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mem_chat ON memories(chat_id)
    """)
    conn.commit()
    return conn

def _load_memories(chat_id: int) -> list[tuple[int, str, str]]:
    """Load all memories for a chat. Returns list of (id, content, category)."""
    with _memory_db_lock:
        conn = _init_memory_db()
        try:
            rows = conn.execute(
                "SELECT id, content, category FROM memories WHERE chat_id = ? ORDER BY id",
                (chat_id,)
            ).fetchall()
            return rows
        finally:
            conn.close()

MAX_MEMORIES_PER_CHAT: int = 100

def _save_memory(chat_id: int, content: str, category: str = "general") -> int | None:
    """Save a new memory. Returns the memory ID. Enforces a per-chat cap
    by deleting the oldest memories when the limit is exceeded."""
    now = time.time()
    with _memory_db_lock:
        conn = _init_memory_db()
        try:
            cur = conn.execute(
                "INSERT INTO memories (chat_id, content, category, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, content, category, now, now)
            )
            # Enforce per-chat cap: delete oldest beyond limit
            conn.execute(
                "DELETE FROM memories WHERE chat_id = ? AND id NOT IN "
                "(SELECT id FROM memories WHERE chat_id = ? ORDER BY id DESC LIMIT ?)",
                (chat_id, chat_id, MAX_MEMORIES_PER_CHAT)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

def _delete_memory(chat_id: int, memory_id: int) -> bool:
    """Delete a memory by ID (scoped to chat_id for safety). Returns True if deleted."""
    with _memory_db_lock:
        conn = _init_memory_db()
        try:
            cur = conn.execute(
                "DELETE FROM memories WHERE id = ? AND chat_id = ?",
                (memory_id, chat_id)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
