"""P2.2a — Auto-restart crashed services detection and tools.

Adds pattern matching to detect crashed services in terminal output and
a tool for the AI manager to restart them (with user confirmation).

Calling spec:
  Inputs:  terminal output text
  Outputs: list of detected crash patterns
  Side effects: none (detection only); restart tool sends keys via backend

Patterns detected:
  - Process exited / segfault / killed / OOM
  - Connection refused / address already in use
  - systemd service failures
  - Docker container exits
  - Python/Node/Java unhandled exceptions with process death

Integration:
  - CRASH_PATTERNS: list of compiled regexes for crash detection
  - detect_crashes(text) -> list of CrashEvent
  - Tool schemas and implementations added to TOOL_REGISTRY
"""

from __future__ import annotations

import logging
import platform
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Crash detection patterns
# ---------------------------------------------------------------------------

@dataclass
class CrashEvent:
    """A detected crash or failure in terminal output."""
    pattern_name: str
    matched_text: str
    severity: str  # "warning", "error", "critical"


# ---------------------------------------------------------------------------
# Pattern loading — user override > bundled default > hardcoded fallback
# ---------------------------------------------------------------------------

_DEFAULT_PATTERNS_FILE = Path(__file__).parent / "default_crash_patterns.md"
_USER_PATTERNS_FILE = Path(".onecmd/crash_patterns.md")


def _parse_patterns_file(text: str) -> list[tuple[str, re.Pattern, str]]:
    """Parse a crash patterns file into compiled patterns.

    Format: ``name | regex | severity``
    The name is before the first ``|`` and severity after the last ``|``.
    Everything in between is the regex (which may contain ``|`` for alternation).
    """
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Split on first and last pipe to allow | inside the regex
        first_pipe = line.find("|")
        last_pipe = line.rfind("|")
        if first_pipe == -1 or first_pipe == last_pipe:
            continue
        name = line[:first_pipe].strip()
        regex_str = line[first_pipe + 1:last_pipe].strip()
        severity = line[last_pipe + 1:].strip()
        if name and regex_str and severity:
            try:
                patterns.append((name, re.compile(regex_str, re.IGNORECASE), severity))
            except re.error as e:
                logger.warning("Invalid crash pattern '%s': %s", name, e)
    return patterns


def _load_patterns() -> list[tuple[str, re.Pattern, str]]:
    """Load crash patterns: user file > bundled default > hardcoded fallback."""
    for path in (_USER_PATTERNS_FILE, _DEFAULT_PATTERNS_FILE):
        if path.exists():
            try:
                patterns = _parse_patterns_file(path.read_text())
                if patterns:
                    return patterns
            except OSError:
                continue
    logger.warning("Crash pattern files not found, using hardcoded fallback")
    return [
        ("segfault", re.compile(r"segfault|segmentation fault|sigsegv|signal 11", re.IGNORECASE), "critical"),
        ("oom_killed", re.compile(r"oom.?kill|out of memory|cannot allocate memory", re.IGNORECASE), "critical"),
    ]


_PATTERNS: list[tuple[str, re.Pattern, str]] = _load_patterns()


def detect_crashes(text: str) -> list[CrashEvent]:
    """Scan terminal text for crash/failure patterns.

    Returns a list of CrashEvent objects, one per unique pattern match.
    Only checks the last 50 lines to avoid false positives from old output.
    """
    lines = text.strip().split("\n")
    recent = "\n".join(lines[-50:])

    events: list[CrashEvent] = []
    seen: set[str] = set()

    for name, pattern, severity in _PATTERNS:
        match = pattern.search(recent)
        if match and name not in seen:
            seen.add(name)
            events.append(CrashEvent(
                pattern_name=name,
                matched_text=match.group(0)[:200],
                severity=severity,
            ))

    return events


