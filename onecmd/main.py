"""Orchestrator entry point for onecmd.

Calling spec:
  Inputs:  sys.argv (CLI args), environment variables
  Outputs: None (runs forever until SIGTERM/SIGINT)
  Side effects:
    - Configures logging
    - Opens SQLite database
    - Sets up TOTP authentication (generates secret on first run)
    - Detects terminal scope (tmux session or macOS parent PID)
    - Creates validated backend
    - Starts Telegram long-polling loop

Recipe:
  1. Ignore SIGPIPE (match C version behaviour)
  2. logging.basicConfig(...)
  3. config = parse_config(sys.argv)
  4. Set DEBUG level if --verbose
  5. store = Store(config.dbfile)
  6. totp_setup(store, config.enable_otp, config.weak_security, config.otp_timeout)
  7. scope = detect_scope()
  8. backend = create_backend(scope, config.danger_mode)
  9. Log scope/backend info
  10. handler = create_handler(config, store, backend)
  11. run_bot(config, handler)
"""

from __future__ import annotations

import logging
import signal
import sys

from onecmd.config import parse_config
from onecmd.store import Store
from onecmd.auth.totp import totp_setup
from onecmd.terminal.scope import detect_scope
from onecmd.terminal.backend import create_backend
from onecmd.bot.handler import create_handler
from onecmd.bot.poller import run_bot

log = logging.getLogger(__name__)


def main() -> None:
    """Entry point: wire everything together and run the bot."""

    # 1. Ignore SIGPIPE so writes to broken pipes raise EPIPE, not crash.
    #    Matches the C version: signal(SIGPIPE, SIG_IGN).
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    # 2. Configure logging.
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        level=logging.INFO,
        datefmt="%H:%M:%S",
    )

    # 3. Parse configuration from CLI args + env vars + apikey.txt.
    config = parse_config(sys.argv)

    # 4. Verbose flag sets logging to DEBUG.
    if config.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.debug("Verbose logging enabled")

    # 5. Open the SQLite store.
    store = Store(config.dbfile)
    log.info("Database: %s", config.dbfile)

    # 6. TOTP setup: check/generate secret before starting the bot.
    otp_active = totp_setup(
        store,
        enable_otp=config.enable_otp,
        weak_security=config.weak_security,
        otp_timeout=config.otp_timeout,
    )
    if config.weak_security:
        log.warning("OTP authentication disabled (--use-weak-security)")
    elif otp_active:
        log.info("OTP authentication active (timeout=%ds)", config.otp_timeout)

    # 7. Detect terminal scope (tmux session or macOS parent PID).
    scope = detect_scope()

    # 8. Create validated, scoped backend.
    backend = create_backend(scope, config.danger_mode)
    if scope.use_tmux:
        log.info("Backend: tmux (session=%s)", scope.session_name)
    elif scope.parent_pid:
        log.info("Backend: macOS native (pid=%d)", scope.parent_pid)
    else:
        log.info("Backend: macOS native (no scope)")

    if config.danger_mode:
        log.warning("DANGER MODE: all windows visible")

    # 9. Create the message handler (dispatches commands, enforces auth).
    handler = create_handler(config, store, backend)

    # 10. Start Telegram long-polling (blocks until SIGTERM/SIGINT).
    log.info("Starting bot...")
    run_bot(config, handler)

    # Cleanup.
    store.close()
    log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
