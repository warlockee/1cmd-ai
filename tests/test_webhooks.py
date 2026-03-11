"""Tests for P2.2c — deploy webhook routes."""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch

from onecmd.admin.routes_webhooks import (
    _load_hooks,
    _save_hooks,
    _verify_github_signature,
)


class TestGitHubSignature:
    def test_valid_signature(self):
        import hashlib, hmac
        secret = "mysecret"
        payload = b'{"ref":"refs/heads/main"}'
        sig = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256).hexdigest()
        assert _verify_github_signature(payload, sig, secret) is True

    def test_invalid_signature(self):
        assert _verify_github_signature(
            b"payload", "sha256=wrong", "mysecret") is False

    def test_no_secret_no_verification(self):
        # If no secret is configured, any signature is accepted
        assert _verify_github_signature(b"payload", "", "") is True

    def test_secret_but_no_signature(self):
        assert _verify_github_signature(
            b"payload", "", "mysecret") is False

    def test_wrong_prefix(self):
        assert _verify_github_signature(
            b"payload", "sha1=abc", "mysecret") is False


class TestHookPersistence:
    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "onecmd.admin.routes_webhooks.HOOKS_FILE",
            str(tmp_path / "nonexistent.json"))
        assert _load_hooks() == []

    def test_save_and_load(self, tmp_path, monkeypatch):
        hooks_file = str(tmp_path / "hooks.json")
        monkeypatch.setattr(
            "onecmd.admin.routes_webhooks.HOOKS_FILE", hooks_file)
        hooks = [{"repo": "user/repo", "branch": "main", "command": "deploy"}]
        _save_hooks(hooks)
        loaded = _load_hooks()
        assert len(loaded) == 1
        assert loaded[0]["repo"] == "user/repo"
