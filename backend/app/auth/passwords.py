"""Password hashing and policy (ADR-002).

Preferred: Argon2id via ``argon2-cffi`` when importable (UNTESTED in the build sandbox, where the
package is unavailable). Fallback: stdlib scrypt with OWASP-aligned parameters, encoded in a
self-describing string so parameters can be raised later and old hashes re-hashed on login.

Passwords are NFKC-normalised before hashing so visually identical inputs verify identically.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import unicodedata
from typing import Any

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_CHARS = 256
MAX_PASSWORD_BYTES = 1024

# Deliberately tiny: this is a backstop against the very worst choices, not a breach corpus.
_COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "passwordpassword",
        "123456789012",
        "1234567890123",
        "qwertyuiop12",
        "qwertyuiopas",
        "letmeinletmein",
        "administrator",
        "adminadminadmin",
        "iloveyou1234",
        "welcome12345",
        "changemechangeme",
        "orbitnestorbitnest",
    }
)


class PasswordPolicyError(ValueError):
    pass


def _normalize(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def validate_password(password: str, username: str | None = None) -> None:
    """Length-based policy (no composition rules). Raises PasswordPolicyError with a safe message."""
    normalized = _normalize(password)
    if len(normalized) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(normalized) > MAX_PASSWORD_CHARS or len(normalized.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD_CHARS} characters.")
    lowered = normalized.lower()
    if username and lowered == username.lower():
        raise PasswordPolicyError("Password must not be the same as the username.")
    if lowered in _COMMON_PASSWORDS or len(set(normalized)) < 4:
        raise PasswordPolicyError("Password is too easy to guess.")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class PasswordHasher:
    """Hash/verify with Argon2id if available, else scrypt. ``verify`` never raises."""

    def __init__(
        self,
        *,
        scrypt_log_n: int = 15,
        scrypt_r: int = 8,
        scrypt_p: int = 3,
        prefer_argon2: bool = True,
    ) -> None:
        self.log_n, self.r, self.p = scrypt_log_n, scrypt_r, scrypt_p
        self._argon2: Any = None
        if prefer_argon2:
            try:
                from argon2 import PasswordHasher as Argon2Hasher  # type: ignore[import-not-found]

                self._argon2 = Argon2Hasher()
            except ImportError:
                self._argon2 = None
        self._dummy: str | None = None

    @property
    def scheme(self) -> str:
        return "argon2id" if self._argon2 is not None else "scrypt"

    # -- scrypt ------------------------------------------------------------------------
    def _scrypt_raw(self, password: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
        return hashlib.scrypt(
            _normalize(password).encode("utf-8"),
            salt=salt,
            n=1 << log_n,
            r=r,
            p=p,
            maxmem=256 * 1024 * 1024,
            dklen=32,
        )

    def hash(self, password: str) -> str:
        if self._argon2 is not None:
            return str(self._argon2.hash(_normalize(password)))
        salt = secrets.token_bytes(16)
        digest = self._scrypt_raw(password, salt, self.log_n, self.r, self.p)
        return f"$scrypt$ln={self.log_n},r={self.r},p={self.p}${_b64(salt)}${_b64(digest)}"

    def _parse_scrypt(self, stored: str) -> tuple[int, int, int, bytes, bytes] | None:
        try:
            _, scheme, params, salt_b64, digest_b64 = stored.split("$")
            if scheme != "scrypt":
                return None
            values = dict(part.split("=") for part in params.split(","))
            log_n, r, p = int(values["ln"]), int(values["r"]), int(values["p"])
            # Bound parameters so a corrupted/tampered row cannot trigger huge allocations.
            if not (10 <= log_n <= 20 and 1 <= r <= 16 and 1 <= p <= 16):
                return None
            return log_n, r, p, _unb64(salt_b64), _unb64(digest_b64)
        except (ValueError, KeyError):
            return None

    def verify(self, password: str, stored: str) -> bool:
        try:
            if stored.startswith("$argon2"):
                if self._argon2 is None:
                    return False
                return bool(self._argon2.verify(stored, _normalize(password)))
            parsed = self._parse_scrypt(stored)
            if parsed is None:
                return False
            log_n, r, p, salt, expected = parsed
            actual = self._scrypt_raw(password, salt, log_n, r, p)
            return hmac.compare_digest(actual, expected)
        except Exception:  # argon2 VerifyMismatch/InvalidHash, memory errors, etc.
            return False

    def needs_rehash(self, stored: str) -> bool:
        if self._argon2 is not None:
            if not stored.startswith("$argon2"):
                return True
            try:
                return bool(self._argon2.check_needs_rehash(stored))
            except Exception:
                return True
        parsed = self._parse_scrypt(stored)
        if parsed is None:
            return True
        log_n, r, p, _, _ = parsed
        return (log_n, r, p) != (self.log_n, self.r, self.p)

    def verify_dummy(self, password: str) -> None:
        """Burn comparable time when the user does not exist (reduces username-timing signal)."""
        if self._dummy is None:
            self._dummy = self.hash(secrets.token_urlsafe(16))
        self.verify(password, self._dummy)
