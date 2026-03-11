"""Tests for P2.2b — resource monitoring."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from onecmd.manager.resource_monitor import (
    ResourceAlert,
    check_disk,
    check_cpu_load,
    check_all_resources,
    format_resource_alert,
    ResourceMonitor,
)


class TestCheckDisk:
    @patch("onecmd.manager.resource_monitor._run_cmd")
    def test_high_disk_usage(self, mock_cmd):
        mock_cmd.return_value = (
            "Filesystem     Size  Used Avail Use% Mounted on\n"
            "/dev/sda1      100G   95G    5G  95% /\n"
            "/dev/sdb1       50G   10G   40G  20% /data\n"
        )
        alerts = check_disk()
        assert len(alerts) == 1
        assert alerts[0].resource == "disk"
        assert "95%" in alerts[0].current_value

    @patch("onecmd.manager.resource_monitor._run_cmd")
    def test_normal_disk(self, mock_cmd):
        mock_cmd.return_value = (
            "Filesystem     Size  Used Avail Use% Mounted on\n"
            "/dev/sda1      100G   50G   50G  50% /\n"
        )
        alerts = check_disk()
        assert len(alerts) == 0

    @patch("onecmd.manager.resource_monitor._run_cmd")
    def test_empty_output(self, mock_cmd):
        mock_cmd.return_value = ""
        alerts = check_disk()
        assert len(alerts) == 0


class TestCheckCPU:
    @patch("os.cpu_count", return_value=4)
    @patch("os.getloadavg", return_value=(10.0, 8.0, 6.0))
    def test_high_load(self, mock_load, mock_cpu):
        alerts = check_cpu_load()
        assert len(alerts) == 1
        assert alerts[0].resource == "cpu"

    @patch("os.cpu_count", return_value=8)
    @patch("os.getloadavg", return_value=(2.0, 1.5, 1.0))
    def test_normal_load(self, mock_load, mock_cpu):
        alerts = check_cpu_load()
        assert len(alerts) == 0


class TestFormatAlert:
    def test_empty(self):
        assert format_resource_alert([]) == ""

    def test_single_alert(self):
        alerts = [ResourceAlert("disk", "95%", "90%", "/dev/sda1 on /")]
        msg = format_resource_alert(alerts)
        assert "DISK" in msg
        assert "95%" in msg
        assert "investigate" in msg.lower()

    def test_multiple_alerts(self):
        alerts = [
            ResourceAlert("disk", "95%", "90%", "/dev/sda1 on /"),
            ResourceAlert("cpu", "load 10.0", ">8", ""),
        ]
        msg = format_resource_alert(alerts)
        assert "DISK" in msg
        assert "CPU" in msg


class TestResourceMonitor:
    def test_start_stop(self):
        notify = MagicMock()
        monitor = ResourceMonitor(notify, chat_id=123, interval=5)
        monitor.start()
        assert monitor.running
        monitor.stop()

    def test_double_start(self):
        notify = MagicMock()
        monitor = ResourceMonitor(notify, chat_id=123, interval=5)
        monitor.start()
        monitor.start()  # Should be a no-op
        assert monitor.running
        monitor.stop()
