-- 0002 auth: users, sessions, login-failure tracking, password reset tokens.
-- Only hashes of session/reset tokens are stored. Passwords are stored as self-describing hashes.

CREATE TABLE users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user', 'read_only')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_login_at TEXT
) STRICT;

CREATE UNIQUE INDEX idx_users_username ON users (username);

CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,            -- public identifier (safe to show), NOT the secret
    token_hash      TEXT NOT NULL UNIQUE,        -- sha256 of the secret cookie value
    user_id         TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    expires_at      TEXT NOT NULL,               -- absolute lifetime
    revoked_at      TEXT,
    ip              TEXT,
    user_agent      TEXT
) STRICT;

CREATE INDEX idx_sessions_user ON sessions (user_id);
CREATE INDEX idx_sessions_expiry ON sessions (expires_at);

-- One row per failed attempt. scope: login_user | login_ip | setup_ip | reset_ip.
CREATE TABLE auth_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    occurred_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_auth_failures_lookup ON auth_failures (scope, key_hash, occurred_at);

CREATE TABLE password_resets (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT REFERENCES users (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
) STRICT;

CREATE INDEX idx_password_resets_user ON password_resets (user_id);
