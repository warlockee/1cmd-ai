"""Tests for terminal/backend.py — ValidatedBackend guards and create_backend factory."""

from __future__ import annotations

import importlib
import sys
import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from onecmd.terminal.backend import (
    BACKENDS,
    ValidatedBackend,
    create_backend,
)
from onecmd.terminal.scope import Scope


# ---------------------------------------------------------------------------
# Fake backend + TermInfo for testing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeTermInfo:
    id: str
    pid: int = 1
    name: str = "bash"
    title: str = ""


class FakeBackend:
    """Minimal backend that satisfies the Backend protocol."""

    def __init__(self, terms: list[FakeTermInfo] | None = None) -> None:
        self._terms = terms or []
        self.send_calls: list[tuple[str, str]] = []

    def list(self) -> list[FakeTermInfo]:
        return list(self._terms)

    def connected(self, term_id: str) -> bool:
        return any(t.id == term_id for t in self._terms)

    def capture(self, term_id: str) -> str | None:
        return f"captured:{term_id}"

    def send_keys(self, term_id: str, text: str, literal: bool = True) -> bool:
        self.send_calls.append((term_id, text))
        return True

    def free_list(self) -> None:
        self._terms = []


# ---------------------------------------------------------------------------
# ValidatedBackend: ID rejection
# ---------------------------------------------------------------------------


class TestIDValidation:
    def test_send_keys_rejects_unknown_id(self) -> None:
        vb = ValidatedBackend(FakeBackend([FakeTermInfo(id="%0")]))
        vb.list()
        with pytest.raises(ValueError, match="Unknown terminal ID"):
            vb.send_keys("%99", "hello")

    def test_capture_rejects_unknown_id(self) -> None:
        vb = ValidatedBackend(FakeBackend([FakeTermInfo(id="%0")]))
        vb.list()
        with pytest.raises(ValueError, match="Unknown terminal ID"):
            vb.capture("%99")

    def test_connected_rejects_unknown_id(self) -> None:
        vb = ValidatedBackend(FakeBackend([FakeTermInfo(id="%0")]))
        vb.list()
        with pytest.raises(ValueError, match="Unknown terminal ID"):
            vb.connected("%99")

    def test_rejects_before_any_list_call(self) -> None:
        vb = ValidatedBackend(FakeBackend([FakeTermInfo(id="%0")]))
        # No list() called — _known_ids is empty
        with pytest.raises(ValueError, match="Unknown terminal ID"):
            vb.send_keys("%0", "hello")


# ---------------------------------------------------------------------------
# ValidatedBackend: list updates known IDs
# ---------------------------------------------------------------------------


