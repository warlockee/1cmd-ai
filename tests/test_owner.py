"""Tests for onecmd.auth.owner — owner registration and verification."""

from __future__ import annotations

import pytest

from onecmd.auth.owner import OWNER_KEY, check_owner
from onecmd.store import Store


@pytest.fixture()
def store(tmp_path):
    """Yield a Store backed by a temporary SQLite file."""
    s = Store(str(tmp_path / "owner_test.sqlite"))
    yield s
    s.close()


class TestCheckOwner:
    def test_first_user_becomes_owner(self, store):
        is_owner, just_registered = check_owner(store, 111)
        assert is_owner is True
        assert just_registered is True

    def test_first_user_is_persisted(self, store):
        check_owner(store, 111)
        assert store.get(OWNER_KEY) == "111"

    def test_second_user_is_not_owner(self, store):
        check_owner(store, 111)
        is_owner, just_registered = check_owner(store, 222)
        assert is_owner is False
        assert just_registered is False

    def test_owner_recognized_on_subsequent_calls(self, store):
        check_owner(store, 111)
        is_owner, just_registered = check_owner(store, 111)
        assert is_owner is True
        assert just_registered is False

    def test_owner_recognized_after_many_calls(self, store):
        check_owner(store, 42)
        for _ in range(10):
            is_owner, just_registered = check_owner(store, 42)
            assert is_owner is True
            assert just_registered is False

    def test_multiple_non_owners_rejected(self, store):
        check_owner(store, 1)
        for uid in [2, 3, 4, 5]:
            is_owner, _ = check_owner(store, uid)
            assert is_owner is False

    def test_just_registered_only_true_once(self, store):
        _, reg1 = check_owner(store, 100)
        _, reg2 = check_owner(store, 100)
        _, reg3 = check_owner(store, 200)
        assert reg1 is True
        assert reg2 is False
        assert reg3 is False
