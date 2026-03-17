"""Tests for onecmd.main startup wiring."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from onecmd import main


def test_main_logs_codex_preflight_error_and_continues():
    config = SimpleNamespace(
        verbose=False,
        dbfile=":memory:",
        enable_otp=False,
        weak_security=False,
        otp_timeout=300,
        danger_mode=False,
        admin_port=None,
        has_slack=False,
    )
    store = mock.Mock()
    backend = mock.Mock()
    scope = SimpleNamespace(use_tmux=False, parent_pid=None, session_name=None)

    with mock.patch("onecmd.main.parse_config", return_value=config), \
         mock.patch("onecmd.main.Store", return_value=store), \
         mock.patch("onecmd.main.totp_setup", return_value=False), \
         mock.patch("onecmd.main.preflight_codex_credentials", side_effect=main.CodexAuthError("broken codex auth")) as preflight, \
         mock.patch("onecmd.main.detect_scope", return_value=scope), \
         mock.patch("onecmd.main.create_backend", return_value=backend), \
         mock.patch("onecmd.main._run_telegram_only") as run_telegram, \
         mock.patch("onecmd.main.log") as log:
        main.main()

    preflight.assert_called_once_with(logger=log)
    run_telegram.assert_called_once_with(config, store, backend)
    log.info.assert_any_call("Database: %s", config.dbfile)
    log.info.assert_any_call("Backend: macOS native (no scope)")
