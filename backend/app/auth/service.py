"""Authentication and user-management business logic. HTTP concerns live in ``app.api``."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.auth import ratelimit, setup_token
from app.auth.passwords import (
    MAX_PASSWORD_BYTES,
    MAX_PASSWORD_CHARS,
    PasswordHasher,
    PasswordPolicyError,
    validate_password,
)
from app.auth.policy import ROLES
from app.core import errors
from app.core.config import Settings
from app.core.ids import from_iso, new_id, to_iso, utcnow
from app.db import Database
from app.models.user import User
from app.services import audit, settings_store
from app.services.audit import RequestContext

RESET_TTL = timedelta(minutes=30)
TOUCH_INTERVAL = timedelta(seconds=60)
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}\Z")
MAX_LOGIN_USERNAME = 64


@dataclass(frozen=True)
class AuthContext:
    user: User
    session_id: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validation(field: str, reason: str) -> errors.AppError:
    return errors.AppError(
        errors.VALIDATION_ERROR,
        "Request body is invalid.",
        422,
        [{"field": field, "reason": reason, "type": "value_error"}],
    )


def validate_username(username: str) -> str:
    if not USERNAME_RE.match(username):
        raise _validation(
            "username",
            "3-32 characters: letters, digits, '.', '_' or '-', starting with a letter or digit.",
        )
    return username


def _check_password(password: str, username: str | None, field: str = "password") -> None:
    try:
        validate_password(password, username)
    except PasswordPolicyError as exc:
        raise _validation(field, str(exc)) from None


def _rate_limited(retry_after: int) -> errors.AppError:
    return errors.AppError(
        errors.RATE_LIMITED,
        "Too many attempts. Try again later.",
        429,
        headers={"Retry-After": str(retry_after)},
    )


class AuthService:
    def __init__(
        self,
        db: Database,
        hasher: PasswordHasher,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.hasher = hasher
        self.settings = settings
        self.now = now
        self.limiter = ratelimit.RateLimiter(db)

    # -- queries ---------------------------------------------------------------------------
    def user_count(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def setup_required(self) -> bool:
        return self.user_count() == 0

    def get_user(self, user_id: str) -> User | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(row) if row else None

    def list_users(self, *, limit: int = 100, offset: int = 0) -> list[User]:
        limit = max(1, min(limit, 200))
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at, id LIMIT ? OFFSET ?", (limit, max(0, offset))
            ).fetchall()
        return [User.from_row(r) for r in rows]

    # -- CSRF -------------------------------------------------------------------------------
    def csrf_token(self, session_id: str) -> str:
        """Stateless per-session token: HMAC(secret, session id). Never stored."""
        digest = hmac.new(self.settings.secret_bytes, b"csrf:" + session_id.encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")

    def verify_csrf(self, session_id: str, supplied: str | None) -> bool:
        if not supplied:
            return False
        return hmac.compare_digest(self.csrf_token(session_id).encode(), supplied.encode("utf-8", "ignore"))

    # -- sessions ---------------------------------------------------------------------------
    def _create_session(self, conn: sqlite3.Connection, user_id: str, ctx: RequestContext) -> tuple[str, str]:
        now = self.now()
        token = secrets.token_urlsafe(32)
        session_id = new_id()
        absolute = now + timedelta(hours=self.settings.session_absolute_hours)
        idle = min(now + timedelta(minutes=self.settings.session_idle_minutes), absolute)
        conn.execute(
            "INSERT INTO sessions (id, token_hash, user_id, created_at, last_seen_at, idle_expires_at, "
            "expires_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                _hash_token(token),
                user_id,
                to_iso(now),
                to_iso(now),
                to_iso(idle),
                to_iso(absolute),
                ctx.ip,
                (ctx.user_agent or "")[:200] or None,
            ),
        )
        return token, session_id

    def resolve_session(self, token: str) -> AuthContext | None:
        if not token or len(token) > 256:
            return None
        now = self.now()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT s.id AS session_id, s.last_seen_at, s.expires_at, u.* FROM sessions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? "
                "AND s.idle_expires_at > ? AND u.is_active = 1",
                (_hash_token(token), to_iso(now), to_iso(now)),
            ).fetchone()
            if row is None:
                return None
            if now - from_iso(row["last_seen_at"]) > TOUCH_INTERVAL:
                idle = min(
                    now + timedelta(minutes=self.settings.session_idle_minutes),
                    from_iso(row["expires_at"]),
                )
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? WHERE id = ?",
                    (to_iso(now), to_iso(idle), row["session_id"]),
                )
        return AuthContext(user=User.from_row(row), session_id=row["session_id"])

    def revoke_session_by_id(self, session_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (to_iso(self.now()), session_id),
            )

    def _revoke_user_sessions(
        self, conn: sqlite3.Connection, user_id: str, *, except_session: str | None = None
    ) -> int:
        cur = conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL "
            "AND (? IS NULL OR id != ?)",
            (to_iso(self.now()), user_id, except_session, except_session),
        )
        return cur.rowcount

    def revoke_all_sessions(self) -> int:
        """Incident response: invalidate every session."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL", (to_iso(self.now()),)
            )
            audit.record(
                self.db,
                action="auth.sessions_revoked_all",
                outcome="success",
                details={"count": cur.rowcount, "via": "cli"},
                conn=conn,
            )
            return cur.rowcount

    def list_sessions(self, user_id: str, current_session_id: str | None) -> list[dict[str, object]]:
        now = to_iso(self.now())
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, last_seen_at, expires_at, ip, user_agent FROM sessions "
                "WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ? AND idle_expires_at > ? "
                "ORDER BY created_at DESC",
                (user_id, now, now),
            ).fetchall()
        return [{**dict(r), "current": r["id"] == current_session_id} for r in rows]

    def revoke_session_for(self, auth: AuthContext, public_id: str, ctx: RequestContext) -> None:
        """Revoke one of the caller's own sessions (admins may revoke anyone's). 404 otherwise."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE id = ? AND revoked_at IS NULL", (public_id,)
            ).fetchone()
            if row is None or (row["user_id"] != auth.user.id and auth.user.role != "admin"):
                raise errors.AppError(errors.NOT_FOUND, "Session not found.", 404)
            conn.execute("UPDATE sessions SET revoked_at = ? WHERE id = ?", (to_iso(self.now()), public_id))
            audit.record(
                self.db,
                action="auth.session_revoked",
                outcome="success",
                actor_user_id=auth.user.id,
                target_type="session",
                target_id=public_id,
                ctx=ctx,
                conn=conn,
            )

    # -- first-run setup -------------------------------------------------------------------
    def setup_admin(
        self,
        *,
        username: str,
        password: str,
        supplied_token: str,
        app_url: str | None,
        ctx: RequestContext,
    ) -> tuple[User, str, str]:
        ip_key = ctx.ip or "unknown"
        now = self.now()
        retry = self.limiter.retry_after(ratelimit.SETUP_IP, ip_key, now)
        if retry:
            raise _rate_limited(retry)
        if not self.setup_required():
            raise errors.AppError(errors.SETUP_UNAVAILABLE, "Setup has already been completed.", 409)
        if not setup_token.verify_setup_token(self.settings.data_dir, supplied_token):
            self.limiter.record_failure(ratelimit.SETUP_IP, ip_key, now)
            audit.record(
                self.db, action="auth.setup", outcome="denied", ctx=ctx, details={"reason": "bad_token"}
            )
            raise errors.AppError(errors.INVALID_SETUP_TOKEN, "Setup token is invalid.", 403)
        validate_username(username)
        _check_password(password, username)
        if app_url is not None:
            parts = urlsplit(app_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise _validation("app_url", "Must be an http(s) URL.")
            app_url = app_url.rstrip("/")
        password_hash = self.hasher.hash(password)
        user_id = new_id()
        with self.db.transaction() as conn:
            # BEGIN IMMEDIATE serialises concurrent setups; re-check under the write lock.
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
                raise errors.AppError(errors.SETUP_UNAVAILABLE, "Setup has already been completed.", 409)
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, 'admin')",
                (user_id, username, password_hash),
            )
            if app_url:
                settings_store.set_setting(self.db, "app_url", app_url, conn=conn)
            token, session_id = self._create_session(conn, user_id, ctx)
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (to_iso(self.now()), user_id))
            audit.record(
                self.db,
                action="auth.setup",
                outcome="success",
                actor_user_id=user_id,
                target_type="user",
                target_id=user_id,
                ctx=ctx,
                conn=conn,
            )
        setup_token.remove_setup_token(self.settings.data_dir)
        user = self.get_user(user_id)
        assert user is not None
        return user, token, session_id

    # -- login / logout --------------------------------------------------------------------
    def login(self, *, username: str, password: str, ctx: RequestContext) -> tuple[User, str, str]:
        now = self.now()
        username = username.strip()[:MAX_LOGIN_USERNAME]
        ip_key = ctx.ip or "unknown"
        retry = max(
            self.limiter.retry_after(ratelimit.LOGIN_USER, username, now),
            self.limiter.retry_after(ratelimit.LOGIN_IP, ip_key, now),
        )
        user_hash = ratelimit.key_hash(username)
        if retry:
            # Checked before the password so a correct guess during lockout confirms nothing.
            audit.record(
                self.db,
                action="auth.login",
                outcome="denied",
                ctx=ctx,
                details={"reason": "rate_limited", "username_hash": user_hash[:16]},
            )
            raise _rate_limited(retry)

        row = None
        verified = False
        if len(password) <= MAX_PASSWORD_CHARS and len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES:
            with self.db.connect() as conn:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row is not None:
                verified = self.hasher.verify(password, row["password_hash"])
            else:
                self.hasher.verify_dummy(password)
        if row is None or not verified or not row["is_active"]:
            self.limiter.record_failure(ratelimit.LOGIN_USER, username, now)
            self.limiter.record_failure(ratelimit.LOGIN_IP, ip_key, now)
            audit.record(
                self.db,
                action="auth.login",
                outcome="failure",
                actor_user_id=row["id"] if row is not None else None,
                ctx=ctx,
                details={"username_hash": user_hash[:16]},
            )
            raise errors.AppError(errors.INVALID_CREDENTIALS, "Invalid username or password.", 401)

        new_hash = self.hasher.hash(password) if self.hasher.needs_rehash(row["password_hash"]) else None
        with self.db.transaction() as conn:
            token, session_id = self._create_session(conn, row["id"], ctx)
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (to_iso(self.now()), row["id"]))
            if new_hash:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, row["id"]))
            audit.record(
                self.db,
                action="auth.login",
                outcome="success",
                actor_user_id=row["id"],
                ctx=ctx,
                details={"rehashed": bool(new_hash)},
                conn=conn,
            )
        self.limiter.clear(ratelimit.LOGIN_USER, username)
        user = self.get_user(row["id"])
        assert user is not None
        return user, token, session_id

    def logout(self, auth: AuthContext, ctx: RequestContext) -> None:
        self.revoke_session_by_id(auth.session_id)
        audit.record(self.db, action="auth.logout", outcome="success", actor_user_id=auth.user.id, ctx=ctx)

    # -- password change / reset -----------------------------------------------------------
    def change_password(self, auth: AuthContext, current: str, new: str, ctx: RequestContext) -> None:
        now = self.now()
        retry = self.limiter.retry_after(ratelimit.LOGIN_USER, auth.user.username, now)
        if retry:
            raise _rate_limited(retry)
        with self.db.connect() as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (auth.user.id,)).fetchone()
        ok = (
            row is not None
            and len(current) <= MAX_PASSWORD_CHARS
            and self.hasher.verify(current, row["password_hash"])
        )
        if not ok:
            self.limiter.record_failure(ratelimit.LOGIN_USER, auth.user.username, now)
            audit.record(
                self.db, action="auth.password_change", outcome="failure", actor_user_id=auth.user.id, ctx=ctx
            )
            raise errors.AppError(errors.INVALID_CREDENTIALS, "Current password is incorrect.", 401)
        _check_password(new, auth.user.username, "new_password")
        if hmac.compare_digest(current.encode(), new.encode()):
            raise _validation("new_password", "New password must differ from the current password.")
        new_hash = self.hasher.hash(new)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (new_hash, to_iso(self.now()), auth.user.id),
            )
            revoked = self._revoke_user_sessions(conn, auth.user.id, except_session=auth.session_id)
            audit.record(
                self.db,
                action="auth.password_change",
                outcome="success",
                actor_user_id=auth.user.id,
                ctx=ctx,
                details={"other_sessions_revoked": revoked},
                conn=conn,
            )
        self.limiter.clear(ratelimit.LOGIN_USER, auth.user.username)

    def issue_reset(self, actor: User, target_id: str, ctx: RequestContext) -> tuple[str, str]:
        """Admin issues a single-use, 30-minute reset token for a user. Returned once, stored hashed."""
        target = self.get_user(target_id)
        if target is None:
            raise errors.AppError(errors.NOT_FOUND, "User not found.", 404)
        token = secrets.token_urlsafe(32)
        now = self.now()
        expires = now + RESET_TTL
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM password_resets WHERE user_id = ? AND used_at IS NULL", (target_id,))
            conn.execute(
                "INSERT INTO password_resets (id, user_id, token_hash, created_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_id(), target_id, _hash_token(token), actor.id, to_iso(now), to_iso(expires)),
            )
            audit.record(
                self.db,
                action="auth.password_reset_issued",
                outcome="success",
                actor_user_id=actor.id,
                target_type="user",
                target_id=target_id,
                ctx=ctx,
                conn=conn,
            )
        return token, to_iso(expires)

    def redeem_reset(self, *, token: str, new_password: str, ctx: RequestContext) -> None:
        now = self.now()
        ip_key = ctx.ip or "unknown"
        retry = self.limiter.retry_after(ratelimit.RESET_IP, ip_key, now)
        if retry:
            raise _rate_limited(retry)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT r.id AS reset_id, r.user_id, u.username FROM password_resets r "
                "JOIN users u ON u.id = r.user_id "
                "WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > ? AND u.is_active = 1",
                (_hash_token(token[:256]), to_iso(now)),
            ).fetchone()
        if row is None:
            self.limiter.record_failure(ratelimit.RESET_IP, ip_key, now)
            audit.record(self.db, action="auth.password_reset_redeemed", outcome="failure", ctx=ctx)
            raise errors.AppError(errors.INVALID_RESET_TOKEN, "Reset token is invalid or expired.", 400)
        _check_password(new_password, row["username"], "new_password")
        new_hash = self.hasher.hash(new_password)
        with self.db.transaction() as conn:
            claimed = conn.execute(
                "UPDATE password_resets SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (to_iso(self.now()), row["reset_id"]),
            )
            if claimed.rowcount != 1:  # lost a race with another redemption
                raise errors.AppError(errors.INVALID_RESET_TOKEN, "Reset token is invalid or expired.", 400)
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (new_hash, to_iso(self.now()), row["user_id"]),
            )
            self._revoke_user_sessions(conn, row["user_id"])
            audit.record(
                self.db,
                action="auth.password_reset_redeemed",
                outcome="success",
                actor_user_id=row["user_id"],
                ctx=ctx,
                conn=conn,
            )
        self.limiter.clear(ratelimit.LOGIN_USER, row["username"])

    # -- user administration ---------------------------------------------------------------
    def create_user(
        self, actor: User, *, username: str, password: str, role: str, ctx: RequestContext
    ) -> User:
        validate_username(username)
        if role not in ROLES:
            raise _validation("role", "Unknown role.")
        _check_password(password, username)
        password_hash = self.hasher.hash(password)
        user_id = new_id()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, ?)",
                    (user_id, username, password_hash, role),
                )
                audit.record(
                    self.db,
                    action="user.create",
                    outcome="success",
                    actor_user_id=actor.id,
                    target_type="user",
                    target_id=user_id,
                    ctx=ctx,
                    details={"role": role},
                    conn=conn,
                )
        except sqlite3.IntegrityError:
            raise errors.AppError(errors.CONFLICT, "That username is already taken.", 409) from None
        user = self.get_user(user_id)
        assert user is not None
        return user

    def update_user(
        self,
        actor: User,
        user_id: str,
        *,
        role: str | None,
        is_active: bool | None,
        ctx: RequestContext,
    ) -> User:
        if role is not None and role not in ROLES:
            raise _validation("role", "Unknown role.")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise errors.AppError(errors.NOT_FOUND, "User not found.", 404)
            new_role = role if role is not None else row["role"]
            new_active = int(is_active) if is_active is not None else row["is_active"]
            loses_admin = (
                row["role"] == "admin" and row["is_active"] and (new_role != "admin" or not new_active)
            )
            if loses_admin:
                others = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1 AND id != ?",
                    (user_id,),
                ).fetchone()[0]
                if others == 0:
                    raise errors.AppError(
                        errors.LAST_ADMIN, "At least one active administrator must remain.", 409
                    )
            conn.execute(
                "UPDATE users SET role = ?, is_active = ?, updated_at = ? WHERE id = ?",
                (new_role, new_active, to_iso(self.now()), user_id),
            )
            revoked = 0
            if new_role != row["role"] or new_active != row["is_active"]:
                # Any privilege change takes effect immediately: existing sessions end.
                revoked = self._revoke_user_sessions(conn, user_id)
            audit.record(
                self.db,
                action="user.update",
                outcome="success",
                actor_user_id=actor.id,
                target_type="user",
                target_id=user_id,
                ctx=ctx,
                details={
                    "role": [row["role"], new_role],
                    "is_active": [bool(row["is_active"]), bool(new_active)],
                    "sessions_revoked": revoked,
                },
                conn=conn,
            )
        user = self.get_user(user_id)
        assert user is not None
        return user

    # -- local recovery (CLI; requires shell access to the host) ---------------------------------
    def create_admin_local(self, username: str, password: str) -> User:
        validate_username(username)
        _check_password(password, username)
        user_id = new_id()
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, 'admin')",
                    (user_id, username, self.hasher.hash(password)),
                )
                audit.record(
                    self.db,
                    action="user.create",
                    outcome="success",
                    target_type="user",
                    target_id=user_id,
                    details={"role": "admin", "via": "cli"},
                    conn=conn,
                )
        except sqlite3.IntegrityError:
            raise errors.AppError(errors.CONFLICT, "That username is already taken.", 409) from None
        setup_token.remove_setup_token(self.settings.data_dir)
        user = self.get_user(user_id)
        assert user is not None
        return user

    def force_reset_password(self, username: str, new_password: str, *, reactivate: bool = False) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            raise errors.AppError(errors.NOT_FOUND, "User not found.", 404)
        _check_password(new_password, row["username"], "new_password")
        new_hash = self.hasher.hash(new_password)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ?, "
                "is_active = CASE WHEN ? THEN 1 ELSE is_active END WHERE id = ?",
                (new_hash, to_iso(self.now()), int(reactivate), row["id"]),
            )
            revoked = self._revoke_user_sessions(conn, row["id"])
            conn.execute("DELETE FROM password_resets WHERE user_id = ?", (row["id"],))
            audit.record(
                self.db,
                action="auth.recovery_password_reset",
                outcome="success",
                target_type="user",
                target_id=row["id"],
                details={"via": "cli", "sessions_revoked": revoked, "reactivated": reactivate},
                conn=conn,
            )
        self.limiter.clear(ratelimit.LOGIN_USER, row["username"])
