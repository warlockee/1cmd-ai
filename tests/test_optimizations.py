"""Tests for optimization changes: caching, token guards, memory caps.

Covers:
  1. Skills caching (skills.py) — see test_skills.py for full coverage
  2. Memory save-time content cap (memory.py)
  3. Memory prompt assembly limits (agent.py)
  4. Agent prompt file caching (agent.py)
  5. Dir fingerprint helper (skills.py)
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import onecmd.manager.memory as memory
from onecmd.manager.skills import (
    _dir_fingerprint,
    _skills_cache,
    ensure_skills,
    invalidate_cache,
)
from onecmd.manager.agent import (
    _build_system_prompt,
    _load_system_prompt,
    _prompt_cache,
    _MAX_PROMPT_MEMORIES,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def skills_env(tmp_path, monkeypatch):
    """Run skills functions in a temp directory with a clean cache."""
    monkeypatch.chdir(tmp_path)
    _skills_cache.clear()
    yield tmp_path
    _skills_cache.clear()


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    """Ensure agent prompt cache is clean for every test."""
    _prompt_cache.clear()
    yield
    _prompt_cache.clear()


@pytest.fixture(autouse=True)
def _tmp_memory_db(tmp_path):
    """Redirect memory module to a temporary SQLite database."""
    db_path = str(tmp_path / "test_memory.sqlite")
    with patch.object(memory, "_DB_PATH", db_path):
        yield db_path


# ===========================================================================
# 1. Skills caching tests
# ===========================================================================


class TestSkillsCaching:
    """mtime-based caching in ensure_skills()."""

    def test_returns_cached_on_second_call(self, skills_env):
        """Second call returns same string without re-reading files."""
        first = ensure_skills()
        second = ensure_skills()
        assert first == second
        assert "skills" in _skills_cache

    def test_invalidate_cache_forces_reread(self, skills_env):
        """invalidate_cache() clears the cache dict."""
        ensure_skills()
        assert "skills" in _skills_cache

        invalidate_cache()
        assert "skills" not in _skills_cache


# ===========================================================================
# 2. Memory save-time content cap
# ===========================================================================


class TestMemorySaveTimeCap:
    """_MAX_CONTENT_CHARS enforcement in memory.save()."""

    def test_short_content_saved_as_is(self):
        """Content shorter than cap is stored unchanged."""
        content = "Short memory"
        row_id = memory.save(1, content, "knowledge")
        entries = memory.list_for_chat(1)
        assert entries[0][1] == content

    def test_long_content_truncated_with_ellipsis(self):
        """Content longer than 500 chars is truncated with '...' suffix."""
        content = "A" * 600
        row_id = memory.save(1, content, "knowledge")
        entries = memory.list_for_chat(1)
        saved = entries[0][1]
        assert saved.endswith("...")
        assert len(saved) <= 503  # 500 chars + "..."

    def test_exactly_500_chars_not_truncated(self):
        """Content of exactly 500 chars should NOT be truncated."""
        content = "B" * 500
        row_id = memory.save(1, content, "general")
        entries = memory.list_for_chat(1)
        saved = entries[0][1]
        assert saved == content
        assert "..." not in saved

    def test_501_chars_truncated(self):
        """Content of 501 chars SHOULD be truncated."""
        content = "C" * 501
        row_id = memory.save(1, content, "general")
        entries = memory.list_for_chat(1)
        saved = entries[0][1]
        assert saved.endswith("...")
        assert len(saved) <= 503


# ===========================================================================
# 3. Memory prompt assembly limits
# ===========================================================================


class TestMemoryPromptAssembly:
    """_MAX_PROMPT_MEMORIES limit in _build_system_prompt()."""

    def _make_memories(self, n: int) -> list[tuple[int, str, str]]:
        """Create a list of n fake memory tuples (id, content, category).

        Uses zero-padded IDs so 'mem_002' is never a substring of 'mem_020'.
        """
        return [(i, f"mem_{i:04d}", "knowledge") for i in range(1, n + 1)]

    def test_fewer_than_limit_all_included(self):
        """With <30 memories, all are included in the prompt."""
        mems = self._make_memories(10)
        prompt = _build_system_prompt(memories=mems)
        for _, content, _ in mems:
            assert content in prompt
        # No "older memories stored" message
        assert "older memories stored" not in prompt

    def test_exactly_at_limit_all_included(self):
        """With exactly 30 memories, all are included (boundary)."""
        mems = self._make_memories(30)
        prompt = _build_system_prompt(memories=mems)
        for _, content, _ in mems:
            assert content in prompt
        assert "older memories stored" not in prompt

    def test_over_limit_oldest_omitted(self):
        """With >30 memories, only 30 most recent are included."""
        mems = self._make_memories(50)
        prompt = _build_system_prompt(memories=mems)

        # The newest 30 (ids 21-50) should be present
        for mid, content, _ in mems[-30:]:
            assert content in prompt, f"{content} should be in prompt"

        # The oldest 20 (ids 1-20) should NOT be present
        for mid, content, _ in mems[:20]:
            assert content not in prompt, f"{content} should NOT be in prompt"

    def test_omitted_count_message_shown(self):
        """When memories are omitted, the count message is included."""
        mems = self._make_memories(35)
        prompt = _build_system_prompt(memories=mems)
        assert "5 older memories stored" in prompt

    def test_omitted_count_correct_math(self):
        """Omitted count = total - _MAX_PROMPT_MEMORIES."""
        mems = self._make_memories(45)
        prompt = _build_system_prompt(memories=mems)
        expected_omitted = 45 - _MAX_PROMPT_MEMORIES
        assert f"{expected_omitted} older memories stored" in prompt

    def test_empty_memories_no_section(self):
        """With empty or None memories, no memories section at all."""
        prompt1 = _build_system_prompt(memories=None)
        prompt2 = _build_system_prompt(memories=[])
        assert "YOUR MEMORIES" not in prompt1
        assert "YOUR MEMORIES" not in prompt2


# ===========================================================================
# 4. Agent prompt file caching
# ===========================================================================


class TestAgentPromptCaching:
    """mtime-based caching in _load_system_prompt()."""

    def test_caches_by_mtime(self, tmp_path):
        """After first read, prompt is cached and returned from cache."""
        prompt_file = tmp_path / "test_prompt.md"
        prompt_file.write_text("Hello from test prompt")

        with patch("onecmd.manager.agent._USER_PROMPT_FILE", prompt_file), \
             patch("onecmd.manager.agent._DEFAULT_PROMPT_FILE", tmp_path / "nonexistent.md"):
            first = _load_system_prompt()
            assert first == "Hello from test prompt"
            # Should be cached now
            assert str(prompt_file) in _prompt_cache

            second = _load_system_prompt()
            assert second == first

    def test_returns_cached_on_second_call(self, tmp_path):
        """Two sequential calls return the same value from cache."""
        prompt_file = tmp_path / "cached_prompt.md"
        prompt_file.write_text("Cached content")

        with patch("onecmd.manager.agent._USER_PROMPT_FILE", prompt_file), \
             patch("onecmd.manager.agent._DEFAULT_PROMPT_FILE", tmp_path / "nope.md"):
            first = _load_system_prompt()
            second = _load_system_prompt()
            assert first == second == "Cached content"

    def test_rereads_on_file_modification(self, tmp_path):
        """When file mtime changes, cache is invalidated and file is re-read."""
        prompt_file = tmp_path / "changing_prompt.md"
        prompt_file.write_text("Version 1")

        with patch("onecmd.manager.agent._USER_PROMPT_FILE", prompt_file), \
             patch("onecmd.manager.agent._DEFAULT_PROMPT_FILE", tmp_path / "nope.md"):
            first = _load_system_prompt()
            assert first == "Version 1"

            # Modify the file and bump mtime
            prompt_file.write_text("Version 2")
            new_mtime = prompt_file.stat().st_mtime + 2.0
            os.utime(prompt_file, (new_mtime, new_mtime))

            second = _load_system_prompt()
            assert second == "Version 2"

    def test_falls_back_to_default_when_user_file_missing(self, tmp_path):
        """When user prompt doesn't exist, falls back to default prompt."""
        default_file = tmp_path / "default_prompt.md"
        default_file.write_text("I am the default prompt")

        with patch("onecmd.manager.agent._USER_PROMPT_FILE", tmp_path / "nonexistent.md"), \
             patch("onecmd.manager.agent._DEFAULT_PROMPT_FILE", default_file):
            result = _load_system_prompt()
            assert result == "I am the default prompt"

    def test_falls_back_to_hardcoded_when_both_missing(self, tmp_path):
        """When both files are missing, returns hardcoded fallback."""
        with patch("onecmd.manager.agent._USER_PROMPT_FILE", tmp_path / "no1.md"), \
             patch("onecmd.manager.agent._DEFAULT_PROMPT_FILE", tmp_path / "no2.md"):
            result = _load_system_prompt()
            assert "onecmd AI manager" in result

    def test_skips_empty_user_file(self, tmp_path):
        """Empty user file is skipped -> falls back to default."""
        user_file = tmp_path / "empty_user.md"
        user_file.write_text("")
        default_file = tmp_path / "default.md"
        default_file.write_text("Default works")

        with patch("onecmd.manager.agent._USER_PROMPT_FILE", user_file), \
             patch("onecmd.manager.agent._DEFAULT_PROMPT_FILE", default_file):
            result = _load_system_prompt()
            assert result == "Default works"


