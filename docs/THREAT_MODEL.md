# Threat model (living document)

## Assets

1. The host running the Docker daemon (root-equivalent).
2. User workspace data (project files, volumes).
3. Credentials: password hashes, session tokens, deployment secrets.
4. Availability of the control plane.

## Trust boundaries

| Boundary | Trusted side | Untrusted side |
|---|---|---|
| Browser to API | API | Everything in the request (headers, body, cookies, WebSocket frames) |
| API to runtime adapter | Adapter | Template/workspace parameters from users |
| Workspace to host | Host | Everything running in a workspace |
| Workspace to control plane | Control plane | Workspace network traffic |
| Reverse proxy to app | App | Forwarded headers unless the proxy is explicitly trusted |

## Threats and mitigations

| # | Threat | Mitigation | Status |
|---|---|---|---|
| T1 | Control-plane compromise becomes host root via Docker | Narrow adapter; no docker.sock in workspaces; allowlisted images/options; no `--privileged`; documented that docker-group membership is root-equivalent | Adapter implemented; flags checked against real CLI help; **not verified against a real daemon** |
| T2 | Container escape / lateral movement | Non-root user, dropped caps, `no-new-privileges`, PID/memory/CPU limits, isolated network, no host mounts | Adapter implemented (same caveat as T1) |
| T3 | Workspace reaches control plane or cloud metadata | Dedicated network, egress rules, block 169.254.169.254 | Bridge with ICC disabled implemented; **egress/metadata blocking is NOT enforced by the adapter** (deployment firewall, Phase 9) |
| T4 | Credential stuffing / brute force | Argon2id-class hashing (scrypt fallback here), rate limit with backoff, uniform login errors | Implemented and tested (Phase 2) |
| T5 | Session theft / fixation | HttpOnly, SameSite cookies, Secure on HTTPS, new session per login, hashed tokens at rest, revocation | Implemented and tested (Phase 2) |
| T6 | CSRF | Per-session HMAC CSRF token on every unsafe authenticated request, plus Origin check; login/setup/reset check Origin | Implemented and tested (Phase 2) |
| T7 | IDOR/BOLA | Ownership check in the service layer on every workspace access; opaque IDs | Implemented and tested (service + HTTP; foreign ids return 404 and are audited; mutant caught) |
| T8 | Path traversal / symlink escape in file manager | Canonical-path containment, reject symlinks that resolve outside root | Implemented and tested (Phase 6): openat-style walk with O_NOFOLLOW and dir fds, so symlinks are never followed; atomic uploads; POSIX only (unsupported on native Windows) |
| T9 | Command injection | No `shell=True`; argv lists only; schema-validated inputs | all phases |
| T10 | SQL injection | Parameterized queries only | all phases |
| T11 | XSS | Strict CSP, no inline script, output encoding in the UI | Implemented (Phase 5): CSP without inline script/style, UI built with createElement/textContent only (no innerHTML), CSRF token kept in memory. Verified in Chromium: no CSP violations, hostile file name rendered inert. No automated browser test in the repo |
| T12 | WebSocket auth bypass / cross-site WebSocket hijack | Authenticate each connection, validate Origin, authorize owner/admin | Implemented and tested (Phase 7): session-cookie auth plus Origin check on the WebSocket handshake, owner/admin authorization, limits per workspace/user. **The real wire protocol was not exercised** (no `websockets` package in the build sandbox); tests drive the ASGI interface |
| T13 | Secret leakage in logs/errors | Redaction filter, no secrets in URLs | Phase 1 |
| T14 | Insecure default config | Startup validation fails closed in production; no generated predictable secrets | Phase 1 |
| T15 | Unauthenticated admin surface on a public interface | Refuse non-loopback bind without explicit opt-in and an env secret; route-guard test fails if any route is public without being allowlisted | Implemented and tested |
| T16 | Resource exhaustion | Per-user quotas, request size limits, bounded buffers | Per-user workspace/running quotas, request and upload size limits, terminal limits (4 per workspace, 8 per user), idle/max terminal lifetime. Filesystem storage quota is reporting-only; a workload can still fill the disk (needs XFS project quotas or per-workspace filesystems) |
| T18 | Stream gateway abuse (cross-user viewing, WS hijack, port exposure) | Cookie auth + Origin check, owner/admin with WORKSPACE_WRITE, VNC only on container loopback bridged via `docker exec` (no host port), asset allowlist from a root-owned path, 1 MiB frame cap, 2/workspace and 4/user limits, 8h cap, closed on workspace stop | Implemented; tested against fakes only |
| T19 | Stolen or guessed password | Optional TOTP (single-use steps via compare-and-set, ±1 step), hashed single-use recovery codes, shared lockout counters, password re-entry to change 2FA, sessions revoked on admin/CLI reset, secret sealed at rest | Implemented and tested |
| T17 | Termux/PRoot mistaken for a sandbox | UI and docs label it "no isolation"; no claim otherwise | Mitigated by labelling: capability report, API (`isolation: none`) and UI call local mode "no isolation"; local terminals are admin-only; docs/INSTALL.md repeats it |

## Residual risks (always listed)

- TOTP secrets are sealed with a key derived from the server secret: protects a copied DB/backup, not an attacker who also has the secret.
- A browser inside a container can reach whatever its network policy allows; Tor Browser in a container is not a substitute for Tails-grade anonymity.
- Streaming and image builds have not been exercised on real Docker.

- Login lockout is keyed on the attempted username, so an attacker can temporarily lock a known
  username (bounded to 15 minutes, per-IP limits also apply). Chosen over leaking which usernames exist.
- Rate limiting is per source IP as seen by the app; behind a proxy set `ORBITNEST_TRUSTED_PROXY`
  correctly or all clients share one IP.
- First-run setup requires a token read from the data directory, so a network attacker cannot
  claim the admin account first; anyone with read access to that directory can.
- Anyone who can create Docker containers on the host can become root on it.
- PRoot and Termux-local mode provide no meaningful isolation.
- Disposable-workspace deletion is ordinary filesystem deletion, not cryptographic erasure.
- Kernel/runtime vulnerabilities can defeat container isolation.
- A process started with `docker exec` for a terminal can outlive the browser connection if the
  control plane is killed abruptly; the container is the boundary, and stopping the workspace ends it.
- Backup archives contain password hashes and the signing key; whoever holds one can attack offline.
- Mounting the Docker socket into a control-plane container (deploy/compose) is root-equivalent.

## Incident-response checklist (starter)

1. Contain: stop the control plane or block ingress at the proxy/firewall.
2. Revoke: invalidate all sessions (`python -m app revoke-sessions`), rotate the secret key (this also invalidates all CSRF tokens).
3. Preserve: copy logs and the audit table before cleanup.
4. Assess: list workspaces and containers created in the window; check for privileged or unexpected options.
5. Recover: rebuild from known-good images, restore data from backup, force password resets.
6. Review: update this model and add a regression test.
