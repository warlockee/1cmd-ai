"""Tests for onecmd.config — defaults, bounds, env vars, validation."""

import os
from pathlib import Path
from unittest import mock

import pytest
from pydantic import ValidationError

from onecmd.config import Config, parse_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ARGV = ["onecmd", "--apikey", "123:ABC"]


def _parse(**env_overrides) -> Config:
    """Parse with minimal valid argv and optional env overrides."""
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "ONECMD_MGR_MODEL",
            "ONECMD_VISIBLE_LINES",
            "ONECMD_SPLIT_MESSAGES",
        }
    }
    clean_env.update(env_overrides)
    with mock.patch.dict(os.environ, clean_env, clear=True):
        return parse_config(MINIMAL_ARGV)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_defaults(self):
        cfg = _parse()
        assert cfg.apikey == "123:ABC"
        assert cfg.dbfile == "./mybot.sqlite"
        assert cfg.danger_mode is False
        assert cfg.weak_security is False
        assert cfg.enable_otp is False
        assert cfg.verbose is False
        assert cfg.visible_lines == 40
        assert cfg.split_messages is False
        assert cfg.mgr_model is None
        assert cfg.otp_timeout == 300
        assert cfg.anthropic_api_key is None
        assert cfg.google_api_key is None

    def test_has_llm_key_false_by_default(self):
        cfg = _parse()
        assert cfg.has_llm_key is False

    def test_has_llm_key_anthropic(self):
        cfg = _parse(ANTHROPIC_API_KEY="sk-test")
        assert cfg.has_llm_key is True

    def test_has_llm_key_google(self):
        cfg = _parse(GOOGLE_API_KEY="AIza-test")
        assert cfg.has_llm_key is True


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------


class TestCLIParsing:
    def test_all_flags(self):
        argv = [
            "onecmd",
            "--apikey",
            "tok",
            "--dbfile",
            "/tmp/test.db",
            "--dangerously-attach-to-any-window",
            "--use-weak-security",
            "--enable-otp",
            "--verbose",
        ]
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = parse_config(argv)
        assert cfg.apikey == "tok"
        assert cfg.dbfile == "/tmp/test.db"
        assert cfg.danger_mode is True
        assert cfg.weak_security is True
        assert cfg.enable_otp is True
        assert cfg.verbose is True

    def test_unknown_flags_ignored(self):
        """Unknown CLI flags are silently ignored (matching C behavior)."""
        argv = ["onecmd", "--apikey", "tok", "--unknown-flag", "--also-unknown"]
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = parse_config(argv)
        assert cfg.apikey == "tok"


# ---------------------------------------------------------------------------
# Env var reading
# ---------------------------------------------------------------------------


class TestEnvVars:
    def test_anthropic_key(self):
        cfg = _parse(ANTHROPIC_API_KEY="sk-ant-123")
        assert cfg.anthropic_api_key == "sk-ant-123"

    def test_google_key(self):
        cfg = _parse(GOOGLE_API_KEY="AIza-xyz")
        assert cfg.google_api_key == "AIza-xyz"

    def test_mgr_model(self):
        cfg = _parse(ONECMD_MGR_MODEL="claude-opus-4-6")
        assert cfg.mgr_model == "claude-opus-4-6"

    def test_visible_lines_from_env(self):
        cfg = _parse(ONECMD_VISIBLE_LINES="80")
        assert cfg.visible_lines == 80

    def test_visible_lines_invalid_env_uses_default(self):
        cfg = _parse(ONECMD_VISIBLE_LINES="not-a-number")
        assert cfg.visible_lines == 40

    def test_split_messages_true(self):
        cfg = _parse(ONECMD_SPLIT_MESSAGES="1")
        assert cfg.split_messages is True

    def test_split_messages_true_word(self):
        cfg = _parse(ONECMD_SPLIT_MESSAGES="true")
        assert cfg.split_messages is True

    def test_split_messages_false_default(self):
        cfg = _parse(ONECMD_SPLIT_MESSAGES="0")
        assert cfg.split_messages is False

    def test_empty_anthropic_key_treated_as_none(self):
        cfg = _parse(ANTHROPIC_API_KEY="")
        assert cfg.anthropic_api_key is None

    def test_empty_google_key_treated_as_none(self):
        cfg = _parse(GOOGLE_API_KEY="")
        assert cfg.google_api_key is None


