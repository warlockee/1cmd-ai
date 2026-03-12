"""Tool definitions, registry, and terminal activity tracker.

Calling spec:
  Inputs:  tool_name, tool_args, ctx dict (backend, queue_cls, tasks, chat_id, notify, …)
  Outputs: tool result string (max 4000 chars)
  Side effects: terminal operations via Backend

Exports: TOOL_REGISTRY, TOOL_SCHEMAS, dispatch()
Activity tracker: daemon thread polls every 60s, tracks last_active per terminal.
All terminal ops through ValidatedBackend.  Results truncated to 4000 chars.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 4000
_CAPTURE_LINES = 80  # roughly one screenful for LLM reads
_POLL_INTERVAL = 60  # seconds between activity polls
ALIASES_PATH = ".onecmd/aliases.json"

# ---------------------------------------------------------------------------
# Backend protocol (minimal interface used by this module)
# ---------------------------------------------------------------------------

class _Backend(Protocol):
    def list(self) -> list[Any]: ...
    def capture(self, term_id: str) -> str | None: ...
    def send_keys(self, term_id: str, text: str) -> bool: ...
    def create(self) -> str | None: ...


# ---------------------------------------------------------------------------
# Context type
# ---------------------------------------------------------------------------

# Tools receive a ctx dict with at minimum:
#   backend:   ValidatedBackend
#   queue_cls: TerminalQueue class (has .get(terminal_id, backend))
#   tasks:     dict[int, task]   (shared mutable task registry)
#   tasks_lock: threading.Lock
#   chat_id:   int
#   notify:    Callable[[int, str], None]  (send notification to user)
#   llm_client: Any  (for smart tasks)
#   llm_model:  str  (for smart tasks)
#   next_task_id: Callable[[], int]
ToolFunc = Callable[[dict[str, Any], dict[str, Any]], str]


# ---------------------------------------------------------------------------
# Terminal activity tracker (in-memory, resets on restart)
# ---------------------------------------------------------------------------

_activity: dict[str, tuple[str, float]] = {}  # id -> (tail_content, timestamp)
_activity_lock = threading.Lock()
_poll_started = False


def _tail(text: str, n: int = 20) -> str:
    """Return the last *n* lines of *text*."""
    lines = text.strip().split("\n")
    return "\n".join(lines[-n:]) if len(lines) > n else text.strip()


def _is_meaningful_change(old: str, new: str) -> bool:
    """Return True if the change represents real terminal activity.

    Ignores changes where only 1 line differs (status bars, clocks,
    spinners, prompt redraws — regardless of position).
    Compares using the shorter length to handle line-count fluctuation.
    """
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    min_len = min(len(old_lines), len(new_lines))
    changed = sum(1 for i in range(min_len) if old_lines[i] != new_lines[i])
    changed += abs(len(old_lines) - len(new_lines))
    return changed >= 2


def _track(terminal_id: str, content: str) -> None:
    """Update activity state for a terminal."""
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
    """Format the last-active timestamp as a human-readable string."""
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


def _poll_loop(backend: _Backend) -> None:
    """Background poller: captures all terminals every 60s for activity tracking."""
    while True:
        try:
            terminals = backend.list()
            for t in terminals:
                out = backend.capture(t.id)
                if out is not None:
                    _track(t.id, out)
        except Exception as e:
            logger.error("Activity poll error: %s", e)
        time.sleep(_POLL_INTERVAL)


def _ensure_polling(backend: _Backend) -> None:
    """Start the activity-polling daemon thread (once)."""
    global _poll_started
    if not _poll_started:
        _poll_started = True
        threading.Thread(target=_poll_loop, args=(backend,), daemon=True).start()


# ---------------------------------------------------------------------------
# Terminal aliases (.onecmd/aliases.json — compatible with C version)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Result truncation
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


_PREVIEW_LINES = 5  # lines of content to show in list_terminals auto-read


def tool_list_terminals(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """List all terminal sessions with IDs, names, titles, and activity.

    On first call (before any terminal has been read), auto-captures every
    terminal and includes a short content preview so the LLM knows what's
    running everywhere without needing separate read_terminal calls.
    """
    backend: _Backend = ctx["backend"]
    terminals = backend.list()
    if not terminals:
        _ensure_polling(backend)
        return "No terminals found."

    # Determine which terminals haven't been read yet (before starting
    # the poll daemon, which would race and track them first).
    with _activity_lock:
        unread = [t for t in terminals if t.id not in _activity]

    _ensure_polling(backend)

    # Auto-read unread terminals
    previews: dict[str, str] = {}
    for t in unread:
        out = backend.capture(t.id)
        if out is not None:
            _track(t.id, out)
            tail = out.strip().split("\n")
            previews[t.id] = "\n".join(tail[-_PREVIEW_LINES:])

    aliases = _read_aliases()
    lines: list[str] = []
    for i, t in enumerate(terminals):
        ago = _format_ago(t.id)
        alias = aliases.get(str(t.id), "")
        alias_str = f" ({alias})" if alias else ""
        act_str = f" — {ago}" if ago else ""
        title_str = f" - {t.title}" if t.title and t.title != t.name else ""
        line = f"Terminal {i}{alias_str} [{t.id}]: {t.name}{title_str}{act_str}"
        preview = previews.get(t.id)
        if preview:
            line += f"\n  Content:\n  " + "\n  ".join(preview.split("\n"))
        lines.append(line)

    if previews:
        lines.insert(0, f"[auto-read {len(previews)} terminal(s)]")

    return "\n".join(lines)


def tool_read_terminal(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Capture the current visible text from a terminal (last N lines)."""
    backend: _Backend = ctx["backend"]
    tid: str = args["terminal_id"]
    out = backend.capture(tid)
    if out is None:
        return f"[Error capturing terminal {tid}]"
    _track(tid, out)
    # Tail to _CAPTURE_LINES for the LLM
    lines = out.split("\n")
    if len(lines) > _CAPTURE_LINES:
        lines = lines[-_CAPTURE_LINES:]
    return _truncate("\n".join(lines))


