# Architecture

OrbitNest is a **control plane plus pluggable workspace runtimes**.

```
Browser / mobile client
        |  HTTPS (reverse proxy in remote deployments)
        v
+-------------------- Control plane (Python, ASGI) --------------------+
|  API /api/v1  |  auth + RBAC  |  services  |  audit  |  settings     |
|                         |                                           |
|                 Runtime adapter interface                           |
|        fake (tests) | docker | termux-local | podman (optional)      |
+---------------------------------------------------------------------+
        |                         |                       |
   Docker Engine / Desktop     local processes        remote host
```

## Layering rules

- `api/` handles HTTP only: parse, authorize, call a service, serialize.
- `services/` holds business logic and enforces authorization and ownership.
- `runtimes/` is the only code that touches containers or processes. It receives validated,
  allowlisted specs, never raw user input, and never uses a shell.
- `db/` is the only code that issues SQL, always parameterized.
- `core/` holds config, logging, errors, and platform detection.

## Architecture decision records

### ADR-001: Web framework (deviation from spec, environmental)

- **Spec:** FastAPI + SQLAlchemy 2 + Alembic.
- **Situation:** the build sandbox cannot reach PyPI (policy 403) or npm. Installed and usable:
  Starlette 1.7, Pydantic 2.13, uvicorn, anyio, python-multipart, itsdangerous, stdlib `sqlite3`.
- **Decision:** build on Starlette + Pydantic v2 (the exact layers FastAPI itself is built on),
  stdlib `sqlite3` with a small versioned-SQL migration runner, and stdlib `unittest`.
- **Consequences:** no auto-generated OpenAPI, no PostgreSQL support yet, no SQLAlchemy models.
  The `db/` module is the only SQL boundary, so moving to SQLAlchemy/Alembic/PostgreSQL later is a
  contained change. Pydantic schemas are kept so a FastAPI port is mechanical.
  This ADR should be revisited as soon as the package index is reachable.

### ADR-002: Password hashing

- **Spec:** Argon2id or another suitable maintained library.
- **Decision:** a `PasswordHasher` interface. Uses `argon2-cffi` (Argon2id) when importable;
  otherwise stdlib `hashlib.scrypt` with parameters recorded in the hash string so they can be
  upgraded. The active scheme is exposed in diagnostics; hashes self-describe and are re-hashed on
  login when parameters are outdated.

### ADR-003: Platform-first design

Platform detection runs at startup and on demand. Features that a platform cannot honestly
provide are disabled with a reason, never faked. Termux has no container runtime assumption.

### ADR-004: Loopback by default

The server binds `127.0.0.1` by default. Binding anything else requires explicit configuration,
and in production mode startup refuses a missing/weak secret key.

### ADR-005: Terminal and streaming run inside workspaces

The terminal PTY is started via the runtime adapter inside the workspace (`docker exec`-style,
argv only). A host shell is never exposed to non-admin users.

## MVP scope

In: auth, workspace lifecycle against fake and (unverified here) Docker adapters, persistence
semantics, file manager, terminal, capability reporting, dashboard.
Out until verified: streamed desktops/browsers (Phase 8), OIDC, Podman, PostgreSQL.

## Authentication design (Phase 2)

- **First run:** `python -m app serve` creates `setup-token` (0600) in the data directory and logs
  only the file name. `POST /api/v1/auth/setup` needs that token, works exactly once, and removes
  the file. Loopback is *not* treated as trusted (a same-host proxy would make everyone loopback).
- **Sessions:** random 256-bit cookie value, stored only as SHA-256; idle and absolute expiry;
  revoked on logout, password change/reset, role change and deactivation.
- **CSRF:** every unsafe authenticated request needs `X-CSRF-Token` (HMAC of the session id with
  the secret key, never stored) and an acceptable `Origin`. Login/setup/reset check `Origin` only.
- **Lockout:** DB-backed failure counts, exponential backoff capped at 15 minutes, keyed on the
  attempted username and on the client IP; the lock check runs *before* password verification.
- **Roles:** `admin`, `user`, `read_only`; `app/auth/policy.py`. Enforced in handlers via
  `authenticate(request, permission=...)`; workspace ownership rules (`can_access_workspace`) are
  used by the workspace service in Phase 3.
- **Recovery (needs shell access to the host):** `python -m app setup-token | create-admin <name> |
  reset-password <name> | revoke-sessions`. All recovery actions are audited.
- **Known gaps:** no OIDC (optional in spec, not started); no 2FA; no email-based self-service
  reset (admin-issued single-use tokens only, because there is no mail transport).
