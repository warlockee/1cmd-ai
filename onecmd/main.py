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
    - Starts connector(s): Telegram (always), Slack (if tokens provided)
    - Starts resource monitor if LLM key is configured

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
  10. Create connector handler (unified for all platforms)
  11. Start admin panel (if --admin-port)
  12. Start Telegram connector (blocks in legacy mode, or async with Slack)
  13. If Slack tokens present, start Slack connector too
"""

from __future__ import annotations

import logging
import signal
import sys

from onecmd.config import parse_config
from onecmd.store import Store
from onecmd.auth.codex import CodexAuthError, preflight_codex_credentials
from onecmd.auth.totp import totp_setup
from onecmd.terminal.scope import detect_scope
from onecmd.terminal.backend import create_backend

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

    try:
        preflight_codex_credentials(logger=log)
    except CodexAuthError:
        pass

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

    # 9a. Start admin panel (if --admin-port is set).
    if config.admin_port:
        try:
            from onecmd.admin.server import create_app, start_admin
            admin_app = create_app(backend, config, store, router=None)
            start_admin(admin_app, port=config.admin_port)
            log.info("Admin panel: http://0.0.0.0:%d", config.admin_port)
        except Exception as exc:
            log.error("Failed to start admin panel: %s", exc, exc_info=True)

    # 10. Start connector(s).
    #
    # If only Telegram token is set, use the legacy code path (identical
    # behavior to before the connector refactor).  If Slack tokens are also
    # present, use the new multi-connector path.
    if config.has_slack:
        _run_multi_connector(config, store, backend)
    else:
        _run_telegram_only(config, store, backend)

    # Cleanup.
    store.close()
    log.info("Shutdown complete.")


def _run_telegram_only(config, store, backend) -> None:
    """Legacy code path: single Telegram bot using python-telegram-bot."""
    from onecmd.bot.handler import create_handler
    from onecmd.bot.poller import run_bot

    log.info(
        "Agent mode active: %s (skills_dir=%s)",
        getattr(config, "agent_mode", "legacy"),
        getattr(config, "skills_dir", ".onecmd/skills"),
    )
    handler = create_handler(config, store, backend)
    log.info("Starting bot (Telegram only)...")
    try:
        run_bot(config, handler)
    except Exception as exc:
        log.error("Telegram bot failed: %s", exc)
        if config.admin_port:
            log.info(
                "Admin panel still running on port %d. Press Ctrl+C to exit.",
                config.admin_port)
            import threading
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


def _run_multi_connector(config, store, backend) -> None:
    """Multi-connector path: Telegram + Slack running concurrently."""
    import asyncio
    import threading
    from onecmd.connectors.handler import create_connector_handler
    from onecmd.connectors.telegram import TelegramConnector

    msg_handler, cb_handler, register_fn = create_connector_handler(
        config, store, backend)

    async def _run_all() -> None:
        tasks = []

        # Always start Telegram
        tg = TelegramConnector(token=config.apikey)
        register_fn(tg)
        log.info("Starting Telegram connector...")
        tasks.append(asyncio.create_task(
            tg.start(msg_handler, cb_handler)))

        # Start Slack if configured
        if config.has_slack:
            try:
                from onecmd.connectors.slack import SlackConnector
                slack = SlackConnector(
                    bot_token=config.slack_bot_token,
                    app_token=config.slack_app_token,
                )
                register_fn(slack)
                log.info("Starting Slack connector...")
                tasks.append(asyncio.create_task(
                    slack.start(msg_handler, cb_handler)))
            except Exception as exc:
                log.error("Failed to start Slack connector: %s", exc)

        log.info("Running %d connector(s)...", len(tasks))

        # Wait for any task to complete (or fail)
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED)

        # If one connector dies, log it but keep running
        for task in done:
            if task.exception():
                log.error("Connector failed: %s", task.exception())

        # If there are still running connectors, wait for them
        if pending:
            await asyncio.wait(pending)

    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    except Exception as exc:
        log.error("Multi-connector error: %s", exc)
        if config.admin_port:
            log.info(
                "Admin panel still running on port %d. Press Ctrl+C to exit.",
                config.admin_port)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