class TestListUpdatesIDs:
    def test_list_populates_known_ids(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0"), FakeTermInfo(id="%1")])
        vb = ValidatedBackend(fake)
        result = vb.list()
        assert len(result) == 2
        # Now both IDs should be accepted
        assert vb.capture("%0") == "captured:%0"
        assert vb.capture("%1") == "captured:%1"

    def test_list_replaces_previous_ids(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0")])
        vb = ValidatedBackend(fake)
        vb.list()
        # %0 valid
        vb.capture("%0")
        # Now backend only has %1
        fake._terms = [FakeTermInfo(id="%1")]
        vb.list()
        with pytest.raises(ValueError, match="Unknown terminal ID"):
            vb.capture("%0")
        assert vb.capture("%1") == "captured:%1"


# ---------------------------------------------------------------------------
# ValidatedBackend: rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_11_rapid_sends_triggers_rate_limit(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0")])
        vb = ValidatedBackend(fake)
        vb.list()

        for i in range(10):
            vb.send_keys("%0", f"cmd{i}")

        with pytest.raises(RuntimeError, match="Rate limit"):
            vb.send_keys("%0", "cmd10")

    def test_rate_limit_is_per_terminal(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0"), FakeTermInfo(id="%1")])
        vb = ValidatedBackend(fake)
        vb.list()

        for i in range(10):
            vb.send_keys("%0", f"cmd{i}")

        # %1 should still be fine
        vb.send_keys("%1", "cmd0")

    def test_rate_limit_resets_after_one_second(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0")])
        vb = ValidatedBackend(fake)
        vb.list()

        # Fill up rate limit with timestamps in the past
        past = time.time() - 2.0
        vb._send_timestamps["%0"] = [past] * 10

        # Should succeed because old timestamps are expired
        vb.send_keys("%0", "cmd")


# ---------------------------------------------------------------------------
# ValidatedBackend: text length cap
# ---------------------------------------------------------------------------


class TestTextLengthCap:
    def test_rejects_text_over_10000_chars(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0")])
        vb = ValidatedBackend(fake)
        vb.list()
        with pytest.raises(ValueError, match="Text too long"):
            vb.send_keys("%0", "x" * 10_001)

    def test_accepts_text_at_10000_chars(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0")])
        vb = ValidatedBackend(fake)
        vb.list()
        assert vb.send_keys("%0", "x" * 10_000) is True


# ---------------------------------------------------------------------------
# ValidatedBackend: free_list delegates
# ---------------------------------------------------------------------------


class TestFreeList:
    def test_free_list_delegates(self) -> None:
        fake = FakeBackend([FakeTermInfo(id="%0")])
        vb = ValidatedBackend(fake)
        vb.free_list()
        assert fake._terms == []


# ---------------------------------------------------------------------------
# create_backend: selects correct backend
# ---------------------------------------------------------------------------


class TestCreateBackend:
    def test_tmux_scope_creates_tmux_backend(self) -> None:
        scope = Scope(use_tmux=True, session_name="main")
        vb = create_backend(scope)
        assert isinstance(vb, ValidatedBackend)
        # Inner should be TmuxBackend
        from onecmd.terminal.tmux import TmuxBackend

        assert isinstance(vb._inner, TmuxBackend)
        assert vb._inner._session_name == "main"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_macos_scope_creates_macos_backend(self) -> None:
        """Test that macos scope resolves to the MacOSBackend class."""
        # MacOSBackend requires pyobjc which may not be installed in test env.
        # Mock the import to verify the factory wiring.
        mock_inner = FakeBackend([FakeTermInfo(id="42")])

        class MockMacOSBackend:
            def __init__(self, parent_pid, danger_mode):
                self.parent_pid = parent_pid
                self.danger_mode = danger_mode

        mock_mod = type(sys)("fake_macos")
        mock_mod.MacOSBackend = MockMacOSBackend  # type: ignore[attr-defined]

        scope = Scope(use_tmux=False, parent_pid=1234)
        with patch.object(importlib, "import_module", return_value=mock_mod):
            vb = create_backend(scope, danger_mode=True)
        assert isinstance(vb, ValidatedBackend)
        assert isinstance(vb._inner, MockMacOSBackend)
        assert vb._inner.parent_pid == 1234  # type: ignore[attr-defined]
        assert vb._inner.danger_mode is True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# pyobjc not imported on non-darwin
# ---------------------------------------------------------------------------


class TestPlatformConditional:
    def test_macos_backend_only_registered_on_darwin(self) -> None:
        if sys.platform == "darwin":
            assert "macos" in BACKENDS
        else:
            assert "macos" not in BACKENDS

    def test_pyobjc_not_imported_for_tmux_backend(self) -> None:
        """Creating a tmux backend should never import pyobjc modules."""
        scope = Scope(use_tmux=True, session_name="test")
        original_import = importlib.import_module
        imported_modules: list[str] = []

        def tracking_import(name: str, *args, **kwargs):
            imported_modules.append(name)
            return original_import(name, *args, **kwargs)

        with patch.object(importlib, "import_module", side_effect=tracking_import):
            create_backend(scope)

        # No pyobjc or macos module should have been imported
        for mod in imported_modules:
            assert "macos" not in mod
            assert "pyobjc" not in mod
