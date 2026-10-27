-- 0004 two-factor authentication (TOTP) and one-time recovery codes.
-- The TOTP secret is stored sealed (encrypt-then-MAC under a key derived from the server secret).
-- Recovery codes are stored only as hashes and are single use.

CREATE TABLE user_totp (
    user_id      TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    secret_sealed TEXT NOT NULL,
    confirmed_at TEXT,                        -- NULL while enrolment is pending
    last_step    INTEGER NOT NULL DEFAULT 0,  -- last accepted 30-second step: a code is never accepted twice
    created_at   TEXT NOT NULL
) STRICT;

CREATE TABLE recovery_codes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at   TEXT
) STRICT;

CREATE INDEX idx_recovery_user ON recovery_codes (user_id);