def format_crash_alert(events: list[CrashEvent], terminal_name: str) -> str:
    """Format crash events into a user-friendly alert message."""
    if not events:
        return ""

    severity_icons = {
        "critical": "\u2757",  # exclamation mark
        "error": "\u26a0\ufe0f",  # warning
        "warning": "\u2139\ufe0f",  # info
    }

    lines = [f"Service issue detected in {terminal_name}:"]
    for event in events:
        icon = severity_icons.get(event.severity, "")
        lines.append(f"  {icon} {event.pattern_name}: {event.matched_text}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Restart tool for AI manager
# ---------------------------------------------------------------------------

def _detect_os() -> str:
    """Detect whether we're on Linux (systemd) or macOS (brew services)."""
    return "linux" if platform.system() == "Linux" else "macos"


def tool_restart_service(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Restart a system service.

    SAFETY: This tool always requires user confirmation unless the service
    is pre-approved in custom_rules.md.
    """
    service_name: str = args["service_name"]
    confirmed: bool = args.get("confirmed", False)

    if not confirmed:
        return (
            f"CONFIRMATION REQUIRED: Restart service '{service_name}'?\n"
            f"This will run the restart command on the system.\n"
            f"Ask the user to confirm before proceeding.\n"
            f"Call this tool again with confirmed=true after user approval."
        )

    # Check custom_rules.md for pre-approved services
    from onecmd.manager.sop import ensure_sop
    sop_content = ensure_sop()
    auto_approved = False
    if sop_content:
        import re
        # Look for lines like: "auto-restart: nginx, redis, myapp"
        match = re.search(
            r"auto[- ]restart\s*:\s*(.+)", sop_content, re.IGNORECASE)
        if match:
            approved_list = [
                s.strip().lower() for s in match.group(1).split(",")]
            if service_name.lower() in approved_list:
                auto_approved = True

    os_type = _detect_os()
    if os_type == "linux":
        cmd = f"sudo systemctl restart {service_name}"
    else:
        cmd = f"brew services restart {service_name}"

    # Use the backend to send the restart command to a terminal
    backend = ctx["backend"]
    terminals = backend.list()
    if not terminals:
        return "No terminals available to run restart command."

    # Find a suitable terminal (prefer one not running an interactive program)
    target_tid = terminals[0].id

    queue_cls = ctx["queue_cls"]
    notify = ctx.get("notify")
    chat_id = ctx.get("chat_id")

    q = queue_cls.get(target_tid, backend)

    def on_complete(result: str) -> None:
        if notify and chat_id is not None:
            notify(chat_id, f"Service restart result for '{service_name}':\n{result}")

    q.enqueue(cmd + "\n", f"Restart {service_name}",
              stable_seconds=10.0, on_complete=on_complete)

    return (
        f"Restart command queued: {cmd}\n"
        f"Running in terminal {target_tid}. "
        f"You'll be notified when it completes."
    )


def tool_detect_crashes(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Scan a terminal for crash/failure patterns."""
    backend = ctx["backend"]
    tid: str = args["terminal_id"]
    output = backend.capture(tid)
    if output is None:
        return f"Cannot read terminal {tid}."

    events = detect_crashes(output)
    if not events:
        return f"No crash patterns detected in terminal {tid}."

    lines = [f"Crash patterns detected in terminal {tid}:"]
    for e in events:
        lines.append(f"  [{e.severity}] {e.pattern_name}: {e.matched_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool schemas for AI manager integration
# ---------------------------------------------------------------------------

RESTART_TOOL_SCHEMA = {
    "name": "restart_service",
    "description": (
        "Restart a system service (systemd on Linux, brew services on macOS). "
        "ALWAYS ask the user for confirmation first unless the service is "
        "listed in auto-restart rules in custom_rules.md. "
        "Set confirmed=true only after user approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service_name": {
                "type": "string",
                "description": "Name of the service to restart (e.g. 'nginx', 'redis', 'postgres')",
            },
            "confirmed": {
                "type": "boolean",
                "description": "Whether the user has confirmed the restart. Must be true to proceed.",
            },
        },
        "required": ["service_name"],
    },
}

DETECT_CRASHES_TOOL_SCHEMA = {
    "name": "detect_crashes",
    "description": (
        "Scan a terminal's output for crash/failure patterns (segfault, OOM, "
        "connection refused, service failures, etc.). Use this when you suspect "
        "something may have crashed or the user reports issues."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "terminal_id": {
                "type": "string",
                "description": "Terminal ID to scan for crash patterns",
            },
        },
        "required": ["terminal_id"],
    },
}
