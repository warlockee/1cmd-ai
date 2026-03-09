"""
onecmd mgr — Tool definitions and terminal operation helpers.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from utils import ctl_run

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Terminal activity tracker (in-memory, resets on restart)
# ---------------------------------------------------------------------------

_activity: dict[str, tuple[str, float]] = {}  # id -> (tail_content, timestamp)
_activity_lock = threading.Lock()
_poll_started = False

def _tail(text: str, n: int = 20) -> str:
    lines = text.strip().split('\n')
    return '\n'.join(lines[-n:]) if len(lines) > n else text.strip()

def _is_meaningful_change(old: str, new: str) -> bool:
    """Return True if the change represents real terminal activity.
    Ignores changes where only 1 line differs (status bars, clocks,
    spinners, prompt redraws — regardless of position).
    Compares using the shorter length to handle line count fluctuation."""
    old_lines = old.split('\n')
    new_lines = new.split('\n')
    # Compare overlapping lines; extra lines at the end count as changes
    min_len = min(len(old_lines), len(new_lines))
    changed = sum(1 for i in range(min_len) if old_lines[i] != new_lines[i])
    changed += abs(len(old_lines) - len(new_lines))
    return changed >= 2

def _track(terminal_id: str, content: str) -> None:
    tail = _tail(content)
    now = time.time()
    with _activity_lock:
        prev = _activity.get(terminal_id)
        if prev is None:
            # First capture: baseline only, no timestamp (shows as "")
            _activity[terminal_id] = (tail, 0)
        elif _is_meaningful_change(prev[0], tail):
            _activity[terminal_id] = (tail, now)
        else:
            # Noise — update content, keep old timestamp
            _activity[terminal_id] = (tail, prev[1])

def _format_ago(terminal_id: str) -> str:
    with _activity_lock:
        entry = _activity.get(terminal_id)
    if entry is None or entry[1] == 0:
        return ""
    delta = int(time.time() - entry[1])
    if delta < 60:
        return "active just now"
    if delta < 3600:
        return f"active {delta // 60}m ago"
    h = delta // 3600
    m = (delta % 3600) // 60
    return f"active {h}h{m}m ago" if m else f"active {h}h ago"

def _poll_loop() -> None:
    while True:
        try:
            terminals = _raw_list_terminals()
            for t in terminals:
                out, _, rc = ctl_run(["capture", str(t["id"])])
                if rc == 0:
                    _track(t["id"], out)
        except Exception as e:
            logger.error("Activity poll error: %s", e)
        time.sleep(60)

def _ensure_polling() -> None:
    global _poll_started
    if not _poll_started:
        _poll_started = True
        threading.Thread(target=_poll_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# Terminal operations
# ---------------------------------------------------------------------------

def _raw_list_terminals() -> list[dict[str, Any]]:
    out, err, rc = ctl_run(["list"])
    if rc != 0:
        logger.error("list_terminals failed (rc=%d): %s", rc, err)
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        logger.error("list_terminals JSON parse error: %s (output: %s)", e, out[:200])
        return []

def list_terminals() -> list[dict[str, Any]]:
    _ensure_polling()
    terminals = _raw_list_terminals()
    aliases = _read_aliases()
    for t in terminals:
        ago = _format_ago(t["id"])
        if ago:
            t["last_active"] = ago
        alias = aliases.get(str(t["id"]))
        if alias:
            t["alias"] = alias
    return terminals

_CAPTURE_LINES: int = 80  # Roughly one screen worth of output

def capture_terminal(terminal_id: str) -> str:
    """Capture full text from a terminal by ID (used for stability detection)."""
    out, err, rc = ctl_run(["capture", str(terminal_id)])
    if rc != 0:
        return f"[Error capturing terminal {terminal_id}: {err}]"
    _track(terminal_id, out)
    return out

def capture_terminal_tail(terminal_id: str) -> str:
    """Capture last N lines from a terminal (used for LLM-facing reads)."""
    out = capture_terminal(terminal_id)
    if out.startswith("[Error"):
        return out
    lines = out.split('\n')
    if len(lines) > _CAPTURE_LINES:
        lines = lines[-_CAPTURE_LINES:]
    return '\n'.join(lines)

def send_keys(terminal_id: str, keys: str) -> str:
    """Send keystrokes to a terminal by ID."""
    # Translate literal control characters to the escape sequences
    # that the backend expects: \n → \\n (Enter), \t → \\t (Tab)
    keys = keys.replace('\n', '\\n')
    keys = keys.replace('\t', '\\t')
    out, err, rc = ctl_run(["send", str(terminal_id), keys])
    if rc != 0:
        return f"[Error sending keys to terminal {terminal_id}: {err}]"
    return "Keys sent successfully."

# ---------------------------------------------------------------------------
# Terminal aliases
# ---------------------------------------------------------------------------

ALIASES_PATH = ".onecmd/aliases.json"

def _read_aliases() -> dict[str, str]:
    try:
        with open(ALIASES_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _write_aliases(aliases: dict[str, str]) -> None:
    os.makedirs(".onecmd", exist_ok=True)
    with open(ALIASES_PATH, "w") as f:
        json.dump(aliases, f, indent=2)

def rename_terminal(terminal_id: str, name: str) -> str:
    """Set a custom name for a terminal."""
    aliases = _read_aliases()
    aliases[terminal_id] = name
    _write_aliases(aliases)
    return f"Terminal '{terminal_id}' renamed to '{name}'."

# ---------------------------------------------------------------------------
# Claude API tools
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_terminals",
        "description": "List all available terminal sessions with their IDs, names, and titles.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "rename_terminal",
        "description": (
            "Give a terminal a custom name for easy identification. "
            "When you first list terminals and see generic names like 'iTerm2 - bash', "
            "proactively suggest descriptive names based on what's running in each terminal "
            "(e.g. 'dev-server', 'db-console', 'build-logs')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "terminal_id": {
                    "type": "string",
                    "description": "Terminal ID from list_terminals"
                },
                "name": {
                    "type": "string",
                    "description": "Custom name for the terminal (e.g. 'dev-server', 'logs')"
                }
            },
            "required": ["terminal_id", "name"]
        }
    },
    {
        "name": "read_terminal",
        "description": "Read/capture the current visible text from a terminal. Use the terminal's 'id' field (e.g., '12399' on macOS or '%%0' on tmux).",
        "input_schema": {
            "type": "object",
            "properties": {
                "terminal_id": {
                    "type": "string",
                    "description": "Terminal ID from list_terminals"
                }
            },
            "required": ["terminal_id"]
        }
    },
    {
        "name": "send_command",
        "description": (
            "Send keystrokes to a terminal and watch for the output to finish in the background. "
            "Returns immediately — the terminal is monitored asynchronously and the user is notified when "
            "the output stabilizes (stops changing). Use this for ALL commands. "
            "You do NOT need to guess if a command is fast or slow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "terminal_id": {
                    "type": "string",
                    "description": "Terminal ID from list_terminals"
                },
                "keys": {
                    "type": "string",
                    "description": "Text/keystrokes to send. Use \\n for Enter, \\t for Tab."
                },
                "description": {
                    "type": "string",
                    "description": "Brief description of what this command does (shown in notification)"
                },
                "stable_seconds": {
                    "type": "number",
                    "description": "How many seconds the output must remain unchanged to be considered done. Default 5. Increase for slower programs."
                }
            },
            "required": ["terminal_id", "keys", "description"]
        }
    },
    {
        "name": "start_background_task",
        "description": (
            "Start a repeating background task that periodically sends input to a terminal "
            "and checks for a specific text condition. Useful for polling tasks like "
            "'keep asking until it says yes'. The task monitors output stability after each send."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "terminal_id": {
                    "type": "string",
                    "description": "Terminal ID to operate on"
                },
                "send_text": {
                    "type": "string",
                    "description": "Text to send to the terminal each iteration (empty string to just monitor)"
                },
                "check_contains": {
                    "type": "string",
                    "description": "Plain text substring to look for in terminal output. Task completes when found."
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable description of what this task does"
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Seconds between sends/checks (default 10, minimum 5)"
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Maximum iterations before giving up (default 100)"
                }
            },
            "required": ["terminal_id", "check_contains", "description"]
        }
    },
    {
        "name": "start_smart_task",
        "description": (
            "Start an intelligent background task that uses LLM judgment to monitor a terminal. "
            "Each iteration captures before/after terminal snapshots, sends them to an LLM which decides: "
            "continue monitoring, notify the user, send recovery keystrokes, or mark the task complete. "
            "Use this for complex monitoring goals that can't be expressed as a simple substring check — "
            "e.g. 'wait for compilation to finish, then run tests', 'watch for errors and restart if needed', "
            "'monitor training loss and alert if it plateaus'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "terminal_id": {
                    "type": "string",
                    "description": "Terminal ID to monitor",
                },
                "prompt": {
                    "type": "string",
                    "description": "Natural language description of what to monitor/achieve (e.g. 'wait for compilation to finish, then run tests')",
                },
                "send_text": {
                    "type": "string",
                    "description": "Optional text to send to the terminal each iteration before capturing",
                },
                "poll_interval": {
                    "type": "integer",
                    "description": "Seconds between iterations (default 10, minimum 5)",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Maximum iterations before giving up (default 100)",
                },
            },
            "required": ["terminal_id", "prompt"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List all background tasks and their status.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "cancel_task",
        "description": "Cancel a running background task by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "Task ID to cancel"
                }
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save something to long-term memory. Persists across restarts. "
            "Use this when the user says 'remember', 'always', 'never', 'from now on', "
            "or when you learn important facts about their environment or preferences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "What to remember (be specific and concise)"
                },
                "category": {
                    "type": "string",
                    "enum": ["rule", "knowledge", "preference"],
                    "description": "rule = user directives (always/never do X), knowledge = facts about environment, preference = style/behavior preferences"
                }
            },
            "required": ["content", "category"]
        }
    },
    {
        "name": "delete_memory",
        "description": "Delete a memory by its ID. Use when a memory is outdated or the user asks to forget something.",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "integer",
                    "description": "Memory ID to delete (from the memories list in context)"
                }
            },
            "required": ["memory_id"]
        }
    },
]
