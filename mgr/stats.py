"""
onecmd mgr — Runtime statistics and content serialization.
"""
from __future__ import annotations

import logging
import resource
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Serialize content blocks for conversation history
# ---------------------------------------------------------------------------

def serialize_content(content: list[Any]) -> list[dict[str, Any]]:
    """Convert anthropic content blocks to serializable dicts."""
    result: list[dict[str, Any]] = []
    for block in content:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return result

# ---------------------------------------------------------------------------
# Runtime stats (process-lifetime counters)
# ---------------------------------------------------------------------------

class Stats:
    def __init__(self) -> None:
        self.started_at: float = time.time()
        self.messages_processed: int = 0
        self.api_calls: int = 0
        self.tool_calls: int = 0
        self.errors: int = 0
        self.tasks_created: int = 0
        self.tasks_pruned: int = 0
        self._lock: threading.Lock = threading.Lock()

    def inc(self, field: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + n)

    def get_rss_mb(self) -> float:
        """Current RSS in MB."""
        ru = resource.getrusage(resource.RUSAGE_SELF)
        # macOS returns bytes, Linux returns KB
        if sys.platform == "darwin":
            return ru.ru_maxrss / (1024 * 1024)
        return ru.ru_maxrss / 1024

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_s": int(time.time() - self.started_at),
                "rss_mb": round(self.get_rss_mb(), 1),
                "messages": self.messages_processed,
                "api_calls": self.api_calls,
                "tool_calls": self.tool_calls,
                "errors": self.errors,
                "tasks_created": self.tasks_created,
                "tasks_pruned": self.tasks_pruned,
            }

STATS: Stats = Stats()
