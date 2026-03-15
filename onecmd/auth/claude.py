"""Helpers for Claude Code OAuth credentials.

Sources (in priority order):
  1) CLAUDE_CODE_OAUTH_TOKEN env var
  2) ~/.onecmd/auth.json  key "claudeAiOauth"
  3) macOS Keychain  service "Claude Code-credentials"

Schema in auth.json:
{
  "claudeAiOauth": {
    "type": "oauth",
    "accessToken": "sk-ant-oat01-...",
    "refreshToken": "sk-ant-ort01-...",
    "expiresAt": 1773540563635,   // ms since epoch
    "scopes": ["user:inference", ...],
    "tokenType": "Bearer"
  }
}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

AUTH_ENV = "ONECMD_AUTH_FILE"
DEFAULT_AUTH_PATH = Path.home() / ".onecmd" / "auth.json"
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
REFRESH_URL = "https://console.anthropic.com/api/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
KEYCHAIN_SERVICE = "Claude Code-credentials"


class ClaudeAuthError(RuntimeError):
    pass


def auth_file_path() -> Path:
    raw = os.environ.get(AUTH_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_AUTH_PATH


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ClaudeAuthError(f"invalid auth file: {path}: {e}") from e


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def _read_keychain() -> dict[str, Any] | None:
    """Read Claude Code credentials from macOS Keychain."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        if not raw:
            return None
        data = json.loads(raw)
        return data.get("claudeAiOauth") if isinstance(data, dict) else None
    except Exception:
        return None


def load_claude_credentials() -> dict[str, Any] | None:
    """Load Claude OAuth creds. Priority: env var > auth.json > Keychain."""
    # 1) Direct env var
    env_token = os.environ.get(OAUTH_TOKEN_ENV)
    if env_token:
        return {
            "accessToken": env_token,
            "refreshToken": "",
            "expiresAt": 0,
            "tokenType": "Bearer",
        }

    # 2) auth.json
    data = _load_json(auth_file_path())
    creds = data.get("claudeAiOauth")
    if isinstance(creds, dict) and creds.get("accessToken"):
        return creds

    # 3) macOS Keychain
    kc = _read_keychain()
    if isinstance(kc, dict) and kc.get("accessToken"):
        return kc

    return None


def save_claude_credentials(creds: dict[str, Any]) -> None:
    path = auth_file_path()
    data = _load_json(path)
    data["claudeAiOauth"] = creds
    _save_json(path, data)


def is_expiring(creds: dict[str, Any], skew_seconds: int = 120) -> bool:
    exp = int(creds.get("expiresAt") or 0)
    if exp <= 0:
        return False
    # expiresAt is in milliseconds
    exp_secs = exp / 1000.0 if exp > 1e12 else float(exp)
    return time.time() >= (exp_secs - skew_seconds)


def refresh_claude_credentials(creds: dict[str, Any]) -> dict[str, Any]:
    refresh_token = creds.get("refreshToken")
    if not refresh_token:
        raise ClaudeAuthError("missing refreshToken — re-login with `claude` CLI")

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    data = json.dumps(body).encode("utf-8")
    req = request.Request(
        REFRESH_URL,
        data=data,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        raise ClaudeAuthError(f"Claude token refresh failed ({e.code}): {msg}") from e
    except Exception as e:
        raise ClaudeAuthError(f"Claude token refresh failed: {e}") from e

    access_token = payload.get("access_token")
    if not access_token:
        raise ClaudeAuthError("refresh response missing access_token")

    updated = dict(creds)
    updated["accessToken"] = access_token
    if payload.get("refresh_token"):
        updated["refreshToken"] = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if expires_in is not None:
        updated["expiresAt"] = int(time.time() + int(expires_in)) * 1000
    if payload.get("scope"):
        updated["scopes"] = payload["scope"].split()

    save_claude_credentials(updated)
    return updated


def ensure_fresh_claude_credentials(skew_seconds: int = 120) -> dict[str, Any]:
    creds = load_claude_credentials()
    if not creds:
        raise ClaudeAuthError(
            "no Claude credentials found — run `claude` CLI to login, "
            "or set CLAUDE_CODE_OAUTH_TOKEN"
        )
    if is_expiring(creds, skew_seconds=skew_seconds):
        creds = refresh_claude_credentials(creds)
    if not creds.get("accessToken"):
        raise ClaudeAuthError("missing accessToken")
    return creds


def has_claude_credentials() -> bool:
    try:
        return bool(load_claude_credentials())
    except Exception:
        return False
