"""Administrator-facing "System" report: what works, what is missing, and what to do about it.

Everything here is read-only. Each problem is reported as a plain sentence with the action that fixes
it, because the questions this answers ("why can't I create a workspace?") are usually asked by
someone who does not know which of Docker, an image, a gateway or a port is at fault.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from app import __version__
from app.runtimes.base import RuntimeFailure
from app.services import images

BACKUP_STALE_DAYS = 7
_UPDATE_TTL_SECONDS = 1800
_update_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _git(repo: Path, *args: str, timeout: int = 15) -> str | None:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "GIT_SSL_CAINFO", "HTTPS_PROXY")}
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def update_status(
    repo: Path | None = None, *, now: float | None = None, force: bool = False
) -> dict[str, Any]:
    """Whether GitHub has commits this checkout lacks. Cached; the network is touched at most every 30 min."""
    repo = repo or images.docker_dir().parent
    t = time.time() if now is None else now
    cached = _update_cache["value"]
    if cached is not None and not force and t - _update_cache["at"] < _UPDATE_TTL_SECONDS:
        return dict(cached)
    result: dict[str, Any] = {"checked": False, "behind": 0, "current": None}
    if (repo / ".git").exists():
        result["current"] = _git(repo, "rev-parse", "--short", "HEAD")
        if _git(repo, "fetch", "--quiet") is not None:
            behind = _git(repo, "rev-list", "--count", "HEAD..@{u}")
            if behind is not None and behind.isdigit():
                result.update(checked=True, behind=int(behind))
    _update_cache.update(at=t, value=dict(result))
    return result


def backup_status(data_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    folder = data_dir / "backups"
    newest = 0.0
    if folder.is_dir():
        for item in folder.iterdir():
            if item.is_file():
                newest = max(newest, item.stat().st_mtime)
    if not newest:
        return {"last_backup_days": None}
    return {"last_backup_days": round(((time.time() if now is None else now) - newest) / 86400, 1)}


def build_report(app: Any, *, check_updates: bool = True) -> dict[str, Any]:
    svc = app.state.workspaces
    caps = app.state.detect_capabilities()
    problems: list[dict[str, str]] = []

    def problem(level: str, what: str, fix: str) -> None:
        problems.append({"level": level, "what": what, "fix": fix})

    docker = next((r for r in caps.runtimes if r.name == "docker"), None)
    adapter = svc.adapters().get("docker")
    runtime = {
        "mode": caps.mode,
        "docker_ok": bool(docker and docker.available),
        "detail": docker.detail if docker else "",
    }
    if caps.mode in ("linux-docker", "windows-docker-desktop") and not runtime["docker_ok"]:
        problem(
            "error",
            "Docker is not reachable.",
            (docker.remediation if docker else None) or "Start Docker and reload.",
        )

    image_rows: list[dict[str, Any]] = []
    gateway_ok: bool | None = None
    if adapter is not None:
        for context, tag in sorted(images.buildable().items()):
            try:
                present = adapter.has_image(tag)
            except RuntimeFailure:
                present = None
            image_rows.append({"name": context, "image": tag, "present": present})
            if context == "tor-gateway":
                gateway_ok = present
        if gateway_ok is False:
            problem(
                "warning",
                "The Tor gateway image is not installed, so workspaces set to Tor cannot start.",
                "Admin, Templates: Install / rebuild Tor gateway image.",
            )

    gateways = {"running": 0, "orphaned": 0}
    if adapter is not None:
        with contextlib.suppress(RuntimeFailure):
            gateways["running"] = len(adapter.list_gateways())
        gateways["orphaned"] = sum(1 for o in svc.find_orphans() if "gateway" in o.get("reason", ""))
        if gateways["orphaned"]:
            problem(
                "warning",
                f"{gateways['orphaned']} leftover Tor gateway container(s) from deleted workspaces.",
                "Run: orbitnest cleanup --apply",
            )

    backup = backup_status(app.state.settings.data_dir)
    days = backup["last_backup_days"]
    if days is None:
        problem(
            "warning", "No backup exists yet.", "Run: orbitnest backup (store the file outside this machine)."
        )
    elif days > BACKUP_STALE_DAYS:
        problem("info", f"The last backup is {days:g} days old.", "Run: orbitnest backup")

    update = update_status() if check_updates else {"checked": False, "behind": 0, "current": None}
    if update["behind"]:
        problem(
            "info",
            f"{update['behind']} update(s) available on GitHub.",
            "Run: orbitnest update (it restarts the server for you).",
        )

    return {
        "version": __version__,
        "runtime": runtime,
        "images": image_rows,
        "gateways": gateways,
        "backup": backup,
        "update": update,
        "problems": problems,
        "ok": not any(p["level"] == "error" for p in problems),
    }
