# BUILD_STATUS

Last updated: 2026-10-25 (Phase 0 and Phase 1 complete)

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
| Argon2id | `PasswordHasher` interface; scrypt fallback (Phase 2) | argon2-cffi not installable |
| pytest, httpx | stdlib `unittest` + custom ASGI client | not installable |
| React + TS + Vite | **not built** | npm unreachable. Phase 5 is blocked; see below |
| mypy / pyright | **not run** | not installed |

## Phase status

| Phase | Status |
|---|---|
| 0 Inspect and plan | Done |
| 1 Foundation | Done (details below) |
| 2 Auth | Next |
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

## Tests (actually run)

```
cd backend && python3 -m unittest discover -s tests -t .
Ran 74 tests ... OK
ruff check .          -> All checks passed
ruff format --check . -> clean
```

Also verified by hand: real `python3 -m app serve` answered health/ready/capabilities over a
socket with security headers; bad Host header got 400; unknown route got the JSON error schema; DB
file created with mode 600; non-loopback bind and production-without-secret both refused (exit 2).
Mutation check: disabling log redaction or the host check made the relevant tests fail.

**Not run / not verifiable here:** mypy/pyright, Python 3.11/3.12, Windows, Termux, WSL, real
Docker, PostgreSQL, any browser or frontend test.

## Known limitations right now

- Nothing is authenticated yet; that is why the server only binds loopback and capabilities is
  loopback-only.
- Desktop streaming and browser terminal are reported as `not_implemented`, not faked.
- Docker adapter (Phase 3) will be unit-tested against a mocked CLI only and labelled unverified.
