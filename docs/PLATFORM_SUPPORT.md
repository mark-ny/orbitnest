# Platform support matrix

The profile table below is generated from `backend/app/core/matrix.py`; a test asserts the two agree.

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

Generated from `backend/app/core/matrix.py` (a test fails if this table drifts from the code).
"Blocked" means the design target is that state but a required piece is not built yet, so the
dashboard disables creation and says why.

<!-- MATRIX:BEGIN -->
| Profile | Linux + Docker | Windows + Docker Desktop (WSL 2) | Termux local | Termux to remote host | Notes |
|---|---|---|---|---|---|
| `browser-chromium` | Full (blocked: streaming) | Full (blocked: streaming) | Remote host required | Full (blocked: streaming) |  |
| `browser-firefox` | Full (blocked: streaming) | Full (blocked: streaming) | Remote host required | Full (blocked: streaming) |  |
| `desktop-ubuntu` | Full (blocked: streaming) | Full (blocked: streaming) | Remote host required | Full (blocked: streaming) |  |
| `desktop-kali` | Limited (blocked: streaming) | Limited (blocked: streaming) | Remote host required | Limited (blocked: streaming) | No verified maintained image is configured; disabled. Authorized security work only. |
| `terminal-ubuntu` | Full | Full | Limited (blocked: proot) | Full | Termux: would use proot-distro (no isolation). |
| `dev-python` | Full | Full | Limited | Full | Termux: local folder and local toolchain, no isolation. |
| `dev-node` | Full | Full | Limited | Full | Termux: local folder and local toolchain, no isolation. |
| `dev-web` | Full | Full | Limited | Full | Termux: local folder and local toolchain, no isolation. |
| `security-lab` | Full | Limited | Remote host required | Full | Authorized security work only. Windows: limited by WSL 2 networking. |
| `custom` | Full | Full | Remote host required | Full |  |
<!-- MATRIX:END -->

Notes:

- Streamed desktops/browsers depend on the Phase 8 gateway. Until that phase is complete and
  verified, `BUILD_STATUS.md` records streaming as **not implemented** and those profiles cannot be created.
- Architecture matters: images are assumed `amd64`. On `arm64` hosts (Apple silicon under
  Docker Desktop, Android devices, ARM VPS) availability is per image and is reported by the
  capability endpoint rather than assumed.
- Windows 10/11 requires WSL 2 and Docker Desktop for any containerized workspace.

## What the capability report checks

OS family and version, CPU architecture, Termux/Android detection, WSL detection, container CLI
presence, daemon reachability, Docker Compose presence, proot-distro presence, data-directory
writability and free space, and Python version. Unsupported options are disabled with a reason
and remediation text instead of failing later.
