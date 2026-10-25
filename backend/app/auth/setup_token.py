"""First-run setup token.

Claiming the first administrator account requires a secret that only someone with filesystem
access to the data directory can read. This prevents a network attacker from racing the owner to
create the admin account, and it deliberately does NOT trust "loopback" (a same-host reverse proxy
makes every remote client look like loopback).
"""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
from pathlib import Path

TOKEN_FILENAME = "setup-token"  # noqa: S105 - a file name, not a credential


def token_path(data_dir: Path) -> Path:
    return data_dir / TOKEN_FILENAME


def ensure_setup_token(data_dir: Path) -> Path:
    """Create the token file (0600) if missing and return its path."""
    path = token_path(data_dir)
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_urlsafe(32))
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    return path


def read_setup_token(data_dir: Path) -> str | None:
    try:
        return token_path(data_dir).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def verify_setup_token(data_dir: Path, supplied: str) -> bool:
    expected = read_setup_token(data_dir)
    if expected is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.strip().encode("utf-8"))


def remove_setup_token(data_dir: Path) -> None:
    with contextlib.suppress(OSError):
        token_path(data_dir).unlink()
