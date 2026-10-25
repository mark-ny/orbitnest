"""Resource profiles. Users pick a named profile; they never supply raw numbers.

This keeps every limit inside a reviewed ceiling and makes validation trivial.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PROFILE_ORDER: Final[tuple[str, ...]] = ("small", "medium", "large")


@dataclass(frozen=True)
class Resources:
    cpu_millis: int  # 1000 = one CPU
    memory_mb: int
    pids: int
    storage_mb: int


PROFILES: Final[dict[str, Resources]] = {
    "small": Resources(cpu_millis=500, memory_mb=512, pids=256, storage_mb=2048),
    "medium": Resources(cpu_millis=1000, memory_mb=1024, pids=512, storage_mb=5120),
    "large": Resources(cpu_millis=2000, memory_mb=2048, pids=1024, storage_mb=10240),
}

# Absolute ceilings no profile may exceed (guards future edits to PROFILES).
HARD_CAPS: Final[Resources] = Resources(cpu_millis=4000, memory_mb=8192, pids=4096, storage_mb=102400)


class ProfileError(ValueError):
    pass


def profile_rank(name: str) -> int:
    try:
        return PROFILE_ORDER.index(name)
    except ValueError:
        raise ProfileError(f"unknown resource profile {name!r}") from None


def resolve_profile(name: str, *, max_profile: str) -> Resources:
    """Return the limits for ``name`` provided it exists and does not exceed ``max_profile``."""
    if name not in PROFILES:
        raise ProfileError(f"unknown resource profile {name!r}")
    if profile_rank(name) > profile_rank(max_profile):
        raise ProfileError(f"profile {name!r} exceeds this template's maximum ({max_profile!r})")
    res = PROFILES[name]
    if (
        res.cpu_millis > HARD_CAPS.cpu_millis
        or res.memory_mb > HARD_CAPS.memory_mb
        or res.pids > HARD_CAPS.pids
        or res.storage_mb > HARD_CAPS.storage_mb
    ):
        raise ProfileError("profile exceeds hard limits")  # pragma: no cover - guards future edits
    return res
