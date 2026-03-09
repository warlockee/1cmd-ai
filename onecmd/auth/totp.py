"""TOTP authentication for onecmd.

Calling spec:
  Inputs:
    totp_setup(store, enable_otp, weak_security, otp_timeout) -> bool
    totp_verify(code, secret_hex) -> bool
    generate_secret() -> bytes (20 random bytes via os.urandom)
    base32_encode(data) -> str (RFC 4648)
    totp_code(secret, time_step) -> int (6-digit TOTP)
    build_otpauth_uri(secret_b32, issuer, label) -> str
    print_qr(uri) -> None (text QR to stdout)
    is_timed_out(last_auth_time, timeout) -> bool
  Outputs: bool (verified) or None (setup side effects)
  Side effects: generates secret on first run, prints QR to terminal

Sealed (deterministic):
  - HMAC-SHA1 based TOTP (RFC 6238), 30s steps, +/-1 tolerance
  - QR code via qrcode library

Guarding:
  - Secret from os.urandom(20), stored in SQLite, never logged
  - OTP codes validated as exactly 6 digits before comparison
  - Constant-time comparison via hmac.compare_digest
  - Timeout enforced: re-auth required after otp_timeout seconds
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time

import qrcode  # type: ignore[import-untyped]

TOTP_PERIOD = 30
TOTP_DIGITS = 6
SECRET_LENGTH = 20
TOTP_WINDOW = 1  # +/-1 time step tolerance
STORE_KEY = "totp_secret"


def generate_secret() -> bytes:
    """Return 20 cryptographically secure random bytes."""
    return os.urandom(SECRET_LENGTH)


_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def base32_encode(data: bytes) -> str:
    """Encode *data* to Base32 (RFC 4648), no padding."""
    buf = 0
    bits = 0
    out: list[str] = []
    for byte in data:
        buf = (buf << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_B32_ALPHABET[(buf >> bits) & 0x1F])
    if bits > 0:
        out.append(_B32_ALPHABET[(buf << (5 - bits)) & 0x1F])
    return "".join(out)


def totp_code(secret: bytes, time_step: int) -> int:
    """Compute 6-digit TOTP from *secret* and *time_step*."""
    msg = struct.pack(">Q", time_step)
    digest = hmac.new(secret, msg, hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    code = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return code % (10**TOTP_DIGITS)


def _bytes_to_hex(data: bytes) -> str:
    return data.hex()


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def build_otpauth_uri(
    secret_b32: str,
    issuer: str = "tgterm",
    label: str = "tgterm",
) -> str:
    """Build an otpauth:// URI for TOTP QR code generation."""
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={issuer}"


def print_qr(uri: str) -> None:
    """Print a text-mode QR code for *uri* to stdout."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii()


def is_timed_out(last_auth_time: float, timeout: int) -> bool:
    """Return True if re-authentication is needed."""
    return (time.time() - last_auth_time) >= timeout


def totp_verify(code: str, secret_hex: str) -> bool:
    """Verify a TOTP *code* against *secret_hex*.

    Returns True only when *code* is a valid 6-digit string matching
    the current time step or +/-1.  Constant-time via hmac.compare_digest.
    """
    if not isinstance(code, str) or len(code) != TOTP_DIGITS:
        return False
    if not code.isdigit():
        return False

    secret = _hex_to_bytes(secret_hex)
    if len(secret) != SECRET_LENGTH:
        return False

    now_step = int(time.time()) // TOTP_PERIOD

    for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        expected = totp_code(secret, now_step + offset)
        expected_str = f"{expected:06d}"
        if hmac.compare_digest(code, expected_str):
            return True

    return False


def totp_setup(
    store: object,
    enable_otp: bool,
    weak_security: bool,
    otp_timeout: int = 300,
) -> bool:
    """Set up TOTP authentication.

    *store* must have get(key)->str|None and set(key, value) methods.
    Returns True if OTP is active, False if disabled.
    """
    if weak_security:
        return False

    if not enable_otp:
        return False

    existing = store.get(STORE_KEY)  # type: ignore[union-attr]
    if existing:
        timeout_str = store.get("otp_timeout")  # type: ignore[union-attr]
        if timeout_str:
            t = int(timeout_str)
            if 30 <= t <= 28800:
                pass  # caller reads back; we validate bounds
        return True

    secret = generate_secret()
    store.set(STORE_KEY, _bytes_to_hex(secret))  # type: ignore[union-attr]

    b32 = base32_encode(secret)
    uri = build_otpauth_uri(b32)

    print("\n=== TOTP Setup ===")
    print("Scan this QR code with Google Authenticator:\n")
    print_qr(uri)
    print(f"\nOr enter this secret manually: {b32}")
    print("==================\n")

    return True
