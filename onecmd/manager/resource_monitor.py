"""P2.2b — Proactive disk/RAM/CPU resource monitoring with alerts.

Runs a background thread that periodically checks system resources
and sends notifications when thresholds are exceeded.

Calling spec:
  Inputs:  notify_fn, config (thresholds from skills/custom_rules)
  Outputs: ResourceAlert objects
  Side effects: sends notifications via notify_fn when thresholds exceeded

Default thresholds:
  - Disk usage > 90%
  - RAM usage > 95%
  - Load average > CPU count x 2

Monitoring interval: 30 minutes (configurable via env var ONECMD_MONITOR_INTERVAL)
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Defaults (overridable via env vars)
DEFAULT_INTERVAL = 1800  # 30 minutes


def _env_int(name: str, default: int) -> int:
    """Read an integer from an environment variable with fallback."""
    val = os.environ.get(name)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return default


DISK_THRESHOLD = _env_int("ONECMD_DISK_THRESHOLD", 90)
RAM_THRESHOLD = _env_int("ONECMD_RAM_THRESHOLD", 95)
LOAD_MULTIPLIER = _env_int("ONECMD_LOAD_MULTIPLIER", 2)

NotifyFn = Callable[[int, str], None]


@dataclass
class ResourceAlert:
    """A resource threshold violation."""
    resource: str  # "disk", "ram", "cpu"
    current_value: str
    threshold: str
    details: str


def _run_cmd(cmd: list[str]) -> str:
    """Run a command and return its stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30)
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def check_disk() -> list[ResourceAlert]:
    """Check disk usage on all mounted filesystems."""
    alerts: list[ResourceAlert] = []
    output = _run_cmd(["df", "-h"])
    if not output:
        return alerts

    for line in output.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        # Find the percentage column (e.g. "94%")
        for part in parts:
            if part.endswith("%"):
                try:
                    pct = int(part.rstrip("%"))
                    if pct >= DISK_THRESHOLD:
                        mount = parts[-1]
                        filesystem = parts[0]
                        alerts.append(ResourceAlert(
                            resource="disk",
                            current_value=f"{pct}%",
                            threshold=f"{DISK_THRESHOLD}%",
                            details=f"{filesystem} mounted on {mount} ({parts[3]} free)",
                        ))
                except ValueError:
                    pass
                break

    return alerts


def check_ram() -> list[ResourceAlert]:
    """Check RAM usage."""
    alerts: list[ResourceAlert] = []

    if platform.system() == "Linux":
        output = _run_cmd(["free", "-m"])
        if output:
            for line in output.strip().split("\n"):
                if line.startswith("Mem:"):
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            total = int(parts[1])
                            used = int(parts[2])
                            if total > 0:
                                pct = int((used / total) * 100)
                                if pct >= RAM_THRESHOLD:
                                    alerts.append(ResourceAlert(
                                        resource="ram",
                                        current_value=f"{pct}% ({used}MB/{total}MB)",
                                        threshold=f"{RAM_THRESHOLD}%",
                                        details=f"Available: {parts[6]}MB" if len(parts) > 6 else "",
                                    ))
                        except (ValueError, ZeroDivisionError):
                            pass
    elif platform.system() == "Darwin":
        # macOS: use vm_stat
        output = _run_cmd(["vm_stat"])
        if output:
            pages: dict[str, int] = {}
            for line in output.strip().split("\n"):
                match = re.match(r"(.+?):\s+(\d+)", line)
                if match:
                    pages[match.group(1).strip()] = int(match.group(2))

            page_size = 16384  # Default on Apple Silicon, 4096 on Intel
            ps_output = _run_cmd(["sysctl", "-n", "hw.pagesize"])
            if ps_output.strip():
                try:
                    page_size = int(ps_output.strip())
                except ValueError:
                    pass

            free_pages = pages.get("Pages free", 0)
            inactive = pages.get("Pages inactive", 0)
            active = pages.get("Pages active", 0)
            wired = pages.get("Pages wired down", 0)
            compressed = pages.get("Pages occupied by compressor", 0)

            total_used = (active + wired + compressed) * page_size
            total_free = (free_pages + inactive) * page_size
            total = total_used + total_free
            if total > 0:
                pct = int((total_used / total) * 100)
                if pct >= RAM_THRESHOLD:
                    used_mb = total_used // (1024 * 1024)
                    total_mb = total // (1024 * 1024)
                    alerts.append(ResourceAlert(
                        resource="ram",
                        current_value=f"{pct}% ({used_mb}MB/{total_mb}MB)",
                        threshold=f"{RAM_THRESHOLD}%",
                        details="",
                    ))

    return alerts


