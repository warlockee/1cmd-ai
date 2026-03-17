"""Tests for onecmd.auth.codex refresh and startup preflight behavior."""

from __future__ import annotations

import json
import logging
from unittest import mock

import pytest

from onecmd.auth import codex


class _FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_refresh_uses_env_client_id(monkeypatch):
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=0):
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"access_token": "new-token", "expires_in": 60})

    monkeypatch.setenv(codex.CLIENT_ID_ENV, "env-client")
    monkeypatch.setattr(codex.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(codex, "save_codex_credentials", mock.Mock())

    updated = codex.refresh_codex_credentials(
        {"refresh_token": "rtok", "client_id": "stored-client"}
    )

    assert captured["timeout"] == 20
    assert captured["body"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rtok",
        "client_id": "env-client",
    }
    assert updated["client_id"] == "env-client"
    assert updated["access_token"] == "new-token"


def test_refresh_uses_default_client_id_when_missing(monkeypatch):
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"access_token": "new-token"})

    monkeypatch.delenv(codex.CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(codex.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(codex, "save_codex_credentials", mock.Mock())

    updated = codex.refresh_codex_credentials({"refresh_token": "rtok"})

    assert captured["body"]["client_id"] == codex.DEFAULT_CLIENT_ID
    assert updated["client_id"] == codex.DEFAULT_CLIENT_ID


def test_preflight_skips_direct_env_token(monkeypatch):
    ensure = mock.Mock()

    monkeypatch.setenv(codex.TOKEN_ENV, "direct-token")
    monkeypatch.setattr(codex, "ensure_fresh_codex_credentials", ensure)

    codex.preflight_codex_credentials()

    ensure.assert_not_called()


def test_preflight_logs_actionable_error(monkeypatch, caplog):
    monkeypatch.delenv(codex.TOKEN_ENV, raising=False)
    monkeypatch.setattr(codex, "load_codex_credentials", mock.Mock(return_value={"refresh_token": "rtok"}))
    monkeypatch.setattr(
        codex,
        "ensure_fresh_codex_credentials",
        mock.Mock(side_effect=codex.CodexAuthError("missing refresh_token")),
    )

    caplog.set_level(logging.ERROR)
    with pytest.raises(codex.CodexAuthError, match="Codex OAuth preflight failed"):
        codex.preflight_codex_credentials(logger=logging.getLogger("test.codex"))

    assert "Re-login with `codex` or set OPENAI_CODEX_TOKEN" in caplog.text
    assert codex.CLIENT_ID_ENV in caplog.text
