#!/usr/bin/env python3
"""
onecmd-mgr — LLM Manager Agent for onecmd

Reads user messages from stdin (JSON lines from onecmd bot),
processes them with Claude API (tool-use for terminal operations),
and writes responses to stdout (JSON lines back to onecmd bot).

Background tasks run autonomously and send notifications via stdout.
"""
from __future__ import annotations

import json
import logging
import sys
import threading

from utils import API_KEY
from manager import Manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Main loop: read JSON lines from stdin
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if not API_KEY:
        logger.error("No LLM API key set. Manager will not start.")
        sys.exit(1)

    mgr: Manager = Manager()
    logger.info("Ready.")

    # Read JSON lines from stdin (sent by onecmd)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg: dict = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON from stdin: %s (line: %s)", e, line[:200])
            continue

        # Validate incoming message fields
        try:
            chat_id: int = int(msg.get("chat_id", 0))
        except (TypeError, ValueError):
            logger.warning("Invalid chat_id in message: %s", msg.get("chat_id"))
            continue

        text: str | None = msg.get("text")

        if chat_id <= 0:
            logger.debug("Skipping message with invalid chat_id: %d", chat_id)
            continue
        if not isinstance(text, str) or not text.strip():
            logger.debug("Skipping message with empty or non-string text from chat %d", chat_id)
            continue

        # Process in a thread (serialized per-chat by internal lock)
        threading.Thread(
            target=mgr.process_message,
            args=(chat_id, text),
            daemon=True
        ).start()


if __name__ == "__main__":
    main()
