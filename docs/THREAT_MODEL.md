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
| T1 | Control-plane compromise becomes host root via Docker | Narrow adapter; no docker.sock in workspaces; allowlisted images/options; no `--privileged`; documented that docker-group membership is root-equivalent | Design (Phase 3) |
| T2 | Container escape / lateral movement | Non-root user, dropped caps, `no-new-privileges`, PID/memory/CPU limits, isolated network, no host mounts | Design (Phase 3) |
| T3 | Workspace reaches control plane or cloud metadata | Dedicated network, egress rules, block 169.254.169.254 | Design (Phase 3/9) |
| T4 | Credential stuffing / brute force | Argon2id-class hashing (scrypt fallback here), rate limit with backoff, uniform login errors | Phase 2 |
| T5 | Session theft / fixation | HttpOnly, SameSite cookies, Secure on HTTPS, rotate on login, hashed tokens at rest, revocation | Phase 2 |
| T6 | CSRF | Per-session CSRF token on state-changing cookie-authenticated requests, Origin check | Phase 2 |
| T7 | IDOR/BOLA | Ownership check in the service layer on every workspace access; opaque IDs | Phase 3 |
| T8 | Path traversal / symlink escape in file manager | Canonical-path containment, reject symlinks that resolve outside root | Phase 6 |
| T9 | Command injection | No `shell=True`; argv lists only; schema-validated inputs | all phases |
| T10 | SQL injection | Parameterized queries only | all phases |
| T11 | XSS | Strict CSP, no inline script, output encoding in the UI | Phase 5 |
| T12 | WebSocket auth bypass / cross-site WebSocket hijack | Authenticate each connection, validate Origin, authorize owner/admin | Phase 7/8 |
| T13 | Secret leakage in logs/errors | Redaction filter, no secrets in URLs | Phase 1 |
| T14 | Insecure default config | Startup validation fails closed in production; no generated predictable secrets | Phase 1 |
| T15 | Unauthenticated admin surface on a public interface | Refuse to bind a non-loopback address unless auth is enforced and a secret is configured | Phase 1/2 |
| T16 | Resource exhaustion | Per-user quotas, request size limits, bounded buffers | Phases 3-7 |
| T17 | Termux/PRoot mistaken for a sandbox | UI and docs label it "no isolation"; no claim otherwise | Phase 3 |

## Residual risks (always listed)

- Anyone who can create Docker containers on the host can become root on it.
- PRoot and Termux-local mode provide no meaningful isolation.
- Disposable-workspace deletion is ordinary filesystem deletion, not cryptographic erasure.
- Kernel/runtime vulnerabilities can defeat container isolation.

## Incident-response checklist (starter)

1. Contain: stop the control plane or block ingress at the proxy/firewall.
2. Revoke: invalidate all sessions (`orbitnest admin revoke-sessions`, Phase 2), rotate the secret key.
3. Preserve: copy logs and the audit table before cleanup.
4. Assess: list workspaces and containers created in the window; check for privileged or unexpected options.
5. Recover: rebuild from known-good images, restore data from backup, force password resets.
6. Review: update this model and add a regression test.
