"""Machine-readable platform support matrix (docs/PLATFORM_SUPPORT.md embeds the rendered table).

States describe the *design target* of a (profile, mode) pair. Features that are not built yet are
listed separately in ``BLOCKERS`` so the table never over-promises: a pair can be "full" by design
and still not creatable today.
"""

from __future__ import annotations

from typing import Final

MODES: Final[tuple[str, ...]] = (
    "linux-docker",
    "windows-docker-desktop",
    "termux-local",
    "termux-remote-client",
)

MODE_LABELS: Final[dict[str, str]] = {
    "linux-docker": "Linux + Docker",
    "windows-docker-desktop": "Windows + Docker Desktop (WSL 2)",
    "termux-local": "Termux local",
    "termux-remote-client": "Termux to remote host",
}

STATE_LABELS: Final[dict[str, str]] = {
    "full": "Full",
    "limited": "Limited",
    "remote": "Remote host required",
    "unsupported": "Unsupported",
}

# (linux-docker, windows-docker-desktop, termux-local, termux-remote-client)
MATRIX: Final[dict[str, tuple[str, str, str, str]]] = {
    "browser-chromium": ("full", "full", "remote", "full"),
    "browser-firefox": ("full", "full", "remote", "full"),
    "browser-tor": ("full", "full", "remote", "full"),
    "desktop-ubuntu": ("full", "full", "remote", "full"),
    "desktop-kali": ("limited", "limited", "remote", "limited"),
    "terminal-ubuntu": ("full", "full", "limited", "full"),
    "dev-python": ("full", "full", "limited", "full"),
    "dev-node": ("full", "full", "limited", "full"),
    "dev-web": ("full", "full", "limited", "full"),
    "security-lab": ("full", "limited", "remote", "full"),
    "custom": ("full", "full", "remote", "full"),
}

MATRIX_NOTES: Final[dict[str, str]] = {
    "desktop-kali": "No verified maintained image is configured; disabled. Authorized security work only.",
    "browser-tor": "x86_64 only; needs outbound access. The build downloads and verifies Tor Browser.",
    "terminal-ubuntu": "Termux: would use proot-distro (no isolation).",
    "security-lab": "Authorized security work only. Windows: limited by WSL 2 networking.",
    "dev-python": "Termux: local folder and local toolchain, no isolation.",
    "dev-node": "Termux: local folder and local toolchain, no isolation.",
    "dev-web": "Termux: local folder and local toolchain, no isolation.",
}

# Capabilities that are designed but not implemented in this build. A pair affected by a blocker
# is not creatable regardless of its matrix state. Keep in step with BUILD_STATUS.md.
BLOCKERS: Final[dict[str, str]] = {
    "proot": "The proot-distro runtime adapter is not implemented yet.",
}

# Which blocker (if any) applies to a profile in which mode.
BLOCKED: Final[dict[tuple[str, str], str]] = {
    ("terminal-ubuntu", "termux-local"): "proot",
}


def matrix_state(profile_key: str, mode: str) -> str:
    row = MATRIX.get(profile_key)
    if row is None or mode not in MODES:
        return "unsupported"
    return row[MODES.index(mode)]


def render_markdown() -> str:
    header = "| Profile | " + " | ".join(MODE_LABELS[m] for m in MODES) + " | Notes |"
    rule = "|---|" + "---|" * (len(MODES) + 1)
    lines = [header, rule]
    for key, row in MATRIX.items():
        cells = []
        for mode, state in zip(MODES, row, strict=True):
            label = STATE_LABELS[state]
            blocker = BLOCKED.get((key, mode))
            if blocker:
                label += f" (blocked: {blocker})"
            cells.append(label)
        lines.append(f"| `{key}` | " + " | ".join(cells) + f" | {MATRIX_NOTES.get(key, '')} |")
    return "\n".join(lines)