# ---------------------------------------------------------------------------
# apikey.txt fallback
# ---------------------------------------------------------------------------


class TestApikeyFile:
    def test_reads_from_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apikey.txt").write_text("file-token-123\n")
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "ANTHROPIC_API_KEY",
                "GOOGLE_API_KEY",
                "ONECMD_MGR_MODEL",
                "ONECMD_VISIBLE_LINES",
                "ONECMD_SPLIT_MESSAGES",
            }
        }
        with mock.patch.dict(os.environ, clean_env, clear=True):
            cfg = parse_config(["onecmd"])
        assert cfg.apikey == "file-token-123"

    def test_missing_file_and_no_flag_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit, match="Telegram bot token not provided"):
                parse_config(["onecmd"])

    def test_empty_file_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apikey.txt").write_text("   \n")
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit, match="Telegram bot token not provided"):
                parse_config(["onecmd"])


# ---------------------------------------------------------------------------
# Pydantic extra='forbid' — reject unknown fields
# ---------------------------------------------------------------------------


class TestExtraForbid:
    def test_rejects_unknown_field(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Config(apikey="tok", unknown_field="bad")

    def test_rejects_multiple_unknown_fields(self):
        with pytest.raises(ValidationError):
            Config(apikey="tok", foo="a", bar="b")


# ---------------------------------------------------------------------------
# Bounds validation
# ---------------------------------------------------------------------------


class TestBounds:
    def test_visible_lines_too_low(self):
        with pytest.raises(ValidationError, match="greater than or equal to 10"):
            Config(apikey="tok", visible_lines=5)

    def test_visible_lines_too_high(self):
        with pytest.raises(ValidationError, match="less than or equal to 200"):
            Config(apikey="tok", visible_lines=999)

    def test_visible_lines_at_lower_bound(self):
        cfg = Config(apikey="tok", visible_lines=10)
        assert cfg.visible_lines == 10

    def test_visible_lines_at_upper_bound(self):
        cfg = Config(apikey="tok", visible_lines=200)
        assert cfg.visible_lines == 200

    def test_otp_timeout_too_low(self):
        with pytest.raises(ValidationError, match="greater than or equal to 30"):
            Config(apikey="tok", otp_timeout=10)

    def test_otp_timeout_too_high(self):
        with pytest.raises(ValidationError, match="less than or equal to 28800"):
            Config(apikey="tok", otp_timeout=99999)

    def test_otp_timeout_at_bounds(self):
        cfg = Config(apikey="tok", otp_timeout=30)
        assert cfg.otp_timeout == 30
        cfg = Config(apikey="tok", otp_timeout=28800)
        assert cfg.otp_timeout == 28800

    def test_apikey_empty_rejected(self):
        with pytest.raises(ValidationError, match="at least 1 character"):
            Config(apikey="")

    def test_env_visible_lines_out_of_bounds_rejected(self):
        """Out-of-bounds visible_lines from env var is caught by Pydantic."""
        with mock.patch.dict(
            os.environ,
            {"ONECMD_VISIBLE_LINES": "5"},
            clear=True,
        ):
            with pytest.raises(ValidationError, match="greater than or equal to 10"):
                parse_config(MINIMAL_ARGV)


# ---------------------------------------------------------------------------
# Model validator — empty string LLM keys
# ---------------------------------------------------------------------------


class TestModelValidator:
    def test_empty_anthropic_key_directly_rejected(self):
        with pytest.raises(ValidationError, match="anthropic_api_key must be non-empty"):
            Config(apikey="tok", anthropic_api_key="")

    def test_empty_google_key_directly_rejected(self):
        with pytest.raises(ValidationError, match="google_api_key must be non-empty"):
            Config(apikey="tok", google_api_key="")

    def test_none_keys_accepted(self):
        cfg = Config(apikey="tok", anthropic_api_key=None, google_api_key=None)
        assert cfg.anthropic_api_key is None
        assert cfg.google_api_key is None
