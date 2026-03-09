"""Tests for onecmd.store — CRUD, expiry, thread safety, limits."""

from __future__ import annotations

import threading
import time

import pytest

from onecmd.store import MAX_KEY_LENGTH, MAX_VALUE_LENGTH, Store


@pytest.fixture()
def store(tmp_path):
    """Yield a Store backed by a temporary SQLite file."""
    s = Store(str(tmp_path / "test.sqlite"))
    yield s
    s.close()


# ── CRUD ─────────────────────────────────────────────────────────────


class TestCRUD:
    def test_get_missing_key_returns_none(self, store):
        assert store.get("nonexistent") is None

    def test_set_and_get(self, store):
        store.set("hello", "world")
        assert store.get("hello") == "world"

    def test_set_overwrites(self, store):
        store.set("k", "v1")
        store.set("k", "v2")
        assert store.get("k") == "v2"

    def test_delete_existing(self, store):
        store.set("k", "v")
        store.delete("k")
        assert store.get("k") is None

    def test_delete_missing_is_noop(self, store):
        store.delete("nope")  # should not raise

    def test_empty_value(self, store):
        store.set("k", "")
        assert store.get("k") == ""

    def test_unicode_roundtrip(self, store):
        store.set("emoji", "hello 🌍")
        assert store.get("emoji") == "hello 🌍"

    def test_multiple_keys(self, store):
        store.set("a", "1")
        store.set("b", "2")
        assert store.get("a") == "1"
        assert store.get("b") == "2"


# ── Expiry ───────────────────────────────────────────────────────────


class TestExpiry:
    def test_non_expired_key_returned(self, store):
        future = int(time.time()) + 3600
        store.set("k", "v", expire=future)
        assert store.get("k") == "v"

    def test_expired_key_returns_none(self, store):
        past = int(time.time()) - 1
        store.set("k", "v", expire=past)
        assert store.get("k") is None

    def test_expired_key_is_deleted(self, store):
        """After get() returns None for an expired key, the row is gone."""
        past = int(time.time()) - 1
        store.set("k", "v", expire=past)
        store.get("k")  # triggers cleanup
        # Verify directly via raw SQL that the row was removed
        row = store._conn.execute(
            "SELECT * FROM KeyValue WHERE key = ?", ("k",)
        ).fetchone()
        assert row is None

    def test_zero_expire_means_no_expiry(self, store):
        store.set("k", "v", expire=0)
        assert store.get("k") == "v"

    def test_overwrite_clears_expiry(self, store):
        past = int(time.time()) - 1
        store.set("k", "v", expire=past)
        store.set("k", "v2", expire=0)
        assert store.get("k") == "v2"


# ── Thread safety ────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_writes(self, store):
        """Multiple threads writing distinct keys must not lose data."""
        errors: list[Exception] = []
        count = 50

        def writer(idx: int):
            try:
                store.set(f"key-{idx}", f"val-{idx}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        for i in range(count):
            assert store.get(f"key-{i}") == f"val-{i}"

    def test_concurrent_read_write(self, store):
        """Reads during writes must not crash."""
        store.set("shared", "initial")
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(30):
                    store.set("shared", f"v{i}")
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(30):
                    store.get("shared")  # may return any version
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == []


# ── Key / value length limits ────────────────────────────────────────


class TestLimits:
    def test_key_at_max_length_ok(self, store):
        key = "k" * MAX_KEY_LENGTH
        store.set(key, "v")
        assert store.get(key) == "v"

    def test_key_over_max_rejected(self, store):
        key = "k" * (MAX_KEY_LENGTH + 1)
        with pytest.raises(ValueError, match="Key too long"):
            store.set(key, "v")

    def test_key_over_max_rejected_on_get(self, store):
        with pytest.raises(ValueError, match="Key too long"):
            store.get("k" * (MAX_KEY_LENGTH + 1))

    def test_key_over_max_rejected_on_delete(self, store):
        with pytest.raises(ValueError, match="Key too long"):
            store.delete("k" * (MAX_KEY_LENGTH + 1))

    def test_empty_key_rejected(self, store):
        with pytest.raises(ValueError, match="non-empty"):
            store.set("", "v")

    def test_value_at_max_length_ok(self, store):
        val = "x" * MAX_VALUE_LENGTH  # ASCII: 1 byte per char
        store.set("k", val)
        assert store.get("k") == val

    def test_value_over_max_rejected(self, store):
        val = "x" * (MAX_VALUE_LENGTH + 1)
        with pytest.raises(ValueError, match="Value too large"):
            store.set("k", val)

    def test_multibyte_value_limit_is_byte_based(self, store):
        # Each CJK char is 3 bytes in UTF-8; fill to just over limit
        n_chars = (MAX_VALUE_LENGTH // 3) + 1
        val = "\u4e00" * n_chars  # U+4E00 = 3 bytes
        with pytest.raises(ValueError, match="Value too large"):
            store.set("k", val)


# ── Schema compatibility ─────────────────────────────────────────────


class TestSchemaCompat:
    def test_existing_db_with_keyvalue_table(self, tmp_path):
        """Store opens cleanly against an existing mybot.sqlite schema."""
        import sqlite3

        db_path = str(tmp_path / "mybot.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE KeyValue(expire INT, key TEXT UNIQUE, value BLOB)"
        )
        conn.execute(
            "INSERT INTO KeyValue VALUES (0, 'owner', 'alice')"
        )
        conn.commit()
        conn.close()

        s = Store(db_path)
        assert s.get("owner") == "alice"
        s.close()
