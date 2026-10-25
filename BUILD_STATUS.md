# BUILD_STATUS

Last updated: 2026-10-25 (Phases 0-2 complete)

## Environment of the build sandbox (verified by inspection)

| Item | Result |
|---|---|
| OS / arch | Linux 6.18, x86_64 |
| Python | 3.13.17 (only version tested; code targets >=3.11 but 3.11/3.12 are **not tested**) |
| Node / npm | 22.23.4 / 10.9.9 (registry unreachable) |
| Docker CLI | 29.9.2 present; **no daemon reachable** |
| Package index | **PyPI and npm blocked by sandbox policy (HTTP 403 from the egress proxy)**; not worked around |
| Termux / Windows | neither; those paths are tested by simulating inputs only |

## Deviations from the spec (all environmental; see ADR-001/002)

| Spec | Used instead | Why |
|---|---|---|
| FastAPI | Starlette 1.7 + Pydantic 2.13 | FastAPI not installable. No auto-generated OpenAPI yet |
| SQLAlchemy 2 + Alembic | stdlib `sqlite3` + checksum-verified SQL migration runner | not installable. **No PostgreSQL support yet** |
| Argon2id | `PasswordHasher` interface; **scrypt (N=2^15, r=8, p=3) is what actually ran in tests**; Argon2id path is coded but **untested** (argon2-cffi unavailable) | argon2-cffi not installable |
| pytest, httpx | stdlib `unittest` + custom ASGI client | not installable |
| React + TS + Vite | **not built** | npm unreachable. Phase 5 is blocked; see below |
| mypy / pyright | **not run** | not installed |

## Phase status

| Phase | Status |
|---|---|
| 0 Inspect and plan | Done |
| 1 Foundation | Done (details below) |
| 2 Auth | Done (details below) |
| 3 Domain and runtimes | Not started |
| 4 Lifecycle | Not started |
| 5 Frontend | **Blocked** on npm. Fallback to decide: dependency-free static frontend |
| 6 File manager | Not started |
| 7 Terminal | Not started |
| 8 Streaming | Not started; will stay disabled unless verifiable |
| 9 Packaging | Not started |
| 10 Security / QA | Not started |
| 11 Release | Not started |

## Phase 1: what exists

- Validated config (`app/core/config.py`): loopback default, production requires a strong
  `ORBITNEST_SECRET_KEY`, non-loopback bind refused (also refused while `AUTH_ENFORCED` is False),
  wildcard CORS refused, error messages never echo secret values.
- Redacting JSON logging with rotation (`app/core/logging.py`).
- Error schema with stable codes and request IDs (`app/core/errors.py`).
- Edge/host/body-limit middleware, security headers, CSP, HSTS on HTTPS only
  (`app/core/middleware.py`).
- Platform capability detection with injectable inputs (`app/core/platform.py`).
- SQLite layer and migration runner with tamper detection (`app/db/`), migration `0001`
  (system_settings, audit_events).
- Routes: `GET /api/v1/health`, `GET /api/v1/ready`, `GET /api/v1/system/capabilities`
  (loopback clients only until Phase 2 adds auth).
- CLI: `python -m app {serve,migrate,migration-status,capabilities,check-config}`.

## Phase 2: what exists

- First-run admin setup guarded by a filesystem token; one-shot; concurrency-safe.
- Login with uniform errors, dummy-hash timing equalisation, per-username and per-IP lockout with
  exponential backoff, hashed session tokens, idle + absolute expiry, session list/revoke.
- CSRF (HMAC token + Origin check), security of cookies (HttpOnly, SameSite=Lax, Secure on HTTPS).
- RBAC (`admin`/`user`/`read_only`), user create/update (last-admin protection, sessions revoked on
  privilege change), password change, admin-issued single-use reset tokens (30 min, stored hashed).
- Audit events for setup, login, logout, password changes/resets, user changes, CSRF/authz denials;
  `GET /api/v1/audit-events` (admin, keyset pagination, filters).
- Recovery CLI: `setup-token`, `create-admin`, `reset-password`, `revoke-sessions`.
- Route-guard test: every registered route must reject anonymous requests unless listed in
  `PUBLIC_ROUTES`. `AUTH_ENFORCED = True`; non-loopback bind now possible only with explicit
  opt-in and an environment secret.

## Tests (actually run)

```
cd backend && python3 -m unittest discover -s tests -t .
Ran 167 tests ... OK
ruff check .           -> All checks passed
```

- Mutation check (manual script, not committed): 12 deliberate breakages of security controls
  (CSRF, lockout, session revocation/expiry filters, permission check, Origin check, X-Forwarded-For
  trust, reset-token reuse, unauthenticated capabilities, setup-token check, last-admin guard,
  password-change revocation). All were caught except two that are redundant by design: the reset
  token is protected by two independent guards (either alone can be removed without a test failing;
  removing both is caught), and the session `is_active` filter is backed by session revocation at
  deactivation.
- Real-socket smoke test (uvicorn + curl): anonymous 401, setup with CLI-provided token, cookie
  HttpOnly, CSRF rejection, cross-origin rejection, audit trail, restart keeps sessions and keeps
  setup closed, no passwords/tokens in log files.

**Not run / not verifiable here:** mypy/pyright, Python 3.11/3.12, Argon2id backend, Windows,
Termux, WSL, real Docker, PostgreSQL, any browser or frontend test, load/timing analysis of the
login path (timing equalisation is coded, not measured).

## Known limitations right now

- The synchronous auth handlers run short SQLite queries on the event loop; password hashing is
  offloaded to a threadpool. Fine for the target scale, not load-tested.
- No OIDC, no 2FA, no email-based reset.
- Desktop streaming and browser terminal are reported as `not_implemented`, not faked.
- Docker adapter (Phase 3) will be unit-tested against a mocked CLI only and labelled unverified.
