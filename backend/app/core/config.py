"""Central, validated configuration.

Design rules (see docs/ARCHITECTURE.md ADR-004):

* Loopback bind by default.
* Production refuses to start without an explicit, strong ``ORBITNEST_SECRET_KEY``.
* A predictable secret is never generated. Development persists a random key to a 0600 file;
  tests use a per-process random key that is never written to disk.
* Non-loopback binding is refused until authentication is enforced (``AUTH_ENFORCED``) *and* the
  operator opts in explicitly.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

ENV_PREFIX = "ORBITNEST_"

# True once real authentication guards every non-public route (Phase 2). A route-walking test
# (tests/test_route_guard.py) fails if a route is added without authentication or an explicit
# public-allowlist entry. If this is ever set False, the server only binds loopback addresses.
AUTH_ENFORCED = True

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MIN_SECRET_LENGTH = 32
_PLACEHOLDER_SECRETS = frozenset(
    {
        "changeme",
        "change-me",
        "secret",
        "replace-me",
        "replace_with_random_secret",
        "your-secret-key",
    }
)


class ConfigError(Exception):
    """Raised when configuration is missing or unsafe. Messages never contain secret values."""


def default_data_dir(environ: Mapping[str, str], sys_platform: str | None = None) -> Path:
    """Platform-aware default data directory (no hard-coded /home paths)."""
    plat = sys_platform or sys.platform
    home = environ.get("HOME") or environ.get("USERPROFILE") or str(Path.home())
    prefix = environ.get("PREFIX", "")
    if environ.get("TERMUX_VERSION") or "com.termux" in prefix:
        return Path(home) / ".orbitnest"
    if plat.startswith("win"):
        base = environ.get("LOCALAPPDATA") or str(Path(home) / "AppData" / "Local")
        return Path(base) / "OrbitNest"
    xdg = environ.get("XDG_DATA_HOME")
    base_dir = Path(xdg) if xdg else Path(home) / ".local" / "share"
    return base_dir / "orbitnest"


def _is_weak_secret(value: str) -> bool:
    return len(value) < MIN_SECRET_LENGTH or value.strip().lower() in _PLACEHOLDER_SECRETS


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    env: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    data_dir: Path
    database_path: Path | None = None
    log_dir: Path | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True
    secret_key: SecretStr | None = None
    secret_from_env: bool = False
    allow_non_loopback: bool = False
    cookie_secure: Literal["auto", "always", "never"] = "auto"
    cors_origins: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    public_url: str | None = None
    trusted_proxy: bool = False
    max_request_bytes: int = Field(default=1_048_576, ge=1_024, le=64 * 1024 * 1024)
    session_idle_minutes: int = Field(default=480, ge=5, le=60 * 24 * 30)
    session_absolute_hours: int = Field(default=168, ge=1, le=24 * 90)
    # Workspace operations
    start_timeout_seconds: int = Field(default=60, ge=1, le=900)
    stop_grace_seconds: int = Field(default=10, ge=1, le=120)
    abandoned_after_minutes: int = Field(default=5, ge=1, le=240)
    maintenance_interval_seconds: int = Field(default=60, ge=0, le=3600)  # 0 disables the sweeper

    @field_validator("host")
    @classmethod
    def _host_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("host must not be empty")
        return v  # wildcard binds count as non-loopback and need the explicit opt-in below

    @field_validator("cors_origins")
    @classmethod
    def _cors_valid(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for origin in v:
            origin = origin.strip().rstrip("/")
            if not origin:
                continue
            if origin == "*":
                raise ValueError("wildcard CORS origin is not allowed")
            parts = urlsplit(origin)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path:
                raise ValueError(f"invalid CORS origin: {origin!r}")
            out.append(origin)
        return out

    @field_validator("public_url")
    @classmethod
    def _public_url_valid(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        parts = urlsplit(v.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("public_url must be an http(s) URL")
        return v.strip().rstrip("/")

    @model_validator(mode="after")
    def _safety_rules(self) -> Settings:
        loopback = self.host in LOOPBACK_HOSTS
        if not loopback:
            if not self.allow_non_loopback:
                raise ValueError(
                    "refusing to bind a non-loopback address; set ORBITNEST_ALLOW_NON_LOOPBACK=true "
                    "only behind a TLS reverse proxy"
                )
            if not AUTH_ENFORCED:
                raise ValueError(
                    "refusing to bind a non-loopback address: authentication is not enforced in this build"
                )
            if not self.secret_from_env:
                raise ValueError("a non-loopback bind requires ORBITNEST_SECRET_KEY from the environment")
        if self.env == "production":
            if not self.secret_from_env:
                raise ValueError("production requires ORBITNEST_SECRET_KEY")
            if self.cookie_secure == "never" and not loopback:
                raise ValueError("production must not disable Secure cookies on a non-loopback bind")
        if (
            self.secret_key is not None
            and self.secret_from_env
            and _is_weak_secret(self.secret_key.get_secret_value())
        ):
            raise ValueError(
                f"ORBITNEST_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters and not a placeholder"
            )
        return self

    # -- derived values -------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.database_path or (self.data_dir / "orbitnest.sqlite3")

    @property
    def workspaces_root(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def logs_path(self) -> Path:
        return self.log_dir or (self.data_dir / "logs")

    @property
    def effective_allowed_hosts(self) -> frozenset[str]:
        hosts = {"localhost", "127.0.0.1", "[::1]", "::1"}
        if self.host not in {"0.0.0.0", "::"}:  # noqa: S104 - comparison, not a bind
            hosts.add(self.host.strip("[]"))
        if self.public_url:
            hostname = urlsplit(self.public_url).hostname
            if hostname:
                hosts.add(hostname.lower())
        hosts.update(h.strip().lower() for h in self.allowed_hosts if h.strip())
        return frozenset(hosts)

    @property
    def secret_bytes(self) -> bytes:
        if self.secret_key is None:
            raise ConfigError("secret key has not been resolved")
        return self.secret_key.get_secret_value().encode("utf-8")

    # -- construction ---------------------------------------------------------------------
    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        sys_platform: str | None = None,
    ) -> Settings:
        env = os.environ if environ is None else environ
        raw: dict[str, object] = {}

        def pick(name: str) -> str | None:
            value = env.get(ENV_PREFIX + name.upper())
            return value if value is not None and value != "" else None

        for name in (
            "env",
            "host",
            "port",
            "log_level",
            "cookie_secure",
            "public_url",
            "max_request_bytes",
            "session_idle_minutes",
            "session_absolute_hours",
            "start_timeout_seconds",
            "stop_grace_seconds",
            "abandoned_after_minutes",
            "maintenance_interval_seconds",
        ):
            value = pick(name)
            if value is not None:
                raw[name] = value.upper() if name == "log_level" else value
        for name in ("log_json", "allow_non_loopback", "trusted_proxy"):
            value = pick(name)
            if value is not None:
                raw[name] = value
        for name in ("cors_origins", "allowed_hosts"):
            value = pick(name)
            if value is not None:
                raw[name] = [p for p in (s.strip() for s in value.split(",")) if p]
        data_dir = pick("data_dir")
        raw["data_dir"] = Path(data_dir) if data_dir else default_data_dir(env, sys_platform)
        for name in ("database_path", "log_dir"):
            value = pick(name)
            if value is not None:
                raw[name] = Path(value)
        secret = pick("secret_key")
        if secret is not None:
            raw["secret_key"] = SecretStr(secret)
            raw["secret_from_env"] = True
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            # Report field names and reasons only. Never echo input values (may hold secrets).
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg']}" for e in exc.errors()
            )
            raise ConfigError(f"invalid configuration: {problems}") from None

    def with_resolved_secret(self) -> Settings:
        """Return settings with a usable secret key.

        Production and any env-supplied key are used as-is. Otherwise: tests get an ephemeral
        random key; development loads or creates a random key stored with mode 0600.
        """
        if self.secret_key is not None:
            return self
        key = secrets.token_urlsafe(48) if self.env == "test" else _load_or_create_dev_secret(self.data_dir)
        return self.model_copy(update={"secret_key": SecretStr(key)})


def _load_or_create_dev_secret(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # best effort (e.g. Windows)
        data_dir.chmod(0o700)
    path = data_dir / "secret.key"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= MIN_SECRET_LENGTH:
            return value
    value = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return value
