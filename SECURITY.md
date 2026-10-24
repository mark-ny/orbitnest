# Security policy

## Status

OrbitNest is pre-release. **Do not expose it to untrusted networks.** The current build refuses to
bind a non-loopback address.

## Supported versions

Only the latest commit on the default branch receives fixes until a first tagged release.

## Reporting a vulnerability

Report privately to the maintainer (contact address to be set before public release) with steps to
reproduce. Please do not open a public issue for security problems. Expect an acknowledgement
within a few days; this is a project-level goal, not a contractual commitment.

## Known limitations (kept current in `BUILD_STATUS.md`)

- Docker access is root-equivalent on the host. Anyone able to create containers can become root.
- Termux-local mode and PRoot userlands provide **no meaningful isolation**.
- Disposable-workspace deletion is ordinary filesystem deletion, not cryptographic erasure.
- Container isolation does not defend against kernel or runtime vulnerabilities.
- The Docker adapter has not been verified against a real daemon in the build environment.
- The product name "OrbitNest" has not been cleared for trademark or domain use.

## Secure deployment (summary; detail lands with the deployment docs)

1. Run behind a TLS reverse proxy; never publish workspace ports or the Docker API.
2. Set `ORBITNEST_SECRET_KEY` from a secrets store; use a unique 32+ character random value.
3. Run the control plane as an unprivileged user.
4. Keep backups; test restores.
5. Review `docs/THREAT_MODEL.md` and its incident-response checklist.
