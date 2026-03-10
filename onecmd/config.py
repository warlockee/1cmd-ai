"""
Configuration schema and CLI parsing for onecmd.

Calling spec:
  Inputs:  sys.argv, environment variables, optional apikey.txt file
  Outputs: Config (Pydantic model, fully validated)
  Side effects: reads apikey.txt if --apikey not provided

Env vars read:
  ANTHROPIC_API_KEY   - Anthropic LLM key
  GOOGLE_API_KEY      - Google LLM key
  ONECMD_MGR_MODEL    - Override LLM model name
  ONECMD_VISIBLE_LINES - Terminal visible lines (int, 10-200)
  ONECMD_SPLIT_MESSAGES - Enable message splitting ("1" or "true")
  ONECMD_ADMIN_PASSWORD - Admin panel password
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class Config(BaseModel, extra="forbid"):
    """Validated configuration for the onecmd bot."""

    apikey: str = Field(min_length=1, description="Telegram bot token")
    dbfile: str = Field(default="./mybot.sqlite", description="SQLite database path")
    danger_mode: bool = Field(default=False, description="Attach to any window")
    weak_security: bool = Field(default=False, description="Skip OTP authentication")
    enable_otp: bool = Field(default=False, description="Enable TOTP on first run")
    verbose: bool = Field(default=False, description="Debug-level logging")
    visible_lines: int = Field(default=40, ge=10, le=200, description="Terminal visible lines")
    split_messages: bool = Field(default=False, description="Split long messages")
    mgr_model: str | None = Field(default=None, description="Override LLM model")
    otp_timeout: int = Field(default=300, ge=30, le=28800, description="OTP timeout seconds")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    google_api_key: str | None = Field(default=None, description="Google API key")
    admin_port: int | None = Field(default=None, ge=1024, le=65535, description="Port for admin panel (None = disabled)")
    admin_password: str | None = Field(default=None, description="Admin panel password")

    @property
    def has_llm_key(self) -> bool:
        """True if at least one LLM provider key is configured."""
        return bool(self.anthropic_api_key or self.google_api_key)

    @model_validator(mode="after")
    def _validate_keys(self) -> Config:
        if self.anthropic_api_key is not None and len(self.anthropic_api_key) == 0:
            raise ValueError("anthropic_api_key must be non-empty if provided")
        if self.google_api_key is not None and len(self.google_api_key) == 0:
            raise ValueError("google_api_key must be non-empty if provided")
        return self


def _read_apikey_file(path: str = "apikey.txt") -> str | None:
    """Read Telegram bot token from apikey.txt, returning None if missing."""
    try:
        text = Path(path).read_text().strip()
        return text if text else None
    except OSError:
        return None


def _parse_bool_env(name: str) -> bool:
    """Parse an env var as boolean (truthy: '1', 'true', 'yes')."""
    val = os.environ.get(name, "")
    return val.lower() in ("1", "true", "yes")


def parse_config(argv: list[str] | None = None) -> Config:
    """
    Build a Config from CLI args + env vars + apikey.txt fallback.

    CLI flags (mirrors the C version):
      --apikey <token>
      --dbfile <path>
      --dangerously-attach-to-any-window
      --use-weak-security
      --enable-otp
      --verbose
      --admin-port <port>

    Env vars fill in LLM keys and display settings.
    """
    if argv is None:
        argv = sys.argv

    # --- Parse CLI args (simple flag loop, matching C main.c) ---
    apikey: str | None = None
    dbfile: str = "./mybot.sqlite"
    danger_mode: bool = False
    weak_security: bool = False
    enable_otp: bool = False
    verbose: bool = False
    admin_port: int | None = None

    i = 1  # skip argv[0]
    while i < len(argv):
        arg = argv[i]
        if arg == "--apikey" and i + 1 < len(argv):
            i += 1
            apikey = argv[i]
        elif arg == "--dbfile" and i + 1 < len(argv):
            i += 1
            dbfile = argv[i]
        elif arg == "--dangerously-attach-to-any-window":
            danger_mode = True
        elif arg == "--use-weak-security":
            weak_security = True
        elif arg == "--enable-otp":
            enable_otp = True
        elif arg == "--verbose":
            verbose = True
        elif arg == "--admin-port" and i + 1 < len(argv):
            i += 1
            try:
                admin_port = int(argv[i])
            except ValueError:
                pass
        i += 1

    # --- Fallback: read apikey from file ---
    if apikey is None:
        apikey = _read_apikey_file()
    if apikey is None:
        raise SystemExit(
            "ERROR: Telegram bot token not provided.\n"
            "       Use --apikey <TOKEN> or create an apikey.txt file.\n"
            "       Get a token from @BotFather on Telegram."
        )

    # --- Env vars ---
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or None
    google_key = os.environ.get("GOOGLE_API_KEY") or None
    mgr_model = os.environ.get("ONECMD_MGR_MODEL") or None

    visible_lines_env = os.environ.get("ONECMD_VISIBLE_LINES")
    visible_lines: int = 40
    if visible_lines_env is not None:
        try:
            visible_lines = int(visible_lines_env)
        except ValueError:
            pass

    split_messages = _parse_bool_env("ONECMD_SPLIT_MESSAGES")

    admin_password = os.environ.get("ONECMD_ADMIN_PASSWORD") or None

    return Config(
        apikey=apikey,
        dbfile=dbfile,
        danger_mode=danger_mode,
        weak_security=weak_security,
        enable_otp=enable_otp,
        verbose=verbose,
        visible_lines=visible_lines,
        mgr_model=mgr_model,
        otp_timeout=300,
        anthropic_api_key=anthropic_key,
        google_api_key=google_key,
        split_messages=split_messages,
        admin_port=admin_port,
        admin_password=admin_password,
    )
