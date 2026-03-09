"""Tests for manager/memory.py — Long-term memory (SQLite)."""

from __future__ import annotations

import os
import sqlite3
import threading
from unittest.mock import patch

import pytest

import onecmd.manager.memory as memory


# ---------------------------------------------------------------------------
# Fixtures — use a temp DB for every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    """Redirect memory module to a temporary SQLite database."""
    db_path = str(tmp_path / "test_memory.sqlite")
    with patch.object(memory, "_DB_PATH", db_path):
        yield db_path


# ---------------------------------------------------------------------------
# save / list / delete roundtrip
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_save_and_list_for_chat(self):
        row_id = memory.save(1, "remember this", "knowledge")
        assert row_id is not None
        entries = memory.list_for_chat(1)
        assert len(entries) == 1
        assert entries[0] == (row_id, "remember this", "knowledge")

    def test_delete_removes_entry(self):
        row_id = memory.save(1, "to delete")
        assert memory.delete(1, row_id) is True
        assert memory.list_for_chat(1) == []

    def test_delete_wrong_chat_returns_false(self):
        row_id = memory.save(1, "scoped")
        assert memory.delete(999, row_id) is False
        assert len(memory.list_for_chat(1)) == 1

    def test_delete_nonexistent_returns_false(self):
        assert memory.delete(1, 99999) is False


# ---------------------------------------------------------------------------
# 100 memory limit
# ---------------------------------------------------------------------------


class TestPruning:
    def test_oldest_pruned_at_limit(self):
        chat_id = 42
        ids = []
        for i in range(105):
            rid = memory.save(chat_id, f"mem-{i}", "general")
            ids.append(rid)

        entries = memory.list_for_chat(chat_id)
        assert len(entries) == 100
        # The oldest 5 should have been pruned
        remaining_ids = {e[0] for e in entries}
        for old_id in ids[:5]:
            assert old_id not in remaining_ids
        # The newest should still be present
        for new_id in ids[-100:]:
            assert new_id in remaining_ids


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class TestCategories:
    @pytest.mark.parametrize("category", ["rule", "knowledge", "preference"])
    def test_category_stored_and_returned(self, category):
        memory.save(1, f"cat-{category}", category)
        entries = memory.list_for_chat(1)
        assert entries[0][2] == category

    def test_default_category_is_general(self):
        memory.save(1, "no category")
        entries = memory.list_for_chat(1)
        assert entries[0][2] == "general"


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_saves(self):
        """Multiple threads saving simultaneously should not corrupt data."""
        errors: list[Exception] = []
        chat_id = 77

        def saver(start: int):
            try:
                for i in range(20):
                    memory.save(chat_id, f"thread-{start}-{i}", "knowledge")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=saver, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        entries = memory.list_for_chat(chat_id)
        # 5 threads * 20 saves = 100, which is exactly at the limit
        assert len(entries) == 100
