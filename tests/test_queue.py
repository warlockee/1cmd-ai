"""Tests for manager/queue.py — TerminalQueue and stability detection."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

import onecmd.manager.queue as queue_mod
from onecmd.manager.queue import (
    TerminalQueue,
    _output_diff_ratio,
    _wait_stable,
)


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


class FakeBackend:
    """Backend mock that returns scripted capture outputs."""

    def __init__(self, outputs: list[str] | None = None):
        self._outputs = list(outputs) if outputs else []
        self._idx = 0
        self.sent_keys: list[tuple[str, str]] = []

    def capture(self, term_id: str) -> str | None:
        if self._idx < len(self._outputs):
            out = self._outputs[self._idx]
            self._idx += 1
            return out
        return self._outputs[-1] if self._outputs else ""

    def send_keys(self, term_id: str, text: str) -> bool:
        self.sent_keys.append((term_id, text))
        return True


class FailSendBackend(FakeBackend):
    def send_keys(self, term_id: str, text: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Stability detection
# ---------------------------------------------------------------------------


class TestWaitStable:
    def test_returns_when_output_unchanged(self):
        """Output stays the same for stable_seconds -> returns."""
        backend = FakeBackend(["line1\nline2"] * 20)
        result = _wait_stable(backend, "t1", stable_seconds=0.1)
        assert "line1" in result

    def test_respects_max_wait(self):
        """If output keeps changing, eventually hits max_wait."""
        counter = {"n": 0}
        backend = MagicMock()

        def changing_capture(term_id):
            counter["n"] += 1
            return f"output-{counter['n']}"

        backend.capture = changing_capture
        start = time.time()
        _wait_stable(backend, "t1", stable_seconds=5.0, max_wait=0.5)
        elapsed = time.time() - start
        assert elapsed < 2.0  # Should not hang

    def test_cancel_event_stops_early(self):
        backend = FakeBackend(["a", "b", "c", "d"])
        cancel = threading.Event()
        cancel.set()
        result = _wait_stable(backend, "t1", cancel_event=cancel)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Diff ratio
# ---------------------------------------------------------------------------


class TestDiffRatio:
    def test_identical_output_is_zero(self):
        text = "line1\nline2\nline3"
        assert _output_diff_ratio(text, text) == 0.0

    def test_completely_different_is_one(self):
        before = "\n".join(f"a{i}" for i in range(20))
        after = "\n".join(f"b{i}" for i in range(20))
        assert _output_diff_ratio(before, after) == 1.0

    def test_small_change_below_threshold(self):
        lines = [f"line{i}" for i in range(20)]
        before = "\n".join(lines)
        lines[-1] = "lineXX"
        after = "\n".join(lines)
        ratio = _output_diff_ratio(before, after)
        assert ratio < 0.1  # small change

    def test_empty_inputs(self):
        assert _output_diff_ratio("", "") == 0.0


# ---------------------------------------------------------------------------
# TerminalQueue — sends keys and calls on_complete
# ---------------------------------------------------------------------------


class TestTerminalQueue:
    @pytest.fixture(autouse=True)
    def _clear_queues(self):
        """Ensure singleton cache is clean between tests."""
        TerminalQueue._queues.clear()
        yield
        TerminalQueue._queues.clear()

    def test_sends_keys_and_waits_for_stability(self):
        # Baseline capture, then changed output that stabilizes
        outputs = ["$ ", "$ ls\nfile1\nfile2"] + ["$ ls\nfile1\nfile2"] * 30
        backend = FakeBackend(outputs)
        done = threading.Event()
        result_holder: list[str] = []

        def on_complete(result: str):
            result_holder.append(result)
            done.set()

        q = TerminalQueue("t1", backend)
        q.enqueue("ls\n", "list files", stable_seconds=0.1, on_complete=on_complete)
        done.wait(timeout=10)

        assert len(result_holder) == 1
        assert ("t1", "ls\n") in backend.sent_keys

    def test_no_change_reports_error(self):
        """If diff ratio < 5%, reports 'barely changed'."""
        # Use a multi-line output where baseline and final are nearly identical
        # (diff ratio < 5%) but different enough to not trigger pending-command
        # detection.
        base = "\n".join(f"line{i}" for i in range(20)) + "\n$ "
        final = "\n".join(f"line{i}" for i in range(20)) + "\n$ \n"
        outputs = [base, final] + [final] * 30
        backend = FakeBackend(outputs)
        done = threading.Event()
        result_holder: list[str] = []

        def on_complete(result: str):
            result_holder.append(result)
            done.set()

        q = TerminalQueue("t2", backend)
        q.enqueue("noop\n", "no-op", stable_seconds=0.1, on_complete=on_complete)
        done.wait(timeout=10)

        assert len(result_holder) == 1
        assert "barely changed" in result_holder[0].lower()

    def test_one_command_at_a_time(self):
        """Commands run sequentially — second starts after first finishes."""
        order: list[str] = []
        all_done = threading.Event()

        # Each command gets its own stable output sequence
        call_count = {"n": 0}
        backend = MagicMock()

        def fake_capture(term_id):
            return f"output-{call_count['n']}"

        def fake_send(term_id, text):
            call_count["n"] += 1
            return True

        backend.capture = fake_capture
        backend.send_keys = fake_send

        def cb1(result):
            order.append("first")

        def cb2(result):
            order.append("second")
            all_done.set()

        q = TerminalQueue("t3", backend)
        q.enqueue("cmd1\n", "first", stable_seconds=0.1, on_complete=cb1)
        q.enqueue("cmd2\n", "second", stable_seconds=0.1, on_complete=cb2)
        all_done.wait(timeout=10)

        assert order == ["first", "second"]

    def test_send_failure_calls_on_complete_with_error(self):
        backend = FailSendBackend(["baseline"])
        done = threading.Event()
        result_holder: list[str] = []

        def on_complete(result: str):
            result_holder.append(result)
            done.set()

        q = TerminalQueue("t4", backend)
        q.enqueue("fail\n", "fail cmd", on_complete=on_complete)
        done.wait(timeout=5)

        assert len(result_holder) == 1
        assert "Error" in result_holder[0] or "error" in result_holder[0].lower()

    def test_singleton_get(self):
        backend = FakeBackend()
        q1 = TerminalQueue.get("same", backend)
        q2 = TerminalQueue.get("same", backend)
        assert q1 is q2