def check_cpu_load() -> list[ResourceAlert]:
    """Check CPU load average."""
    alerts: list[ResourceAlert] = []
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        threshold = cpu_count * LOAD_MULTIPLIER

        if load1 >= threshold:
            alerts.append(ResourceAlert(
                resource="cpu",
                current_value=f"load {load1:.1f}/{load5:.1f}/{load15:.1f}",
                threshold=f">{threshold} (CPUs: {cpu_count})",
                details=f"1min/5min/15min averages",
            ))
    except OSError:
        pass

    return alerts


def check_all_resources() -> list[ResourceAlert]:
    """Run all resource checks and return combined alerts."""
    alerts: list[ResourceAlert] = []
    alerts.extend(check_disk())
    alerts.extend(check_ram())
    alerts.extend(check_cpu_load())
    return alerts


def format_resource_alert(alerts: list[ResourceAlert]) -> str:
    """Format resource alerts into a notification message."""
    if not alerts:
        return ""

    lines = ["\u26a0\ufe0f Resource Alert:"]
    for alert in alerts:
        lines.append(
            f"  {alert.resource.upper()}: {alert.current_value} "
            f"(threshold: {alert.threshold})")
        if alert.details:
            lines.append(f"    {alert.details}")

    lines.append("\nWant me to investigate?")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background monitoring thread
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """Background thread that periodically checks resources and alerts the user."""

    def __init__(self, notify_fn: NotifyFn, chat_id: int,
                 interval: int = DEFAULT_INTERVAL) -> None:
        self._notify = notify_fn
        self._chat_id = chat_id
        self._interval = interval
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_alerts: set[str] = set()  # Avoid repeating same alerts

    def start(self) -> None:
        """Start the monitoring thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="resource-monitor")
        self._thread.start()
        logger.info("Resource monitor started (interval=%ds)", self._interval)

    def stop(self) -> None:
        """Stop the monitoring thread."""
        self._cancel.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Resource monitor stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        # Initial delay — don't check immediately on startup
        self._cancel.wait(60)

        while not self._cancel.is_set():
            try:
                alerts = check_all_resources()
                if alerts:
                    # Only notify about new alerts (avoid spam)
                    new_alerts = []
                    current_keys: set[str] = set()
                    for a in alerts:
                        key = f"{a.resource}:{a.current_value}"
                        current_keys.add(key)
                        if key not in self._last_alerts:
                            new_alerts.append(a)

                    self._last_alerts = current_keys

                    if new_alerts:
                        msg = format_resource_alert(new_alerts)
                        if msg:
                            self._notify(self._chat_id, msg)
                else:
                    self._last_alerts.clear()
            except Exception as e:
                logger.error("Resource monitor error: %s", e)

            self._cancel.wait(self._interval)


# Singleton monitor — started when AI manager activates
_monitor: ResourceMonitor | None = None
_monitor_lock = threading.Lock()


def ensure_monitor(notify_fn: NotifyFn, chat_id: int) -> ResourceMonitor:
    """Get or create the singleton resource monitor."""
    global _monitor
    with _monitor_lock:
        if _monitor is None or not _monitor.running:
            interval = DEFAULT_INTERVAL
            env_interval = os.environ.get("ONECMD_MONITOR_INTERVAL")
            if env_interval:
                try:
                    interval = max(60, int(env_interval))
                except ValueError:
                    pass
            _monitor = ResourceMonitor(notify_fn, chat_id, interval)
            _monitor.start()
        return _monitor


# ---------------------------------------------------------------------------
# Tool for AI manager
# ---------------------------------------------------------------------------


def tool_check_resources(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    """Manually check system resources (disk, RAM, CPU load)."""
    alerts = check_all_resources()
    if not alerts:
        return "All resources within normal limits."

    lines = ["Resource status:"]
    for alert in alerts:
        lines.append(
            f"  {alert.resource.upper()}: {alert.current_value} "
            f"(threshold: {alert.threshold})")
        if alert.details:
            lines.append(f"    {alert.details}")

    return "\n".join(lines)


CHECK_RESOURCES_TOOL_SCHEMA = {
    "name": "check_resources",
    "description": (
        "Check current system resource usage (disk space, RAM, CPU load). "
        "Reports any values exceeding alert thresholds."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}
