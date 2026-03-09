"""
onecmd mgr — Agent SOP (Standard Operating Procedure) generation and loading.

Generates .onecmd/agent_sop.md on first run. This file tells the AI manager
how to detect and recover stuck terminal sessions using Smart Diff.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SOP_DIR: str = ".onecmd"
SOP_FILE: str = "agent_sop.md"

_SOP_CONTENT: str = """\
# OneCmd Agent SOP: Stuck Terminal Detection

## Smart Diff — Detecting Stuck Sessions

A terminal is "stuck" when a command was sent but never executed, or a process
hangs without producing output. Use this procedure to detect and recover.

### Detection Steps

1. **Capture BEFORE snapshot** — read the terminal output.
2. **Send a probe command** — a harmless command that always produces output:
   - Shell prompt visible: send `echo __PROBE_OK__`
   - Interactive program (python, node REPL): send the equivalent print statement
   - Unknown state: send a single Enter keystroke
3. **Wait for stability** (5-10 seconds).
4. **Capture AFTER snapshot** — read the terminal output again.
5. **Compare**: if AFTER contains the probe output (e.g. `__PROBE_OK__`),
   the terminal is responsive. If AFTER is identical to BEFORE, the terminal
   is stuck.

### Recovery Actions

When a terminal is stuck:

- **Pending input not submitted**: The command text is visible but the prompt
  hasn't advanced. Send Enter to submit it.
- **Process hanging**: Output stopped mid-stream. Send Ctrl+C to interrupt,
  then verify with a probe.
- **Waiting for user input** (confirm/password prompt): Read the prompt text
  and follow the RISK CLASSIFICATION rules before responding.
- **Unresponsive**: If Ctrl+C doesn't work, try Ctrl+Z to suspend, then
  `kill %1` to terminate the background job.

### When to Run Stuck Detection

- After sending a command via send_command, if no "command completed"
  notification arrives within 2x the expected stable_seconds.
- When a user reports "nothing is happening" or "it's stuck".
- During smart_task iterations when BEFORE and AFTER snapshots are identical
  for 3+ consecutive checks.

### Rules

- Never send destructive probes (no `rm`, `kill -9`, `DROP TABLE`).
- Always read the terminal BEFORE sending anything — the current state
  may contain important information (error messages, prompts).
- If the terminal shows a password prompt, do NOT probe — ask the user.
- Log stuck detection events so they appear in .health reports.
"""


def ensure_sop() -> str:
    """Ensure the SOP file exists. Returns the SOP content."""
    sop_path: Path = Path(SOP_DIR) / SOP_FILE

    if not sop_path.exists():
        try:
            sop_path.parent.mkdir(parents=True, exist_ok=True)
            sop_path.write_text(_SOP_CONTENT)
            logger.info("Generated agent SOP at %s", sop_path)
        except OSError as e:
            logger.warning("Could not write SOP file: %s", e)
            return _SOP_CONTENT

    try:
        return sop_path.read_text()
    except OSError:
        return _SOP_CONTENT
