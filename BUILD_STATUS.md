# BUILD_STATUS

Last updated: 2026-10-25 (Phases 0-4 complete)

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
| React + TS + Vite | Replaced by a dependency-free vanilla ES-module frontend (`backend/app/web`) | npm unreachable. No xterm.js either: a small in-repo VT emulator (`js/vt.js`) renders the terminal |
| mypy / pyright | **not run** | not installed |

## Phase status

| Phase | Status |
|---|---|
| 0 Inspect and plan | Done |
| 1 Foundation | Done (details below) |
| 2 Auth | Done (details below) |
| 3 Domain and runtimes | Done (fake + local verified with real processes; Docker adapter **unverified against a daemon**) |
| 4 Lifecycle and persistence | Done (details below) |
| 5 Frontend | Done as a dependency-free SPA; checked by hand in Chromium (flows, CSP, phone layout); only the VT emulator has automated tests |
| 6 File manager | Done (symlink-safe, POSIX only; tested incl. real-socket 7 MB round trip) |
| 7 Terminal | Done at ASGI level; **real WebSocket wire protocol and a live terminal in a browser never ran** (no `websockets` package, no Docker here) |
| 8 Streaming | **Not implemented, deliberately.** Reported as `not_implemented`; browser/desktop templates cannot be created |
| 9 Packaging | Scripts, systemd, Caddy, Nginx, compose, backup/restore, docs written. Only backup/restore is tested; installers are syntax-checked only |
| 10 Security / QA | In progress: threat model updated, CI workflow added, grep review done; no external review or pen test |
| 11 Release | Not tagged: unverified on every target platform. Treat as 0.1.0 pre-release |

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
- Routes: `GET /api/v1/health`, `GET /api/v1/ready` (public), `GET /api/v1/system/capabilities`
  (authenticated since Phase 2).
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

## Phases 3-4: what exists

- Domain: ULID ids, state machine with atomic compare-and-set transitions
  (`WorkspaceStore.transition`), 9 built-in templates + admin custom templates (validated; no
  local-process execution; `custom-` ids), resource profiles (users never supply raw numbers),
  platform-aware availability (`templates.evaluate`) with honest reasons/remediation.
- Runtimes: `fake` (tests), `docker` (argv-only, cap-drop ALL, no-new-privileges, non-root, pid/mem/cpu
  limits, only mount is the data dir; flags checked against the real `docker --help`), `local`
  (Termux: **no isolation**, process-group kill, env scrubbing, PID-reuse guard).
  Podman and proot are not implemented.
- Service (`app/services/workspaces.py`): create/start/stop/restart/reset/delete/update/activity.
  Quotas (workspaces and concurrently active per user) are enforced inside the write transaction.
  Ownership is enforced in the service (foreign ids -> 404, audited as `workspace.access_denied`).
  Idempotency keys, per-owner unique names, authorized-use acknowledgement for security profiles.
- Persistence: persistent data survives delete unless `delete_data` **and** the exact name are given;
  disposable data is wiped on stop and delete; reset keeps data unless wipe + name confirmation.
- Maintenance: idle marking/stop, boot + periodic reconciliation (abandoned jobs -> `failed`, dead
  runtime -> `failed`), orphan detection. `python -m app cleanup` is a dry run; `--apply` removes
  orphaned runtime resources and directories already marked purged. Retained data and directories with
  no DB record are only reported, never deleted. The periodic thread only reports orphans.
- API: `/templates` (+admin custom CRUD), `/workspaces` (+start/stop/restart/reset/activity/events),
  `/settings/policy` (admin). Create/start/etc. return 202; progress is read via GET.

## Tests (actually run)



```
cd backend && python3 -m unittest discover -s tests -t .
Ran 371 tests ... OK
node --test frontend-tests/vt.test.mjs  -> 9 pass
ruff check .           -> All checks passed
```

- Mutation check (manual script, not committed): 13 deliberate breakages of security controls
  (CSRF, lockout, revocation and inactive-user filters on sessions, permission check, Origin check,
  X-Forwarded-For trust, reset-token reuse, reset for deactivated users, unauthenticated
  capabilities, setup-token check, last-admin guard, password-change revocation). The first run
  found two survivors: (a) removing only one of the reset token's two independent guards (removing
  both is caught, so this is defence in depth, not a gap), and (b) the session `is_active` filter and
  reset-for-deactivated-user were untested; tests were added and those mutants are now caught.

- Real-socket smoke test (uvicorn + curl): anonymous 401, setup with CLI-provided token, cookie
  HttpOnly, CSRF rejection, cross-origin rejection, audit trail, restart keeps sessions and keeps
  setup closed, no passwords/tokens in log files.

- Phase 3-4 mutation check: removing the ownership check, the workspace-quota check, or the CAS
  status guard each makes tests fail.
- Real-socket smoke test of the workspace API: anonymous 401, CSRF 403, and `template_unavailable`
  with the Docker remediation text (this sandbox has no Docker daemon, so **no workspace was ever
  started for real**).

