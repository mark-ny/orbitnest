"""Command line: ``python -m app <command>``."""

from __future__ import annotations

import argparse
import json
import sys

from app.core.config import ConfigError, Settings


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_check_config(_: argparse.Namespace) -> int:
    settings = _settings()
    print(
        json.dumps(
            {
                "env": settings.env,
                "bind": f"{settings.host}:{settings.port}",
                "database": "configured",
                "secret": "from environment" if settings.secret_from_env else "managed locally",
            },
            indent=2,
        )
    )
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    from app.db import Database, migrate

    settings = _settings()
    applied = migrate(Database(settings.db_path))
    print(f"applied migrations: {applied}" if applied else "database is up to date")
    return 0


def cmd_migration_status(_: argparse.Namespace) -> int:
    from app.db import Database, migration_status

    status = migration_status(Database(_settings().db_path))
    print(json.dumps({"current": status.current, "latest": status.latest, "pending": status.pending}))
    return 0 if status.up_to_date else 1


def cmd_capabilities(_: argparse.Namespace) -> int:
    from app.core.platform import detect_capabilities

    print(detect_capabilities(_settings().data_dir).model_dump_json(indent=2))
    return 0


def cmd_serve(_: argparse.Namespace) -> int:
    import uvicorn

    from app.main import create_app

    settings = _settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        server_header=False,
        proxy_headers=settings.trusted_proxy,
        forwarded_allow_ips="127.0.0.1" if settings.trusted_proxy else None,
    )
    return 0


COMMANDS = {
    "serve": cmd_serve,
    "migrate": cmd_migrate,
    "migration-status": cmd_migration_status,
    "capabilities": cmd_capabilities,
    "check-config": cmd_check_config,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orbitnest", description="OrbitNest Workspaces control plane")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
