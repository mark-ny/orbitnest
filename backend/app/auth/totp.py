"""RFC 6238 time-based one-time passwords, recovery codes and at-rest sealing of the secret.

Only the standard library is used. ``seal``/``unseal`` are encrypt-then-MAC built from HMAC-SHA256
(counter-mode keystream + a tag over nonce and ciphertext), with the encryption and MAC keys derived
separately from the server secret. That protects secrets in a copied database file or backup; it
does not protect against someone who also holds the server secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
SECRET_BYTES = 20  # 160 bits, the RFC 4226 recommendation
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RECOVERY_COUNT = 10


def new_secret() -> bytes:
    return secrets.token_bytes(SECRET_BYTES)


def b32(secret: bytes) -> str:
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(value).zfill(digits)


def totp(key: bytes, timestamp: float, digits: int = DIGITS) -> str:
    return hotp(key, int(timestamp // STEP_SECONDS), digits)


def normalize_code(code: str) -> str | None:
    cleaned = "".join(code.split())
    return cleaned if len(cleaned) == DIGITS and cleaned.isascii() and cleaned.isdigit() else None


def verify(key: bytes, code: str, timestamp: float, last_step: int = 0, window: int = 1) -> int | None:
    """Return the matching step, or None. Steps at or before ``last_step`` are rejected (replay)."""
    cleaned = normalize_code(code)
    if cleaned is None:
        return None
    current = int(timestamp // STEP_SECONDS)
    matched: int | None = None
    for step in range(current - window, current + window + 1):  # no early exit: constant work
        if hmac.compare_digest(hotp(key, step), cleaned) and step > last_step and matched is None:
            matched = step
    return matched


def otpauth_uri(issuer: str, account: str, secret: bytes) -> str:
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    return (
        f"otpauth://totp/{label}?secret={b32(secret)}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


# -- recovery codes ---------------------------------------------------------------------------
def new_recovery_codes(count: int = RECOVERY_COUNT) -> list[str]:
    def one() -> str:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        return f"{raw[:5]}-{raw[5:]}"

    return [one() for _ in range(count)]


def normalize_recovery(code: str) -> str | None:
    cleaned = code.strip().lower().replace("-", "").replace(" ", "")
    if len(cleaned) != 10 or any(c not in RECOVERY_ALPHABET for c in cleaned):
        return None
    return cleaned


def hash_recovery(code: str) -> str | None:
    cleaned = normalize_recovery(code)
    return hashlib.sha256(b"orbitnest-recovery:" + cleaned.encode()).hexdigest() if cleaned else None


# -- sealing ----------------------------------------------------------------------------------
def _keys(server_secret: bytes) -> tuple[bytes, bytes]:
    return (
        hmac.new(server_secret, b"orbitnest/totp/enc/v1", hashlib.sha256).digest(),
        hmac.new(server_secret, b"orbitnest/totp/mac/v1", hashlib.sha256).digest(),
    )


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(server_secret: bytes, plaintext: bytes) -> str:
    enc, mac = _keys(server_secret)
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(plaintext, _stream(enc, nonce, len(plaintext)), strict=True))
    tag = hmac.new(mac, b"v1" + nonce + ct, hashlib.sha256).digest()
    return "v1." + base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def unseal(server_secret: bytes, sealed: str) -> bytes | None:
    if not sealed.startswith("v1."):
        return None
    try:
        blob = base64.urlsafe_b64decode(sealed[3:].encode("ascii"))
    except ValueError:
        return None
    if len(blob) < 16 + 32:
        return None
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    enc, mac = _keys(server_secret)
    if not hmac.compare_digest(hmac.new(mac, b"v1" + nonce + ct, hashlib.sha256).digest(), tag):
        return None
    return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct)), strict=True))
