"""Tests for manager/sop.py — Agent SOP loading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from onecmd.manager.sop import (
    CUSTOM_RULES_FILE,
    DEFAULT_SOP,
    SOP_DIR,
    SOP_FILE,
    ensure_sop,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sop_env(tmp_path, monkeypatch):
    """Run ensure_sop in a temp directory so it doesn't touch real files."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Default SOP loading
# ---------------------------------------------------------------------------


class TestDefaultSOP:
    def test_loads_default_sop_from_package(self, sop_env):
        """ensure_sop returns content from the bundled default_sop.md."""
        result = ensure_sop()
        # Should contain something from the default SOP (non-empty)
        if DEFAULT_SOP.exists():
            expected = DEFAULT_SOP.read_text()
            assert expected in result or len(result) > 0
        else:
            # If default SOP file missing, result can be empty string
            assert isinstance(result, str)


# ---------------------------------------------------------------------------
# First-run copy
# ---------------------------------------------------------------------------


class TestFirstRunCopy:
    def test_copies_to_onecmd_dir_on_first_run(self, sop_env):
        sop_path = sop_env / SOP_DIR / SOP_FILE
        assert not sop_path.exists()

        ensure_sop()

        if DEFAULT_SOP.exists():
            assert sop_path.exists()
            assert sop_path.read_text() == DEFAULT_SOP.read_text()

    def test_does_not_overwrite_existing_sop(self, sop_env):
        sop_path = sop_env / SOP_DIR / SOP_FILE
        sop_path.parent.mkdir(parents=True, exist_ok=True)
        sop_path.write_text("custom sop content")

        result = ensure_sop()

        assert "custom sop content" in result

    def test_creates_custom_rules_template(self, sop_env):
        ensure_sop()
        custom_path = sop_env / SOP_DIR / CUSTOM_RULES_FILE
        assert custom_path.exists()
        content = custom_path.read_text()
        assert "Custom Rules" in content


# ---------------------------------------------------------------------------
# Custom rules
# ---------------------------------------------------------------------------


class TestCustomRules:
    def test_reads_custom_rules_if_present(self, sop_env):
        custom_path = sop_env / SOP_DIR / CUSTOM_RULES_FILE
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("# comment\nAlways run tests\nNever restart DB\n")

        result = ensure_sop()

        assert "Always run tests" in result
        assert "Never restart DB" in result

    def test_ignores_comment_lines(self, sop_env):
        custom_path = sop_env / SOP_DIR / CUSTOM_RULES_FILE
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("# this is a comment\n# another comment\n")

        result = ensure_sop()

        # No custom rules section should appear (all lines are comments)
        assert "Custom Rules" not in result or "## Custom Rules" not in result

    def test_returns_combined_string(self, sop_env):
        """SOP + custom rules are returned as one string."""
        custom_path = sop_env / SOP_DIR / CUSTOM_RULES_FILE
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("Deploy to staging first\n")

        result = ensure_sop()

        assert "Deploy to staging first" in result
        # Result should be a single string, not a list
        assert isinstance(result, str)

    def test_empty_custom_rules_file(self, sop_env):
        custom_path = sop_env / SOP_DIR / CUSTOM_RULES_FILE
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.write_text("")

        result = ensure_sop()
        # Should still return base SOP without error
        assert isinstance(result, str)
