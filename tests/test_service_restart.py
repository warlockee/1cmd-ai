"""Tests for P2.2a — crash detection and service restart tools."""

from __future__ import annotations

import pytest
from onecmd.manager.service_restart import (
    CrashEvent,
    detect_crashes,
    format_crash_alert,
)


class TestCrashDetection:
    def test_no_crashes(self):
        text = "$ ls\nfile1.txt\nfile2.txt\n$ "
        assert detect_crashes(text) == []

    def test_segfault(self):
        text = "Segmentation fault (core dumped)"
        events = detect_crashes(text)
        assert len(events) == 1
        assert events[0].pattern_name == "segfault"
        assert events[0].severity == "critical"

    def test_oom_killed(self):
        text = "kernel: Out of memory: Killed process 1234 (myapp)"
        events = detect_crashes(text)
        assert any(e.pattern_name == "oom_killed" for e in events)

    def test_connection_refused(self):
        text = "curl: (7) Failed to connect: Connection refused"
        events = detect_crashes(text)
        assert any(e.pattern_name == "connection_refused" for e in events)

    def test_systemd_failed(self):
        text = "nginx.service: Failed to start NGINX."
        events = detect_crashes(text)
        assert any(e.pattern_name == "systemd_failed" for e in events)

    def test_docker_exit(self):
        text = "container myapp exited with code 1"
        events = detect_crashes(text)
        assert any(e.pattern_name == "docker_exit" for e in events)

    def test_python_traceback(self):
        text = "Traceback (most recent call last):\n  File 'app.py', line 1"
        events = detect_crashes(text)
        assert any(e.pattern_name == "unhandled_exception" for e in events)

    def test_exit_code_nonzero(self):
        text = "Process exited with code 137"
        events = detect_crashes(text)
        assert any(e.pattern_name == "process_exited" for e in events)

    def test_exit_code_zero_not_flagged(self):
        text = "exit code 0"
        events = detect_crashes(text)
        # Code 0 should not match — our regex requires [1-9]
        process_events = [e for e in events if e.pattern_name == "process_exited"]
        assert len(process_events) == 0

    def test_only_recent_lines_checked(self):
        # Old crash in line 1 should be ignored if >50 lines away
        old_crash = "Segmentation fault\n"
        filler = "normal output\n" * 55
        text = old_crash + filler
        events = detect_crashes(text)
        assert len(events) == 0

    def test_dedup_same_pattern(self):
        text = ("Connection refused\n" * 5
                + "curl: Connection refused again")
        events = detect_crashes(text)
        conn_events = [e for e in events
                       if e.pattern_name == "connection_refused"]
        assert len(conn_events) == 1

    def test_multiple_patterns(self):
        text = ("Segmentation fault\n"
                "Connection refused\n")
        events = detect_crashes(text)
        names = {e.pattern_name for e in events}
        assert "segfault" in names
        assert "connection_refused" in names


class TestFormatCrashAlert:
    def test_empty(self):
        assert format_crash_alert([], "term1") == ""

    def test_format(self):
        events = [CrashEvent("segfault", "Segfault detected", "critical")]
        msg = format_crash_alert(events, "deploy-server")
        assert "deploy-server" in msg
        assert "segfault" in msg

    def test_address_in_use(self):
        text = "Error: Address already in use"
        events = detect_crashes(text)
        assert any(e.pattern_name == "address_in_use" for e in events)