def tool_send_command(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Send keystrokes to a terminal via the command queue."""
    backend: _Backend = ctx["backend"]
    queue_cls = ctx["queue_cls"]
    tid: str = args["terminal_id"]
    keys: str = args["keys"]
    description: str = args["description"]
    stable_seconds: float = args.get("stable_seconds", 5.0)

    q = queue_cls.get(tid, backend)
    notify = ctx.get("notify")
    chat_id = ctx.get("chat_id")

    def on_complete(result: str) -> None:
        if notify and chat_id is not None:
            notify(chat_id, _truncate(result))

    q.enqueue(keys, description, stable_seconds=stable_seconds,
              on_complete=on_complete)

    # Notify user directly — don't let the LLM rephrase system events
    if notify and chat_id is not None:
        notify(chat_id, f"Command sent to terminal {tid}: {description}")

    return (
        f"Command queued for terminal {tid}: {description}\n"
        "The user has already been notified. "
        "A completion notification will be sent automatically by the system. "
        "Do not tell the user you will notify them or follow up."
    )


def tool_create_terminal(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Open a new terminal window/pane."""
    backend: _Backend = ctx["backend"]
    result = backend.create()
    if result is None:
        return "Failed to create terminal."
    # Wait for the OS to register the new window before listing
    time.sleep(1.0)
    terminals = backend.list()
    aliases = _read_aliases()
    lines = [f"New terminal created. {len(terminals)} terminal(s) now available:"]
    for i, t in enumerate(terminals):
        alias = aliases.get(str(t.id), "")
        alias_str = f" ({alias})" if alias else ""
        lines.append(f"Terminal {i}{alias_str} [{t.id}]: {t.name}")
    return "\n".join(lines)


def tool_rename_terminal(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Set a custom alias for a terminal."""
    tid: str = args["terminal_id"]
    name: str = args["name"]
    aliases = _read_aliases()
    aliases[tid] = name
    _write_aliases(aliases)
    return f"Terminal '{tid}' renamed to '{name}'."


def tool_start_bg_task(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Start a repeating background task that polls a terminal for a substring."""
    tasks: dict[int, Any] = ctx["tasks"]
    tasks_lock: threading.Lock = ctx["tasks_lock"]
    chat_id: int = ctx["chat_id"]
    backend: _Backend = ctx["backend"]
    notify = ctx["notify"]
    next_id: Callable[[], int] = ctx["next_task_id"]

    from onecmd.manager.tasks import BackgroundTask

    task = BackgroundTask(
        task_id=next_id(),
        chat_id=chat_id,
        terminal_id=args["terminal_id"],
        send_text=args.get("send_text", ""),
        check_contains=args["check_contains"],
        description=args["description"],
        poll_interval=args.get("poll_interval", 10),
        max_iterations=args.get("max_iterations", 100),
        backend=backend,
        notify=notify,
    )
    with tasks_lock:
        tasks[task.task_id] = task
    task.start()

    notify(chat_id, f"Background task #{task.task_id} started: {task.description}")

    return (
        f"Background task #{task.task_id} started: {task.description} "
        f"(polling every {task.poll_interval}s, "
        f"max {task.max_iterations} iterations)\n"
        "The user has already been notified. "
        "The system will notify them automatically on completion."
    )


def tool_start_smart_task(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Start an LLM-judged background task that monitors a terminal."""
    tasks: dict[int, Any] = ctx["tasks"]
    tasks_lock: threading.Lock = ctx["tasks_lock"]
    backend: _Backend = ctx["backend"]
    notify = ctx["notify"]

    from onecmd.manager.tasks import SmartTask

    task = SmartTask(
        terminal_id=args["terminal_id"],
        backend=backend,
        notify=notify,
        chat_fn=ctx["chat_fn"],
        format_results_fn=ctx["format_results_fn"],
        prompt=args["prompt"],
        send_text=args.get("send_text", ""),
        poll_interval=args.get("poll_interval", 10),
        max_iterations=args.get("max_iterations", 100),
    )
    with tasks_lock:
        tasks[task.task_id] = task
    task.start()

    notify(ctx["chat_id"], f"Smart task #{task.task_id} started: {task.description}")

    return (
        f"Smart task #{task.task_id} started: {task.description} "
        f"(polling every {task.poll_interval}s, "
        f"max {task.max_iterations} iterations)\n"
        "The user has already been notified. "
        "The system will notify them automatically on completion."
    )


def tool_list_tasks(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """List all background tasks and their status."""
    tasks: dict[int, Any] = ctx["tasks"]
    tasks_lock: threading.Lock = ctx["tasks_lock"]
    with tasks_lock:
        task_list = list(tasks.values())
    if not task_list:
        return "No active tasks."
    lines: list[str] = []
    for t in task_list:
        elapsed = int(time.time() - t.started_at)
        iters = getattr(t, "iterations", 0)
        lines.append(
            f"Task #{t.task_id} [{t.status}]: {t.description} "
            f"({iters} iters, {elapsed}s)"
        )
    return "\n".join(lines)


def tool_cancel_task(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Cancel a running background task by ID."""
    tasks: dict[int, Any] = ctx["tasks"]
    tasks_lock: threading.Lock = ctx["tasks_lock"]
    task_id: int = args["task_id"]
    with tasks_lock:
        task = tasks.get(task_id)
    if task is None:
        return f"Task #{task_id} not found."
    task.cancel()
    return f"Task #{task_id} cancelled."


def tool_save_memory(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Save a memory to long-term storage."""
    from onecmd.manager import memory

    chat_id: int = ctx["chat_id"]
    content: str = args["content"]
    category: str = args.get("category", "general")
    mid = memory.save(chat_id, content, category)
    return f"Memory #{mid} saved ({category})."


def tool_delete_memory(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Delete a memory by ID."""
    from onecmd.manager import memory

    chat_id: int = ctx["chat_id"]
    memory_id: int = args["memory_id"]
    if memory.delete(chat_id, memory_id):
        return f"Memory #{memory_id} deleted."
    return f"Memory #{memory_id} not found."


def tool_list_memories(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """List all saved memories for this chat."""
    from onecmd.manager import memory

    chat_id: int = ctx["chat_id"]
    memories = memory.list_for_chat(chat_id)
    if not memories:
        return "No memories saved yet. Use save_memory to store something."
    lines = [f"Memories ({len(memories)}):"]
    for mid, content, category in memories:
        lines.append(f"  #{mid} [{category}] {content}")
    return "\n".join(lines)


def tool_send_message_to_user(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Send an intermediate message to the user immediately."""
    from onecmd.manager.agent import strip_markdown

    notify = ctx["notify"]
    chat_id: int = ctx["chat_id"]
    text: str = strip_markdown(args["text"])
    notify(chat_id, text)
    return "Message sent."


def tool_read_sop(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Read the current SOP and custom rules."""
    from onecmd.manager.sop import ensure_sop
    content = ensure_sop()
    if not content:
        return "No SOP configured."
    return content


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def tool_restart_service(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Restart a system service (delegated to service_restart module)."""
    from onecmd.manager.service_restart import tool_restart_service as _impl
    return _impl(ctx, args)


def tool_detect_crashes(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Scan terminal for crash/failure patterns."""
    from onecmd.manager.service_restart import tool_detect_crashes as _impl
    return _impl(ctx, args)


def tool_check_resources(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Check system resource usage (disk, RAM, CPU)."""
    from onecmd.manager.resource_monitor import tool_check_resources as _impl
    return _impl(ctx, args)


TOOL_REGISTRY: dict[str, ToolFunc] = {
    "list_terminals": tool_list_terminals,
    "create_terminal": tool_create_terminal,
    "read_terminal": tool_read_terminal,
    "send_command": tool_send_command,
    "rename_terminal": tool_rename_terminal,
    "start_background_task": tool_start_bg_task,
    "start_smart_task": tool_start_smart_task,
    "list_tasks": tool_list_tasks,
    "cancel_task": tool_cancel_task,
    "save_memory": tool_save_memory,
    "delete_memory": tool_delete_memory,
    "list_memories": tool_list_memories,
    "read_sop": tool_read_sop,
    "send_message_to_user": tool_send_message_to_user,
    "restart_service": tool_restart_service,
    "detect_crashes": tool_detect_crashes,
    "check_resources": tool_check_resources,
}


# ---------------------------------------------------------------------------
# Tool schemas (LLM tool-use format)
# ---------------------------------------------------------------------------

def _props(**kw: str) -> dict[str, dict[str, str]]:
    """Build a properties dict: _props(name="desc") -> {"name": {"type":"string","description":"desc"}}."""
    return {k: {"type": "string", "description": v} for k, v in kw.items()}

def _schema(props: dict[str, Any] | None = None, required: list[str] | None = None,
            **extra: Any) -> dict[str, Any]:
    s: dict[str, Any] = {"type": "object", "properties": props or {}, "required": required or []}
    if extra:
        for k, v in extra.items():
            # Merge extra property definitions into properties
            s["properties"][k] = v
    return s

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": "list_terminals",
     "description": "List all available terminal sessions with their IDs, names, and titles.",
     "input_schema": _schema()},
    {"name": "create_terminal",
     "description": "Open a new terminal window (macOS) or tmux pane (Linux). "
        "Returns the updated terminal list after creation.",
     "input_schema": _schema()},
    {"name": "rename_terminal",
     "description": "Give a terminal a custom name for easy identification. "
        "When you first list terminals and see generic names like 'iTerm2 - bash', "
        "proactively suggest descriptive names based on what's running in each terminal "
        "(e.g. 'dev-server', 'db-console', 'build-logs').",
     "input_schema": _schema(
         _props(terminal_id="Terminal ID from list_terminals",
                name="Custom name for the terminal (e.g. 'dev-server', 'logs')"),
         ["terminal_id", "name"])},
    {"name": "read_terminal",
     "description": "Read/capture the current visible text from a terminal. "
        "Use the terminal's 'id' field (e.g., '%0' on tmux or '12399' on macOS).",
     "input_schema": _schema(
         _props(terminal_id="Terminal ID from list_terminals"), ["terminal_id"])},
    {"name": "send_command",
     "description": "Send keystrokes to a terminal and watch for the output to finish "
        "in the background. Returns immediately — the terminal is monitored asynchronously "
        "and the user is notified when the output stabilizes (stops changing). "
        "Use this for ALL commands. You do NOT need to guess if a command is fast or slow.",
     "input_schema": _schema(
         _props(terminal_id="Terminal ID from list_terminals",
                keys="Text/keystrokes to send. Use \\n for Enter, \\t for Tab.",
                description="Brief description of what this command does (shown in notification)"),
         ["terminal_id", "keys", "description"],
         stable_seconds={"type": "number",
                         "description": "Seconds output must be unchanged to be done. Default 5."})},
    {"name": "start_background_task",
     "description": "Start a repeating background task that periodically sends input to a terminal "
        "and checks for a specific text condition. Useful for polling tasks like "
        "'keep asking until it says yes'. Monitors output stability after each send.",
     "input_schema": _schema(
         _props(terminal_id="Terminal ID to operate on",
                send_text="Text to send each iteration (empty to just monitor)",
                check_contains="Substring to look for. Task completes when found.",
                description="Human-readable description of what this task does"),
         ["terminal_id", "check_contains", "description"],
         poll_interval={"type": "integer", "description": "Seconds between checks (default 10, min 5)"},
         max_iterations={"type": "integer", "description": "Max iterations before giving up (default 100)"})},
    {"name": "start_smart_task",
     "description": "Start an LLM-judged background task that monitors a terminal. "
        "Each iteration captures before/after snapshots and an LLM decides: continue, "
        "notify user, send keystrokes, or mark complete. Use for complex goals that "
        "can't be a simple substring check.",
     "input_schema": _schema(
         _props(terminal_id="Terminal ID to monitor",
                prompt="Natural language description of what to monitor/achieve",
                send_text="Optional text to send each iteration before capturing"),
         ["terminal_id", "prompt"],
         poll_interval={"type": "integer", "description": "Seconds between iterations (default 10, min 5)"},
         max_iterations={"type": "integer", "description": "Max iterations before giving up (default 100)"})},
    {"name": "list_tasks",
     "description": "List all background tasks and their status.",
     "input_schema": _schema()},
    {"name": "cancel_task",
     "description": "Cancel a running background task by its ID.",
     "input_schema": _schema(
         {"task_id": {"type": "integer", "description": "Task ID to cancel"}},
         ["task_id"])},
    {"name": "save_memory",
     "description": "Save something to long-term memory. Persists across restarts. "
        "Use when the user says 'remember', 'always', 'never', 'from now on', "
        "or when you learn important facts about their environment or preferences.",
     "input_schema": _schema(
         {"content": {"type": "string", "description": "What to remember (be specific and concise)"},
          "category": {"type": "string", "enum": ["rule", "knowledge", "preference"],
                       "description": "rule=directives, knowledge=facts, preference=style"}},
         ["content", "category"])},
    {"name": "delete_memory",
     "description": "Delete a memory by its ID. Use when outdated or user asks to forget.",
     "input_schema": _schema(
         {"memory_id": {"type": "integer", "description": "Memory ID to delete"}},
         ["memory_id"])},
    {"name": "list_memories",
     "description": "List all saved memories for this user. Use when the user asks "
        "'what do you remember', 'show my memories', or 'what's in your memory'.",
     "input_schema": _schema()},
    {"name": "read_sop",
     "description": "Read the current SOP (Standard Operating Procedure) and custom rules. "
        "Use when the user asks about rules, SOP, configuration, or how you're configured.",
     "input_schema": _schema()},
    {"name": "send_message_to_user",
     "description": "Send a message to the user immediately, without waiting for the final response. "
        "Use this to deliver results one by one (e.g. when summarizing multiple terminals, "
        "send each summary as a separate message as soon as it's ready). "
        "You can call this multiple times. Your final response is still sent as usual.",
     "input_schema": _schema(
         _props(text="The message text to send to the user (plain text, no markdown)"),
         ["text"])},
    {"name": "restart_service",
     "description": "Restart a system service (systemd on Linux, brew services on macOS). "
        "ALWAYS ask the user for confirmation first unless the service is in the auto-restart list "
        "in custom_rules.md. Set confirmed=true only after user approval.",
     "input_schema": _schema(
         {"service_name": {"type": "string",
                           "description": "Name of the service to restart (e.g. 'nginx', 'redis')"},
          "confirmed": {"type": "boolean",
                        "description": "Whether the user confirmed. Must be true to proceed."}},
         ["service_name"])},
    {"name": "detect_crashes",
     "description": "Scan a terminal's output for crash/failure patterns (segfault, OOM, "
        "connection refused, service failures, etc.). Use when you suspect a crash or "
        "the user reports issues.",
     "input_schema": _schema(
         _props(terminal_id="Terminal ID to scan for crash patterns"),
         ["terminal_id"])},
    {"name": "check_resources",
     "description": "Check current system resource usage (disk space, RAM, CPU load). "
        "Reports any values exceeding alert thresholds (disk >90%, RAM >95%, "
        "load > 2x CPU count).",
     "input_schema": _schema()},
]


# ---------------------------------------------------------------------------
# Pre-dispatch hooks — auto-fix preconditions instead of blocking
# ---------------------------------------------------------------------------
#
# Each hook is (check_fn, fix_fn):
#   check_fn(tool_args, ctx) -> bool  — True if precondition met
#   fix_fn(tool_args, ctx) -> str|None — fix it silently, return context or None
#
# Hooks run before the tool. If check fails, fix runs automatically.
# The fix result (e.g. terminal content) is prepended to the tool result
# so the LLM sees the context it would have gotten from read_terminal.

# Tools that require a terminal to have been read first
_REQUIRES_TERMINAL_READ: set[str] = {
    "send_command",
    "start_background_task",
    "start_smart_task",
    "restart_service",
}


def _ensure_terminal_read(tool_args: dict[str, Any],
                          ctx: dict[str, Any]) -> str | None:
    """Auto-read a terminal if it hasn't been read yet. Returns content or None."""
    tid = tool_args.get("terminal_id", "")
    if not tid:
        return None
    with _activity_lock:
        if tid in _activity:
            return None  # already read
    # Auto-read — same logic as tool_read_terminal
    backend: _Backend = ctx["backend"]
    out = backend.capture(tid)
    if out is None:
        return None
    _track(tid, out)
    lines = out.split("\n")
    if len(lines) > _CAPTURE_LINES:
        lines = lines[-_CAPTURE_LINES:]
    content = "\n".join(lines)
    logger.info("Auto-read terminal %s before %s", tid,
                tool_args.get("_tool_name", "tool"))
    return content


# ---------------------------------------------------------------------------
# Dangerous command detection
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-\w*[rf]|-\w*[rf]\w*)\b"), "rm with -r or -f flag"),
    (re.compile(r"\brm\s+-rf\b"), "rm -rf"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE), "DROP statement"),
    (re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE), "DELETE FROM"),
    (re.compile(r"\bTRUNCATE\b", re.IGNORECASE), "TRUNCATE"),
    (re.compile(r"\bkill\s+-9\b"), "kill -9"),
    (re.compile(r"\bkillall\b"), "killall"),
    (re.compile(r"\bgit\s+push\s+.*--force\b"), "git push --force"),
    (re.compile(r"\bgit\s+push\s+-f\b"), "git push -f"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f"), "git clean -f"),
    (re.compile(r"\bmkfs\b"), "mkfs (format disk)"),
    (re.compile(r"\bdd\s+.*of=/dev/"), "dd to device"),
    (re.compile(r">\s*/dev/sd[a-z]"), "overwrite block device"),
    (re.compile(r"\bshutdown\b"), "shutdown"),
    (re.compile(r"\breboot\b"), "reboot"),
    (re.compile(r"\bsystemctl\s+(stop|disable)\b"), "systemctl stop/disable"),
]


def _check_dangerous(keys: str) -> str | None:
    """Return a warning string if keys contain a dangerous pattern, else None."""
    for pattern, label in _DANGEROUS_PATTERNS:
        if pattern.search(keys):
            return label
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch(tool_name: str, tool_args: dict[str, Any],
             ctx: dict[str, Any]) -> str:
    """Look up *tool_name* in the registry, run pre-hooks, and execute.

    Pre-hooks auto-fix preconditions (e.g. reading a terminal before
    sending to it). Context from hooks is prepended to the result.
    """
    func = TOOL_REGISTRY.get(tool_name)
    if func is None:
        return f"Unknown tool: {tool_name}"

    # Auto-fix preconditions
    context_prefix = ""
    if tool_name in _REQUIRES_TERMINAL_READ:
        auto_content = _ensure_terminal_read(tool_args, ctx)
        if auto_content:
            context_prefix = (
                f"[auto-read terminal {tool_args.get('terminal_id', '')}]\n"
                f"{_truncate(auto_content, 2000)}\n\n"
            )

    # Dangerous command guard — block and ask for user confirmation
    if tool_name == "send_command":
        keys = tool_args.get("keys", "")
        danger = _check_dangerous(keys)
        if danger:
            desc = tool_args.get("description", "")
            logger.warning("Blocked dangerous command (%s): %s", danger, keys)
            return (
                f"[BLOCKED — dangerous command detected: {danger}]\n"
                f"Command: {keys.strip()}\n"
                f"Description: {desc}\n"
                "Ask the user for explicit confirmation before retrying."
            )

    try:
        result = func(ctx, tool_args)
    except Exception as e:
        logger.error("Tool %s error: %s", tool_name, e, exc_info=True)
        result = f"[Error in {tool_name}: {e}]"
    return _truncate(context_prefix + result)
