"""Tests for optimization changes: caching, token guards, memory caps.

Covers:
  1. mtime-based SOP caching (sop.py)
  2. Auto-pickup size limits (sop.py)
  3. Memory save-time content cap (memory.py)
  4. Memory prompt assembly limits (agent.py)
  5. Agent prompt file caching (agent.py)
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import onecmd.manager.memory as memory
from onecmd.manager import sop as sop_mod
from onecmd.manager.sop import (
    MAX_FILE_CHARS,
    MAX_EXTRAS_CHARS,
    MAX_SOP_CHARS,
    _read_extra_files,
    _dir_fingerprint,
    ensure_sop,
    invalidate_cache,
    _sop_cache,
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
def sop_env(tmp_path, monkeypatch):
    """Run SOP functions in a temp directory with a clean cache."""
    monkeypatch.chdir(tmp_path)
    _sop_cache.clear()
    yield tmp_path
    _sop_cache.clear()


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
# 1. SOP caching tests
# ===========================================================================


class TestSOPCaching:
    """mtime-based caching in ensure_sop()."""

    def test_returns_cached_on_second_call(self, sop_env):
        """Second call returns same string without re-reading files."""
        first = ensure_sop()
        second = ensure_sop()
        assert first == second
        # Cache should now have an entry
        assert "sop" in _sop_cache

    def test_cache_invalidates_on_file_modify(self, sop_env):
        """Modifying a file in .onecmd/ changes the fingerprint -> cache miss."""
        first = ensure_sop()

        sop_path = sop_env / ".onecmd" / "agent_sop.md"
        assert sop_path.exists()
        # Ensure mtime actually differs (some filesystems have 1s resolution)
        time.sleep(0.05)
        sop_path.write_text("modified SOP content")
        # Force mtime change for filesystems with coarse resolution
        new_mtime = sop_path.stat().st_mtime + 1.0
        os.utime(sop_path, (new_mtime, new_mtime))

        second = ensure_sop()
        assert "modified SOP content" in second
        assert first != second

    def test_cache_invalidates_on_file_add(self, sop_env):
        """Adding a new .md file changes fingerprint -> cache miss."""
        first = ensure_sop()
        fp_before = _sop_cache.get("sop", (None,))[0]

        new_file = sop_env / ".onecmd" / "extra_guide.md"
        new_file.write_text("Extra guidance here\n")
        # Bump mtime to be sure
        new_mtime = new_file.stat().st_mtime + 1.0
        os.utime(new_file, (new_mtime, new_mtime))

        second = ensure_sop()
        fp_after = _sop_cache.get("sop", (None,))[0]
        # Fingerprint must have changed
        assert fp_before != fp_after
        assert "Extra Guidance" in second or "Extra guidance here" in second

    def test_cache_invalidates_on_file_delete(self, sop_env):
        """Deleting a file changes fingerprint -> cache miss."""
        # Create an extra file
        sop_dir = sop_env / ".onecmd"
        sop_dir.mkdir(parents=True, exist_ok=True)
        extra = sop_dir / "deleteme.md"
        extra.write_text("will be deleted\n")

        first = ensure_sop()
        fp_before = _sop_cache["sop"][0]

        extra.unlink()

        second = ensure_sop()
        fp_after = _sop_cache["sop"][0]
        assert fp_before != fp_after

    def test_invalidate_cache_forces_reread(self, sop_env):
        """invalidate_cache() clears the cache dict."""
        ensure_sop()
        assert "sop" in _sop_cache

        invalidate_cache()
        assert "sop" not in _sop_cache


# ===========================================================================
# 2. Auto-pickup size limits
# ===========================================================================


class TestAutoPickupLimits:
    """Token consumption guards in _read_extra_files and ensure_sop."""

    def test_large_file_truncated(self, sop_env):
        """A file larger than MAX_FILE_CHARS gets truncated with marker."""
        sop_dir = sop_env / ".onecmd"
        sop_dir.mkdir(parents=True, exist_ok=True)

        big_file = sop_dir / "bigfile.md"
        # Write content larger than the cap (all non-comment lines)
        big_file.write_text("x" * (MAX_FILE_CHARS + 5000))

        extras = _read_extra_files(sop_dir)
        assert len(extras) == 1
        assert "[...truncated]" in extras[0]
        # The section (including header) should be capped near MAX_FILE_CHARS
        # (header adds a few chars, then the cap plus the marker)
        assert len(extras[0]) <= MAX_FILE_CHARS + 100  # generous margin for header + marker

    def test_total_extras_budget_stops_including(self, sop_env):
        """When total budget is exhausted, remaining files are skipped."""
        sop_dir = sop_env / ".onecmd"
        sop_dir.mkdir(parents=True, exist_ok=True)

        # Each file just under MAX_FILE_CHARS so they aren't truncated individually
        file_size = MAX_FILE_CHARS - 200  # room for the header
        num_files = (MAX_EXTRAS_CHARS // file_size) + 5  # more files than budget allows

        for i in range(num_files):
            f = sop_dir / f"zzz_extra_{i:03d}.md"
            f.write_text("a" * file_size)

        extras = _read_extra_files(sop_dir)
        total = sum(len(s) for s in extras)
        # Total should be within the budget
        assert total <= MAX_EXTRAS_CHARS
        # Some files must have been skipped
        assert len(extras) < num_files

    def test_final_sop_truncated_at_hard_cap(self, sop_env):
        """Assembled SOP exceeding MAX_SOP_CHARS is truncated with marker."""
        # Write a massive base SOP to blow past the cap
        sop_dir = sop_env / ".onecmd"
        sop_dir.mkdir(parents=True, exist_ok=True)
        sop_file = sop_dir / "agent_sop.md"
        sop_file.write_text("B" * (MAX_SOP_CHARS + 5000))

        result = ensure_sop()
        assert "[...SOP truncated]" in result
        # Length should be around MAX_SOP_CHARS + marker
        assert len(result) <= MAX_SOP_CHARS + 50

    def test_small_file_not_truncated(self, sop_env):
        """Files well under the cap are included in full."""
        sop_dir = sop_env / ".onecmd"
        sop_dir.mkdir(parents=True, exist_ok=True)

        small_file = sop_dir / "tips.md"
        small_file.write_text("Always be kind to your servers.\n")

        extras = _read_extra_files(sop_dir)
        assert len(extras) == 1
        assert "[...truncated]" not in extras[0]
        assert "Always be kind to your servers." in extras[0]


# ===========================================================================
# 3. Memory save-time content cap
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
# 4. Memory prompt assembly limits
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
# 5. Agent prompt file caching
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
# 6. Dir fingerprint helper
# ===========================================================================


class TestDirFingerprint:
    """_dir_fingerprint helper in sop.py."""

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

    def test_non_md_files_ignored(self, tmp_path):
        """Files that don't match *.md don't affect fingerprint."""
        fp1 = _dir_fingerprint(tmp_path)
        (tmp_path / "readme.txt").write_text("not markdown")
        fp2 = _dir_fingerprint(tmp_path)
        assert fp1 == fp2
