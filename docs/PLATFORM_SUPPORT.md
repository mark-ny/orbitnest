# Platform support matrix

This matrix is the human-readable twin of the machine-readable matrix in
`backend/app/core/matrix.py` (added in Phase 3). A test asserts the two agree.

Legend:

- **Full**: fully supported with container isolation.
- **Limited**: works, with stated limitations (see notes).
- **Remote**: needs a remote Docker-capable Linux host; the local device is only a client.
- **None**: unsupported.

## Platform modes

| Mode | Control plane | Workspace runtime | Isolation |
|---|---|---|---|
| Linux + Docker Engine | native | Docker Engine | Container (namespaces, cgroups, dropped caps, seccomp) |
| Windows + Docker Desktop (WSL 2) | native Python venv | Docker Desktop Linux containers | Container, inside the WSL 2 VM |
| Windows without Docker | native Python venv | none | None; control plane only |
| Termux (Android, no root) | native, bound to 127.0.0.1 | local folders and local processes; optional proot-distro | **None / weak.** Not a security boundary |
| Termux client to remote host | browser/CLI on the device | runs on the remote Linux host | Container (on the remote host) |

Docker Engine does not run in ordinary, unrooted Termux. OrbitNest never assumes it does.

## Profile x mode

| Profile | Linux + Docker | Windows + Docker Desktop | Termux local | Termux to remote host |
|---|---|---|---|---|
| Browser: Chromium | Full | Full | Remote | Full |
| Browser: Firefox | Full | Full | Remote | Full |
| Linux Desktop: Ubuntu | Full | Full | Remote | Full |
| Linux Desktop: Kali | Limited (image availability, authorized use only) | Limited (same, plus arch) | Remote | Limited |
| Terminal: Ubuntu | Full | Full | Limited (proot-distro, if installed; no isolation) | Full |
| Developer: Python | Full | Full | Limited (local venv/process, no isolation) | Full |
| Developer: Node.js | Full | Full | Limited (local process, no isolation) | Full |
| Developer: Web | Full | Full | Limited (local static server, no isolation) | Full |
| Security Lab | Full (non-privileged, no host networking) | Limited | Remote (refused locally) | Full |
| Custom Template | Full (admin-defined, allowlisted) | Full | Remote | Full |

Notes:

- "Full" for streamed desktops/browsers additionally depends on the Phase 8 gateway. Until that
  phase is complete and verified, `BUILD_STATUS.md` records streaming as **not implemented**
  and the dashboard shows it as unavailable.
- Architecture matters: images are assumed `amd64`. On `arm64` hosts (Apple silicon under
  Docker Desktop, Android devices, ARM VPS) availability is per image and is reported by the
  capability endpoint rather than assumed.
- Windows 10/11 requires WSL 2 and Docker Desktop for any containerized workspace.

## What the capability report checks

OS family and version, CPU architecture, Termux/Android detection, WSL detection, container CLI
presence, daemon reachability, Docker Compose presence, proot-distro presence, data-directory
writability and free space, and Python version. Unsupported options are disabled with a reason
and remediation text instead of failing later.
