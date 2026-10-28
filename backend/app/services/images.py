"""Build plan for the locally built workspace images (``docker/`` directory).

Only template ``build_context`` names from the built-in registry are ever accepted, so a caller
cannot make this build an arbitrary directory. The plan is a list of ``docker build`` argv lists.
"""

from __future__ import annotations

from pathlib import Path

from app.services.templates import BUILTIN_TEMPLATES

BASE_TAGS = {
    "debian": "orbitnest/stream-base:debian-1",
    "ubuntu": "orbitnest/stream-base:ubuntu-1",
    "kali": "orbitnest/stream-base:kali-1",
}
BASE_IMAGES = {"debian": "debian:bookworm-slim", "ubuntu": "ubuntu:24.04", "kali": "kalilinux/kali-rolling"}
# Which base each template image is built on.
NEEDS_BASE = {"stream-desktop-ubuntu": "ubuntu", "stream-kali": "kali"}
# Images that do not use a stream base (plain build contexts).
NO_BASE = frozenset({"security-lab", "tor-gateway"})


def docker_dir() -> Path:
    # backend/app/services/images.py -> repository root / docker
    return Path(__file__).resolve().parents[3] / "docker"


HELPER_IMAGES = {"tor-gateway": "orbitnest/tor-gateway:1"}  # not templates, but built the same way


def buildable() -> dict[str, str]:
    """build_context -> image tag, for every built-in template that is built locally, plus helpers."""
    out = {t.build_context: t.image for t in BUILTIN_TEMPLATES if t.build_context}
    out.update(HELPER_IMAGES)
    return out


def plan(selected: list[str] | None, root: Path | None = None, docker_bin: str = "docker") -> list[list[str]]:
    root = root or docker_dir()
    known = buildable()
    names = selected or list(known)
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"unknown image: {unknown[0]} (choose from: {', '.join(sorted(known))})")
    bases = sorted({NEEDS_BASE.get(n, "debian") for n in names if n not in NO_BASE})
    steps: list[list[str]] = []
    for base in bases:
        steps.append(
            [
                docker_bin, "build",
                "--build-arg", f"BASE_IMAGE={BASE_IMAGES[base]}",
                "--tag", BASE_TAGS[base],
                str(root / "stream-base"),
            ]
        )  # fmt: skip
    for name in names:
        steps.append([docker_bin, "build", "--tag", known[name], str(root / name)])
    return steps