**Not run / not verifiable here:** mypy/pyright, Python 3.11/3.12, Argon2id backend, Windows,
Termux, WSL, real Docker, PostgreSQL, any browser or frontend test, load/timing analysis of the
login path (timing equalisation is coded, not measured).

## Known limitations right now

- The synchronous auth handlers run short SQLite queries on the event loop; password hashing is
  offloaded to a threadpool. Fine for the target scale, not load-tested.
- No OIDC, no email-based reset. Optional TOTP 2FA with recovery codes exists (no QR code: users paste the setup key).
- Browser/desktop streaming (Chromium, Firefox, Tor Browser, Ubuntu desktop) is implemented as a VNC-over-`docker exec` gateway with a same-origin noVNC client. **Verified only against a stub RFB module and a fake runtime**: never run against a real Docker daemon, real VNC server, real noVNC, or a built image. The images must be built locally (`python -m app build-images`) and templates show "image not built" until then.
- The Tor Browser image verifies the download signature and fails closed; the signing-key fingerprint in `docker/stream-tor/Dockerfile` was written from memory. Check it against support.torproject.org before trusting it.
- Each browser/desktop workspace needs roughly 1-2 GB RAM (shared-memory size is set per template).
- Terminal availability: reported available in Docker modes, admin-only/limited in Termux.
- Terminals need the `websockets` package (in requirements.txt, never installed or run in the build sandbox).
- Processes started by `docker exec` for a terminal may outlive an abruptly killed control plane; stopping the workspace ends them.
- The file manager is POSIX only; on native Windows it is reported unsupported (run in WSL 2).
- Installers (Linux, Termux, Windows/WSL) and deploy configs were never run on their targets.
- Docker adapter is tested against a scripted CLI and the real `docker --help` only; first use on a
  real host is a test.
- Storage quota is for reporting/file-manager limits only; a workload can still write beyond it
  (needs XFS project quotas or per-workspace filesystems).
- Network policy is `none` or an ICC-disabled bridge. Egress filtering and cloud-metadata blocking
  are deployment concerns (firewall rules), not enforced by the adapter.
- Runtime adapters are chosen once, lazily, from detected capabilities: changing the platform
  (e.g. starting Docker later) needs a service restart.
- Deleting a persistent workspace retains its data on disk but there is no UI/API to re-attach it yet.

## Phases 5-9: what exists

- Frontend served by the app from `backend/app/web` (strict CSP, no inline script, no innerHTML,
  CSRF token in memory): setup/login/reset, workspace dashboard and detail, file manager, terminal
  tab, admin users/policy/audit views. Deviation: no React/TS/Vite/xterm.js (npm unreachable).
- File manager: list/read/upload/download/mkdir/rename/delete under a workspace's data directory,
  works while the workspace is stopped, per-route upload limit.
- Terminal: PTY over WebSocket, cookie auth + Origin check, limits and idle/max lifetime, local
  runtime shells admin-only.
- Packaging: `scripts/install-{linux.sh,termux.sh,windows.ps1}`, `deploy/{systemd,caddy,nginx,compose}`,
  `python -m app backup|restore` (tested: round trip, hostile archives, no overwrite), `docs/INSTALL.md`.
- CI: `.github/workflows/ci.yml` (never run; written for GitHub Actions).

## Update: streaming, Tor, UI, hardening

- 412 Python tests, ruff clean, 9 node tests at last run. New: stream gateway, image build plan, backup/restore, TOTP (RFC 6238 vectors), MFA flows incl. replay/lockout/CSRF, regex-anchor regression (`\Z` instead of `$` across all validators).
- An automated independent security review was attempted but aborted by an API rate limit; **no independent review has been done**. Treat the threat model as the author's own.

## Update: privacy labelling, lab, image installer, tuning

- Browser templates now carry a privacy tag in the UI: "Uses your real IP" (Chromium, Firefox, desktops) versus "Routed through Tor". Chromium/Firefox are NOT anonymous; the Tor template is the only anonymising one, and it is Tor Browser in a container, not Tails.
- Security Lab (CLI, `security-lab`) and Kali desktop (`desktop-kali`): locally built Kali images, authorized-use rules shown and required, optional capabilities NET_RAW / NET_ADMIN / SYS_PTRACE per workspace (audited), run as root inside an unprivileged container. Verified by argv/unit tests only; never built or run against Docker.
- Admin one-click "Install image" (build with live log, cancel, one at a time). Tested with a fake planner, not a real `docker build`.
- Performance: stream templates need at least "medium"; Chromium/Firefox launch with lighter flags and 16-bit colour, x11vnc polls less, noVNC uses lower compression. These are untested guesses at what helps; 1 GB cannot make Chromium feel like 2 GB. Per-workspace override file: `.orbitnest/session.env` in the workspace folder.
- 428 Python tests pass, ruff clean.
