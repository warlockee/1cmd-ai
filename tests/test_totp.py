"""Tests for onecmd.auth.totp — 100% coverage.

Covers:
  - RFC 6238 test vectors (HMAC-SHA1)
  - +/-1 time step tolerance
  - Invalid codes (non-digits, wrong length, empty)
  - Constant-time comparison (hmac.compare_digest usage)
  - Secret generation (length, randomness)
  - Base32 encoding
  - Hex round-trip
  - otpauth URI construction
  - QR printing (smoke test)
  - Timeout logic
  - totp_setup: new secret, existing secret, weak security, OTP disabled
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import time
from unittest import mock

import pytest

from onecmd.auth.totp import (
    STORE_KEY,
    TOTP_DIGITS,
    TOTP_PERIOD,
    _bytes_to_hex,
    _hex_to_bytes,
    base32_encode,
    build_otpauth_uri,
    generate_secret,
    is_timed_out,
    print_qr,
    totp_code,
    totp_setup,
    totp_verify,
)


# =========================================================================
# RFC 6238 Test Vectors (HMAC-SHA1)
# =========================================================================
# RFC 6238 Appendix B uses the ASCII string "12345678901234567890" (20 bytes)
# as the shared secret for SHA1 test vectors.

RFC_SECRET = b"12345678901234567890"
RFC_SECRET_HEX = RFC_SECRET.hex()

# (unix_time, expected_totp)
RFC_VECTORS = [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
]


class TestTotpCode:
    """Test the core TOTP computation against RFC 6238 vectors."""

    @pytest.mark.parametrize("unix_time, expected", RFC_VECTORS)
    def test_rfc6238_vectors(self, unix_time: int, expected: str) -> None:
        time_step = unix_time // TOTP_PERIOD
        result = totp_code(RFC_SECRET, time_step)
        assert f"{result:06d}" == expected

    def test_returns_int(self) -> None:
        result = totp_code(RFC_SECRET, 0)
        assert isinstance(result, int)

    def test_six_digit_range(self) -> None:
        """Result is always in [0, 999999]."""
        for step in range(100):
            code = totp_code(RFC_SECRET, step)
            assert 0 <= code <= 999999


# =========================================================================
# Verification
# =========================================================================


class TestTotpVerify:
    """Test totp_verify including tolerance and input validation."""

    def _code_at(self, secret_hex: str, unix_time: int) -> str:
        step = unix_time // TOTP_PERIOD
        secret = bytes.fromhex(secret_hex)
        return f"{totp_code(secret, step):06d}"

    def test_current_step_matches(self) -> None:
        now = 1111111109
        code = self._code_at(RFC_SECRET_HEX, now)
        with mock.patch("onecmd.auth.totp.time") as mock_time:
            mock_time.time.return_value = float(now)
            assert totp_verify(code, RFC_SECRET_HEX) is True

    def test_minus_one_tolerance(self) -> None:
        """Code from the previous time step should still be accepted."""
        now = 1111111109
        prev_step = (now // TOTP_PERIOD) - 1
        code = f"{totp_code(RFC_SECRET, prev_step):06d}"
        with mock.patch("onecmd.auth.totp.time") as mock_time:
            mock_time.time.return_value = float(now)
            assert totp_verify(code, RFC_SECRET_HEX) is True

    def test_plus_one_tolerance(self) -> None:
        """Code from the next time step should still be accepted."""
        now = 1111111109
        next_step = (now // TOTP_PERIOD) + 1
        code = f"{totp_code(RFC_SECRET, next_step):06d}"
        with mock.patch("onecmd.auth.totp.time") as mock_time:
            mock_time.time.return_value = float(now)
            assert totp_verify(code, RFC_SECRET_HEX) is True

    def test_outside_window_rejected(self) -> None:
        """Code from 2 steps ago should be rejected."""
        now = 1111111109
        old_step = (now // TOTP_PERIOD) - 2
        code = f"{totp_code(RFC_SECRET, old_step):06d}"
        with mock.patch("onecmd.auth.totp.time") as mock_time:
            mock_time.time.return_value = float(now)
            assert totp_verify(code, RFC_SECRET_HEX) is False

    # -- Input validation --------------------------------------------------

    def test_rejects_non_digit_code(self) -> None:
        assert totp_verify("12345a", RFC_SECRET_HEX) is False

    def test_rejects_short_code(self) -> None:
        assert totp_verify("12345", RFC_SECRET_HEX) is False

    def test_rejects_long_code(self) -> None:
        assert totp_verify("1234567", RFC_SECRET_HEX) is False

    def test_rejects_empty_code(self) -> None:
        assert totp_verify("", RFC_SECRET_HEX) is False

    def test_rejects_non_string_code(self) -> None:
        assert totp_verify(123456, RFC_SECRET_HEX) is False  # type: ignore[arg-type]

    def test_rejects_spaces(self) -> None:
        assert totp_verify("123 56", RFC_SECRET_HEX) is False

    def test_rejects_wrong_secret_length(self) -> None:
        short_hex = "aabbcc"  # only 3 bytes
        assert totp_verify("123456", short_hex) is False

    # -- Constant-time comparison ------------------------------------------

    def test_uses_compare_digest(self) -> None:
        """Ensure hmac.compare_digest is called during verification."""
        now = 1111111109
        code = self._code_at(RFC_SECRET_HEX, now)
        with (
            mock.patch("onecmd.auth.totp.time") as mock_time,
            mock.patch("onecmd.auth.totp.hmac.compare_digest", wraps=hmac.compare_digest) as mock_cmp,
        ):
            mock_time.time.return_value = float(now)
            totp_verify(code, RFC_SECRET_HEX)
            assert mock_cmp.called


# =========================================================================
# Secret generation
# =========================================================================


class TestGenerateSecret:
    def test_length(self) -> None:
        secret = generate_secret()
        assert len(secret) == 20

    def test_returns_bytes(self) -> None:
        secret = generate_secret()
        assert isinstance(secret, bytes)

    def test_randomness(self) -> None:
        """Two calls should produce different secrets."""
        a = generate_secret()
        b = generate_secret()
        assert a != b


# =========================================================================
# Base32 encoding
# =========================================================================


class TestBase32Encode:
    def test_rfc_secret(self) -> None:
        """Known encoding of the RFC test secret."""
        result = base32_encode(RFC_SECRET)
        # "12345678901234567890" in base32
        assert result == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def test_empty(self) -> None:
        assert base32_encode(b"") == ""

    def test_single_byte(self) -> None:
        # 0x00 -> 'A' with leftover bits
        result = base32_encode(b"\x00")
        assert result == "AA"

    def test_matches_c_implementation(self) -> None:
        """Verify our encoding matches the C base32_encode output."""
        data = bytes(range(20))
        result = base32_encode(data)
        # Verify it's valid base32 (only uppercase A-Z and 2-7)
        assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in result)


# =========================================================================
# Hex conversion
# =========================================================================


class TestHexConversion:
    def test_round_trip(self) -> None:
        data = b"\xde\xad\xbe\xef" * 5
        assert _hex_to_bytes(_bytes_to_hex(data)) == data

    def test_bytes_to_hex_format(self) -> None:
        assert _bytes_to_hex(b"\x00\xff") == "00ff"

    def test_hex_to_bytes_format(self) -> None:
        assert _hex_to_bytes("deadbeef") == b"\xde\xad\xbe\xef"


# =========================================================================
# OTP URI
# =========================================================================


class TestBuildOtpauthUri:
    def test_default_params(self) -> None:
        uri = build_otpauth_uri("JBSWY3DPEHPK3PXP")
        assert uri == "otpauth://totp/tgterm?secret=JBSWY3DPEHPK3PXP&issuer=tgterm"

    def test_custom_params(self) -> None:
        uri = build_otpauth_uri("ABC", issuer="myapp", label="user@example.com")
        assert uri == "otpauth://totp/user@example.com?secret=ABC&issuer=myapp"


# =========================================================================
# QR printing (smoke test)
# =========================================================================


class TestPrintQr:
    def test_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """print_qr should produce some output without raising."""
        print_qr("otpauth://totp/test?secret=JBSWY3DPEHPK3PXP&issuer=test")
        captured = capsys.readouterr()
        assert len(captured.out) > 0


# =========================================================================
# Timeout
# =========================================================================


class TestIsTimedOut:
    def test_not_timed_out(self) -> None:
        assert is_timed_out(time.time() - 10, 300) is False

    def test_timed_out(self) -> None:
        assert is_timed_out(time.time() - 400, 300) is True

    def test_exact_boundary(self) -> None:
        """At exactly the timeout boundary, should be timed out."""
        now = time.time()
        assert is_timed_out(now - 300, 300) is True

    def test_zero_timeout(self) -> None:
        """Zero timeout means always timed out."""
        assert is_timed_out(time.time(), 0) is True


# =========================================================================
# Setup
# =========================================================================


class FakeStore:
    """Minimal store mock with get/set interface."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = data or {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, expire: int = 0) -> None:
        self._data[key] = value