# ===========================================================================
# 5. Dir fingerprint helper
# ===========================================================================


class TestDirFingerprint:
    """_dir_fingerprint helper in skills.py."""

    def test_empty_dir_returns_zero_file_count(self, tmp_path):
        """Empty dir has fingerprint = 0.0 (count of 0 files)."""
        fp = _dir_fingerprint(tmp_path)
        assert fp == 0.0

    def test_fingerprint_changes_with_new_file(self, tmp_path):
        """Adding a .md file changes the fingerprint."""
        fp1 = _dir_fingerprint(tmp_path)
        (tmp_path / "new.md").write_text("hello")
        fp2 = _dir_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_fingerprint_changes_with_file_modification(self, tmp_path):
        """Modifying a file's mtime changes the fingerprint."""
        f = tmp_path / "doc.md"
        f.write_text("original")
        fp1 = _dir_fingerprint(tmp_path)

        # Bump mtime
        new_mtime = f.stat().st_mtime + 5.0
        os.utime(f, (new_mtime, new_mtime))
        fp2 = _dir_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_fingerprint_changes_with_file_deletion(self, tmp_path):
        """Deleting a .md file changes the fingerprint."""
        f = tmp_path / "gone.md"
        f.write_text("bye")
        fp1 = _dir_fingerprint(tmp_path)

        f.unlink()
        fp2 = _dir_fingerprint(tmp_path)
        assert fp1 != fp2

    def test_nonexistent_dir_returns_zero(self, tmp_path):
        """Non-existent directory returns 0.0."""
        fp = _dir_fingerprint(tmp_path / "nonexistent")
        assert fp == 0.0

    def test_json_files_included(self, tmp_path):
        """JSON files (like SKILL.json) affect fingerprint."""
        fp1 = _dir_fingerprint(tmp_path)
        (tmp_path / "SKILL.json").write_text("{}")
        fp2 = _dir_fingerprint(tmp_path)
        assert fp1 != fp2
