"""Command line: ``python -m app <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def _auth_service():  # type: ignore[no-untyped-def]
    from app.auth.passwords import PasswordHasher
    from app.auth.service import AuthService
    from app.db import Database, migrate

    settings = _settings().with_resolved_secret()
    db = Database(settings.db_path)
    migrate(db)
    return AuthService(db, PasswordHasher(), settings), settings


def _read_password(args: argparse.Namespace, prompt: str = "Password: ") -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    import getpass

    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat password: ") != first:
        print("passwords do not match", file=sys.stderr)
        raise SystemExit(2)
    return first


def _run_auth(action):  # type: ignore[no-untyped-def]
    from app.core.errors import AppError

    try:
        return action()
    except AppError as exc:
        reasons = "; ".join(d["reason"] for d in (exc.details or []))
        print(f"error: {exc.message} {reasons}".strip(), file=sys.stderr)
        raise SystemExit(1) from None


def cmd_setup_token(_: argparse.Namespace) -> int:
    from app.auth import setup_token

    service, settings = _auth_service()
    if not service.setup_required():
        print("setup is already complete; no setup token is needed")
        return 0
    path = setup_token.ensure_setup_token(settings.data_dir)
    token = setup_token.read_setup_token(settings.data_dir)
    print(f"setup token file: {path}")
    print(f"setup token: {token}")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    service, _ = _auth_service()
    password = _read_password(args)
    user = _run_auth(lambda: service.create_admin_local(args.username, password))
    print(f"created administrator {user.username}")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    service, _ = _auth_service()
    password = _read_password(args, "New password: ")
    _run_auth(lambda: service.force_reset_password(args.username, password, reactivate=args.reactivate))
    print(f"password reset for {args.username}; all of their sessions were revoked")
    return 0


def cmd_revoke_sessions(_: argparse.Namespace) -> int:
    service, _settings_unused = _auth_service()
    print(f"revoked {service.revoke_all_sessions()} session(s)")
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


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Report (default) or remove orphaned runtime resources and purged data directories."""
    from app.main import create_app
    from app.services.jobs import InlineRunner

    app = create_app(_settings(), job_runner=InlineRunner())
    report = app.state.workspaces.cleanup(apply=args.apply)
    print(json.dumps(report.to_dict(), indent=2))
    if not args.apply and (report.orphans or report.leftover_data):
        print(
            "dry run: nothing was changed. Re-run with --apply to remove orphaned resources.", file=sys.stderr
        )
    return 0


def cmd_build_images(args: argparse.Namespace) -> int:
    import subprocess

    from app.services import images

    names = [n for n in [args.username, *args.more] if n] or None
    try:
        steps = images.plan(names)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not images.docker_dir().is_dir():
        print(f"error: {images.docker_dir()} not found (run from a source checkout)", file=sys.stderr)
        return 1
    for step in steps:
        print("+ " + " ".join(step), flush=True)
        if subprocess.run(step, check=False).returncode != 0:  # noqa: S603 - argv list from a fixed plan
            print("error: image build failed; see the output above", file=sys.stderr)
            return 1
    print("images built. Restart is not needed; reload the Templates page.")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from app.services.backup import BackupError, create_backup

    settings = _settings()
    try:
        result = create_backup(
            settings,
            Path(args.dest or settings.data_dir / "backups"),
            include_workspaces=args.include_workspaces,
        )
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"backup written: {result.path}")
    print(f"includes signing key: {result.includes_secret}")
    print(f"includes workspace data: {result.includes_workspaces}")
    print("This archive holds password hashes and the signing key. Store it like a secret.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from app.services.backup import BackupError, restore_backup

    if not args.archive:
        print("error: restore requires --archive PATH", file=sys.stderr)
        return 2
    try:
        names = restore_backup(_settings(), Path(args.archive))
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"restored: {', '.join(names)}")
    return 0


COMMANDS = {
    "build-images": cmd_build_images,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "cleanup": cmd_cleanup,
    "serve": cmd_serve,
    "migrate": cmd_migrate,
    "migration-status": cmd_migration_status,
    "capabilities": cmd_capabilities,
    "check-config": cmd_check_config,
    "setup-token": cmd_setup_token,
    "create-admin": cmd_create_admin,
    "reset-password": cmd_reset_password,
    "revoke-sessions": cmd_revoke_sessions,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orbitnest", description="OrbitNest Workspaces control plane")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("username", nargs="?", help="for create-admin / reset-password")
    parser.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    parser.add_argument("--reactivate", action="store_true", help="reset-password: also re-enable the user")
    parser.add_argument(
        "--apply", action="store_true", help="cleanup: actually remove (default is a dry run)"
    )
    parser.add_argument("more", nargs="*", help="build-images: further image names")
    parser.add_argument("--dest", help="backup: destination directory (default DATA_DIR/backups)")
    parser.add_argument(
        "--include-workspaces", action="store_true", help="backup: also archive workspace data"
    )
    parser.add_argument("--archive", help="restore: backup archive to restore")
    args = parser.parse_args(argv)
    if args.command != "build-images" and args.more:
        parser.error("unexpected extra arguments")
    if args.command in {"create-admin", "reset-password"} and not args.username:
        parser.error(f"{args.command} requires a username")
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