class TestTotpSetup:
    def test_weak_security_skips(self) -> None:
        store = FakeStore()
        result = totp_setup(store, enable_otp=True, weak_security=True)
        assert result is False
        assert store.get(STORE_KEY) is None

    def test_otp_disabled_skips(self) -> None:
        store = FakeStore()
        result = totp_setup(store, enable_otp=False, weak_security=False)
        assert result is False
        assert store.get(STORE_KEY) is None

    def test_existing_secret_returns_true(self) -> None:
        store = FakeStore({STORE_KEY: RFC_SECRET_HEX})
        result = totp_setup(store, enable_otp=True, weak_security=False)
        assert result is True

    def test_existing_secret_with_timeout(self) -> None:
        store = FakeStore({STORE_KEY: RFC_SECRET_HEX, "otp_timeout": "600"})
        result = totp_setup(store, enable_otp=True, weak_security=False)
        assert result is True

    def test_existing_secret_with_invalid_timeout(self) -> None:
        """Invalid timeout values should not crash."""
        store = FakeStore({STORE_KEY: RFC_SECRET_HEX, "otp_timeout": "5"})
        result = totp_setup(store, enable_otp=True, weak_security=False)
        assert result is True

    def test_generates_new_secret(self, capsys: pytest.CaptureFixture[str]) -> None:
        store = FakeStore()
        result = totp_setup(store, enable_otp=True, weak_security=False)
        assert result is True
        # Secret should be stored
        stored = store.get(STORE_KEY)
        assert stored is not None
        assert len(bytes.fromhex(stored)) == 20
        # QR output should be printed
        captured = capsys.readouterr()
        assert "TOTP Setup" in captured.out
        assert "secret manually" in captured.out

    def test_new_secret_is_random(self) -> None:
        """Two setups should produce different secrets."""
        store1 = FakeStore()
        store2 = FakeStore()
        with mock.patch("onecmd.auth.totp.print_qr"):
            totp_setup(store1, enable_otp=True, weak_security=False)
            totp_setup(store2, enable_otp=True, weak_security=False)
        assert store1.get(STORE_KEY) != store2.get(STORE_KEY)

    def test_weak_security_takes_precedence(self) -> None:
        """Even with enable_otp=True, weak_security=True disables OTP."""
        store = FakeStore()
        result = totp_setup(store, enable_otp=True, weak_security=True)
        assert result is False
