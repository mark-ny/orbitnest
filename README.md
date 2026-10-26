# OrbitNest Workspaces

*Your environments. One secure workspace.*

A self-hosted workspace platform: a web control plane that creates, runs and manages isolated
environments (browsers, desktops, terminals, dev environments) on a container runtime.

> **Status: 0.1.0 pre-release, not verified on Termux, Windows, WSL or against a real Docker daemon.** See [`BUILD_STATUS.md`](BUILD_STATUS.md) for exactly what exists, what
> has been tested, and what has not. Nothing here is a finished product yet.
> The name "OrbitNest" has not been checked for trademark or domain availability.
> OrbitNest is independent and not affiliated with Kasm Technologies.

## Quick start (development, loopback only)

Requires Python 3.11+ (tested on 3.13) with `starlette`, `pydantic`, `uvicorn`, `python-multipart`.

```bash
cd backend
python3 -m app check-config        # validate configuration
python3 -m app migrate             # create/upgrade the SQLite database
python3 -m app capabilities        # what this machine can honestly support
python3 -m app serve               # http://127.0.0.1:8765/api/v1/health
```

Run the tests:

```bash
cd backend
python3 -m unittest discover -s tests -t .
ruff check . && ruff format --check .
```

## Documentation

- [Installation (Linux, Windows + WSL 2, Termux, VPS)](docs/INSTALL.md)
- [Architecture and decision records](docs/ARCHITECTURE.md)
- [Platform support matrix](docs/PLATFORM_SUPPORT.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Security policy](SECURITY.md)
