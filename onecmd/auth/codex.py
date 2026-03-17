"""Helpers for OpenAI Codex OAuth credentials stored in local auth.json.

Default auth file: ~/.onecmd/auth.json
Schema:
{
  "openai-codex": {
    "type": "oauth",
    "access_token": "...",
    "refresh_token": "...",
    "id_token": "...",
    "account_id": "...",
    "expires_at": 1760000000,
    "token_type": "Bearer"
  }
}
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib import error, request

AUTH_ENV = "ONECMD_AUTH_FILE"
DEFAULT_AUTH_PATH = Path.home() / ".onecmd" / "auth.json"
CLIENT_ID_ENV = "OPENAI_CODEX_CLIENT_ID"
REFRESH_URL_ENV = "OPENAI_CODEX_TOKEN_URL"
TOKEN_ENV = "OPENAI_CODEX_TOKEN"
DEFAULT_REFRESH_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class CodexAuthError(RuntimeError):
    pass


def auth_file_path() -> Path:
    raw = os.environ.get(AUTH_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_AUTH_PATH


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover
        raise CodexAuthError(f"invalid auth file: {path}: {e}") from e


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def decode_account_id_from_jwt(access_token: str) -> str | None:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        auth = data.get("https://api.openai.com/auth", {})
        acc = auth.get("chatgpt_account_id")
        return str(acc) if acc else None
    except Exception:
        return None


def load_codex_credentials() -> dict[str, Any] | None:
    # Highest priority: explicit env vars
    env_token = os.environ.get(TOKEN_ENV)
    env_acc = os.environ.get("OPENAI_CODEX_ACCOUNT_ID")
    if env_token:
        return {
            "type": "oauth",
            "access_token": env_token,
            "account_id": env_acc or decode_account_id_from_jwt(env_token) or "",
            "refresh_token": os.environ.get("OPENAI_CODEX_REFRESH_TOKEN", ""),
            "id_token": os.environ.get("OPENAI_CODEX_ID_TOKEN", ""),
            "expires_at": int(os.environ.get("OPENAI_CODEX_EXPIRES_AT", "0") or "0"),
            "token_type": "Bearer",
        }

    data = _load_json(auth_file_path())
    creds = data.get("openai-codex")
    if isinstance(creds, dict):
        return creds
    return None


def save_codex_credentials(creds: dict[str, Any]) -> None:
    path = auth_file_path()
    data = _load_json(path)
    data["openai-codex"] = creds
    _save_json(path, data)


def is_expiring(creds: dict[str, Any], skew_seconds: int = 120) -> bool:
    exp = int(creds.get("expires_at") or 0)
    if exp <= 0:
        return False
    return time.time() >= (exp - skew_seconds)


def resolve_codex_client_id(creds: dict[str, Any] | None = None) -> str:
    env_client_id = (os.environ.get(CLIENT_ID_ENV) or "").strip()
    if env_client_id:
        return env_client_id

    creds_client_id = str((creds or {}).get("client_id") or "").strip()
    if creds_client_id:
        return creds_client_id

    return DEFAULT_CLIENT_ID


def refresh_codex_credentials(creds: dict[str, Any]) -> dict[str, Any]:
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise CodexAuthError(
            "missing refresh_token — re-login with `codex` or set OPENAI_CODEX_TOKEN"
        )

    url = os.environ.get(REFRESH_URL_ENV, DEFAULT_REFRESH_URL)
    client_id = resolve_codex_client_id(creds)
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    data = json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        if e.code == 400 and "client_id" in msg:
            msg = (
                f"{msg} — refresh requests must include client_id "
                f"(resolved via {CLIENT_ID_ENV}, auth.json openai-codex.client_id, "
                f"or built-in fallback)"
            )
        raise CodexAuthError(f"token refresh failed ({e.code}): {msg}") from e
    except Exception as e:
        raise CodexAuthError(f"token refresh failed: {e}") from e

    access_token = payload.get("access_token")
    if not access_token:
        raise CodexAuthError("refresh response missing access_token")

    updated = dict(creds)
    updated["access_token"] = access_token
    updated["client_id"] = client_id
    if payload.get("refresh_token"):
        updated["refresh_token"] = payload["refresh_token"]
    if payload.get("id_token"):
        updated["id_token"] = payload["id_token"]
    expires_in = payload.get("expires_in")
    if expires_in is not None:
        updated["expires_at"] = int(time.time()) + int(expires_in)

    if not updated.get("account_id"):
        updated["account_id"] = decode_account_id_from_jwt(access_token) or ""

    save_codex_credentials(updated)
    return updated


def preflight_codex_credentials(
    *, logger: logging.Logger | None = None, skew_seconds: int = 120
) -> None:
    if os.environ.get(TOKEN_ENV):
        return

    creds = load_codex_credentials()
    if not creds:
        return

    try:
        ensure_fresh_codex_credentials(skew_seconds=skew_seconds)
    except CodexAuthError as exc:
        msg = (
            f"Codex OAuth preflight failed: {exc}. "
            "Re-login with `codex` or set OPENAI_CODEX_TOKEN. "
            f"Refresh client_id lookup order: {CLIENT_ID_ENV}, "
            "auth.json openai-codex.client_id, built-in fallback."
        )
        if logger is not None:
            logger.error(msg)
        raise CodexAuthError(msg) from exc


def ensure_fresh_codex_credentials(skew_seconds: int = 120) -> dict[str, Any]:
    creds = load_codex_credentials()
    if not creds:
        raise CodexAuthError("no codex credentials found")
    if is_expiring(creds, skew_seconds=skew_seconds):
        creds = refresh_codex_credentials(creds)
    if not creds.get("account_id"):
        acc = decode_account_id_from_jwt(str(creds.get("access_token", "")))
        if acc:
            creds["account_id"] = acc
            save_codex_credentials(creds)
    if not creds.get("access_token"):
        raise CodexAuthError("missing access_token")
    if not creds.get("account_id"):
        raise CodexAuthError("missing account_id")
    return creds


def has_codex_credentials() -> bool:
    try:
        return bool(load_codex_credentials())
    except Exception:
        return False
