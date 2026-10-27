# Installing OrbitNest Workspaces

> None of the installers below could be run on their target platforms in the development sandbox
> (no Termux, Windows, WSL, Docker or package index there). They were syntax-checked and their
> logic reviewed; treat the first run on each platform as a test and report problems.

Requirements everywhere: Python 3.11+ and access to the Python package index.

## Linux (with Docker for isolated workspaces)

```bash
git clone <repo> orbitnest && cd orbitnest
scripts/install-linux.sh
set -a; . ~/.config/orbitnest/orbitnest.env; set +a
cd backend && ~/.local/share/orbitnest/venv/bin/python -m app create-admin <username>
~/.local/share/orbitnest/venv/bin/python -m app serve      # http://127.0.0.1:8765
```

- Install Docker Engine yourself and add your user to the `docker` group. **Anyone who can use the
  Docker socket is root on that host**; OrbitNest's control plane has that power.
- Without a reachable Docker daemon the control plane still works, but workspaces are disabled and
  the dashboard says why (`python -m app capabilities` shows the same report).
- Run as a service: `deploy/systemd/orbitnest.service` (header explains the user-service setup).

## Windows 10/11 (Docker Desktop + WSL 2)

1. Install WSL 2 (`wsl --install -d Ubuntu`, elevated PowerShell, reboot) and Docker Desktop with
   "Use the WSL 2 based engine" plus WSL integration for your distribution.
2. In PowerShell from the repository: `.\scripts\install-windows.ps1`.
3. Follow the printed commands inside WSL, then open http://127.0.0.1:8765 in your Windows browser.

The control plane runs inside WSL, not native Windows Python: the file manager needs POSIX
semantics and the capability report marks it unsupported on native Windows. Windows without Docker
Desktop is control-plane only.

## Termux (Android, no root)

```bash
pkg install git
git clone <repo> orbitnest && cd orbitnest
bash scripts/install-termux.sh
```

**Termux has no container isolation.** Docker Engine does not run in ordinary Termux. Workspaces
are folders plus processes running as your Termux user, and only an administrator may open a terminal.
Do not run untrusted code there. For isolation, host OrbitNest on a remote Linux machine and use
the phone's browser as the client.

Troubleshooting:
- `pydantic-core` is a Rust extension; if pip has no wheel it compiles it (many minutes, hundreds of MB).
  The installer installs `rust` and `clang` for that reason. If the build fails, free storage, set
  `ANDROID_API_LEVEL`, and retry.
- Android may kill background processes. Run `termux-wake-lock`, and exempt Termux from battery optimization.
- The server binds loopback only; do not expose it from the phone.

## Remote Linux host / VPS (recommended for real isolation)

1. Install on the VPS with `scripts/install-linux.sh`; keep `ORBITNEST_HOST=127.0.0.1`.
2. Put TLS in front: `deploy/caddy/Caddyfile` (automatic certificates) or `deploy/nginx/orbitnest.conf`.
3. Set `ORBITNEST_PUBLIC_URL=https://your.host` and `ORBITNEST_TRUSTED_PROXY=true` in the env file.
4. Firewall: allow only 80/443 (and SSH). Block workspace containers from the metadata service
   (169.254.169.254) and from the control-plane port; OrbitNest does not configure host firewalls.
5. Reach it from any device (including Termux) with a browser.

Do not expose the plain-HTTP port. A non-loopback bind is refused unless
`ORBITNEST_ALLOW_NON_LOOPBACK=true` and an environment secret are set.

## Docker Compose (not recommended unless you accept the socket mount)

See the warning at the top of `deploy/compose/docker-compose.yml`. It is untested.

## Building browser and desktop images

Streaming templates (Chromium, Firefox, Tor Browser, Ubuntu desktop) need locally built images:

    python -m app build-images            # all, or name some: python -m app build-images stream-chromium

Your user must be able to run `docker` (Linux: `sudo usermod -aG docker $USER`, then log in again). Reload the Templates page afterwards; no restart is needed. Expect 1-2 GB RAM per workspace. Lost authenticator: `python -m app disable-mfa <username>`.

## Operations

```bash
python -m app backup [--dest DIR] [--include-workspaces]   # consistent snapshot, safe while running
python -m app restore --archive FILE                        # into an empty data dir only
python -m app cleanup [--apply]                             # dry run by default
python -m app revoke-sessions
python -m app reset-password <username>
python -m app migrate                                       # run after upgrading; backs nothing up for you
```

Upgrade: stop the service, `python -m app backup`, `git pull`, re-run the installer, `python -m app migrate`,
start the service. Backup archives contain the signing key and password hashes: store them privately.
