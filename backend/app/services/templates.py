"""Template registry: built-in profiles plus administrator-defined custom templates.

Templates are the only source of images, commands and runtime options. Users select a template id
and a resource profile; they never supply an image, command, mount, port or capability. Custom
templates are admin-only and validated against the same rules as built-ins.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core import matrix
from app.core.ids import to_iso, utcnow
from app.core.platform import Capabilities
from app.db import Database
from app.services.resources import PROFILE_ORDER

Category = Literal["browser", "desktop", "terminal", "developer", "security", "custom"]
Access = Literal["terminal", "stream", "none"]
NetworkPolicy = Literal["none", "isolated-bridge"]

TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}\Z")
# Registry/name[:tag][@sha256:digest]. Must not start with '-' (would be parsed as a CLI flag).
IMAGE_RE = re.compile(r"^[a-z0-9][A-Za-z0-9._/:@-]{0,199}\Z")
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}\Z")
# Linux capabilities a container may ever be given. Deliberately NOT included: SYS_ADMIN, SYS_MODULE,
# SYS_RAWIO, DAC_OVERRIDE, privileged mode, host networking, device passthrough and the Docker socket:
# any of those would let a workload reach the host, which defeats the point of a container.
ALLOWED_CAPABILITIES = frozenset({"NET_BIND_SERVICE", "NET_RAW", "NET_ADMIN", "SYS_PTRACE"})
# What custom (admin-defined) templates may request. The lab capabilities are built-in only.
CUSTOM_ALLOWED_CAPABILITIES = frozenset({"NET_BIND_SERVICE"})
CAPABILITY_HELP = {
    "NET_RAW": "Raw sockets: ping, nmap SYN/ICMP scans, packet capture (tcpdump, wireshark).",
    "NET_ADMIN": "Network setup inside the lab's own network namespace (interfaces, routes, iptables).",
    "SYS_PTRACE": "Attach debuggers and tracers (gdb, strace, ltrace) to processes in the lab.",
}
LAB_RULES_VERSION = "2026-10-28"
LAB_RULES: tuple[str, ...] = (
    "I will only test systems that I own, or that I have clear, written permission to test.",
    "I will not attack, scan or disrupt third parties, shared infrastructure or other OrbitNest users.",
    "I will not use this lab to access, damage or take other people's data, and I will follow the "
    "laws that apply to me.",
    "I understand that this lab's creation, the capabilities I choose and my agreement to these "
    "rules are logged.",
    "I understand an administrator may stop or remove lab workspaces that break these rules, and "
    "that I am responsible for what I do with them.",
)
MAX_ARGV = 16
MAX_ARG_LEN = 200


class TemplateError(ValueError):
    pass


def _check_argv(value: tuple[str, ...], field: str) -> tuple[str, ...]:
    if len(value) > MAX_ARGV:
        raise ValueError(f"{field} has too many arguments")
    for arg in value:
        if not arg or len(arg) > MAX_ARG_LEN or "\x00" in arg or "\n" in arg:
            raise ValueError(f"{field} contains an invalid argument")
    if value and value[0].startswith("-"):
        raise ValueError(f"{field} must start with an executable, not an option")
    return value


class Template(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=500)
    category: Category
    image: str
    image_digest: str | None = None
    command: tuple[str, ...] = ()
    healthcheck: tuple[str, ...] | None = None  # None: "container running" counts as healthy
    access: Access = "terminal"
    default_profile: str = "small"
    max_profile: str = "large"
    default_persistence: Literal["persistent", "disposable"] = "persistent"
    default_idle_minutes: int | None = Field(default=60, ge=1, le=60 * 24 * 14)
    network_policy: NetworkPolicy = "isolated-bridge"
    allowed_networks: tuple[NetworkPolicy, ...] = ("isolated-bridge", "none")
    published_ports: tuple[int, ...] = ()
    # Streamed templates: the VNC port INSIDE the container. It is never published to the host; the
    # control plane bridges to it with `docker exec` (see app/services/streams.py).
    stream_port: int | None = Field(default=None, ge=1, le=65535)
    shm_mb: int = Field(default=64, ge=64, le=2048)
    # Directory under docker/ that builds this template's image (locally built images only).
    build_context: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    capabilities: tuple[str, ...] = ()
    # Extra capabilities a user may switch on per workspace, only after accepting the lab rules.
    optional_capabilities: tuple[str, ...] = ()
    # Run as root INSIDE the (still unprivileged, capability-limited) container. Lab templates only:
    # raw-socket tools need root, and file capabilities do not work under no-new-privileges.
    run_as_root: bool = False
    # "direct": traffic leaves from the host's real IP/ISP. "tor": routed through Tor. "n/a": no browsing.
    privacy: Literal["direct", "tor", "n/a"] = "n/a"
    # Profile at which this template runs comfortably; the UI warns below it.
    recommended_profile: str | None = None
    # Smallest profile that works at all (browsers need more than 512 MB); smaller ones are refused.
    min_profile: str | None = None
    data_mount: str = "/home/workspace"
    local_supported: bool = False
    required_local_tools: tuple[str, ...] = ()
    local_command: tuple[str, ...] = ()
    authorized_use_only: bool = False
    deprecated: bool = False
    deprecation_note: str | None = None
    update_policy: Literal["manual", "pinned"] = "manual"
    enabled: bool = True
    disabled_reason: str | None = None
    notes: str | None = Field(default=None, max_length=500)
    builtin: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not TEMPLATE_ID_RE.match(v):
            raise ValueError("id must be 2-48 chars of a-z, 0-9 and '-', starting with a letter or digit")
        return v

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        if not IMAGE_RE.match(v) or ".." in v or "//" in v:
            raise ValueError("image reference is not valid")
        return v

    @field_validator("image_digest")
    @classmethod
    def _digest(cls, v: str | None) -> str | None:
        if v is not None and not DIGEST_RE.match(v):
            raise ValueError("image_digest must look like sha256:<64 hex>")
        return v

    @field_validator("command", "local_command")
    @classmethod
    def _argv(cls, v: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _check_argv(v, info.field_name)

    @field_validator("healthcheck")
    @classmethod
    def _hc(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if v is None else _check_argv(v, "healthcheck")

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [c for c in v if c not in ALLOWED_CAPABILITIES]
        if bad:
            raise ValueError(f"capability not allowed: {bad[0]}")
        return v

    @field_validator("optional_capabilities")
    @classmethod
    def _optcaps(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [c for c in v if c not in ALLOWED_CAPABILITIES]
        if bad:
            raise ValueError(f"capability not allowed: {bad[0]}")
        return v

    @field_validator("published_ports")
    @classmethod
    def _ports(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if v:
            raise ValueError("publishing ports is not supported; access goes through the gateway")
        return v

    @field_validator("data_mount")
    @classmethod
    def _mount(cls, v: str) -> str:
        if not v.startswith("/") or v == "/" or ".." in v.split("/") or "\x00" in v or ":" in v or "," in v:
            raise ValueError("data_mount must be an absolute path below /")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Template:
        for name in (self.default_profile, self.max_profile):
            if name not in PROFILE_ORDER:
                raise ValueError(f"unknown resource profile {name!r}")
        if PROFILE_ORDER.index(self.default_profile) > PROFILE_ORDER.index(self.max_profile):
            raise ValueError("default_profile exceeds max_profile")
        if self.network_policy not in self.allowed_networks:
            raise ValueError("network_policy must be one of allowed_networks")
        if not self.enabled and not self.disabled_reason:
            raise ValueError("a disabled template needs a disabled_reason")
        if self.deprecated and not self.deprecation_note:
            raise ValueError("a deprecated template needs a deprecation_note")
        if self.local_command and not self.local_supported:
            raise ValueError("local_command requires local_supported")
        if self.run_as_root and not self.authorized_use_only:
            raise ValueError("run_as_root requires authorized_use_only")
        if self.optional_capabilities and not self.authorized_use_only:
            raise ValueError("optional capabilities require authorized_use_only")
        for pname in (self.recommended_profile, self.min_profile):
            if pname is not None and pname not in PROFILE_ORDER:
                raise ValueError("unknown profile name")
        if self.min_profile and PROFILE_ORDER.index(self.min_profile) > PROFILE_ORDER.index(self.max_profile):
            raise ValueError("min_profile exceeds max_profile")
        if self.access == "stream" and self.stream_port is None and self.enabled:
            raise ValueError("a streamed template needs a stream_port")
        if self.access != "stream" and self.stream_port is not None:
            raise ValueError("stream_port is only valid for streamed templates")
        return self

    @property
    def matrix_key(self) -> str:
        return self.id if self.builtin else "custom"

    @property
    def platform_support(self) -> dict[str, str]:
        return {m: matrix.matrix_state(self.matrix_key, m) for m in matrix.MODES}


_IDLE = ("sleep", "infinity")


def _builtin(**kw: Any) -> Template:
    return Template(builtin=True, **kw)


BUILTIN_TEMPLATES: tuple[Template, ...] = (
    _builtin(
        id="browser-chromium",
        name="Browser: Chromium",
        description="Isolated Chromium browser session streamed to your browser.",
        category="browser",
        image="orbitnest/stream-chromium:1",
        build_context="stream-chromium",
        access="stream",
        stream_port=5900,
        shm_mb=512,
        default_profile="large",
        recommended_profile="large",
        min_profile="medium",
        privacy="direct",
        default_persistence="disposable",
        default_idle_minutes=30,
        notes="Built locally from docker/stream-chromium (python -m app build-images).",
    ),
    _builtin(
        id="browser-firefox",
        name="Browser: Firefox",
        description="Isolated Firefox browser session streamed to your browser.",
        category="browser",
        image="orbitnest/stream-firefox:1",
        build_context="stream-firefox",
        access="stream",
        stream_port=5900,
        shm_mb=512,
        default_profile="large",
        recommended_profile="large",
        min_profile="medium",
        privacy="direct",
        default_persistence="disposable",
        default_idle_minutes=30,
        notes="Built locally from docker/stream-firefox (python -m app build-images).",
    ),
    _builtin(
        id="browser-tor",
        name="Browser: Tor Browser",
        description=(
            "Official Tor Browser in an isolated container, streamed to your browser. Its web traffic "
            "goes through the Tor network, so sites see a Tor exit instead of your IP address."
        ),
        category="browser",
        image="orbitnest/stream-tor:1",
        build_context="stream-tor",
        access="stream",
        stream_port=5900,
        shm_mb=512,
        default_profile="large",
        recommended_profile="large",
        min_profile="medium",
        privacy="tor",
        default_persistence="disposable",
        default_idle_minutes=30,
        notes=(
            "Disposable by default: everything is erased when the workspace stops. This is Tor Browser in "
            "a container, NOT Tails: only the browser's traffic uses Tor, your ISP can see that you use "
            "Tor (use bridges in Tor's connection settings to hide that), and accounts you log in to "
            "identify you."
        ),
    ),
    _builtin(
        id="desktop-ubuntu",
        name="Linux Desktop: Ubuntu",
        description="Ubuntu desktop (Xfce) session streamed to your browser.",
        category="desktop",
        image="orbitnest/stream-desktop-ubuntu:1",
        build_context="stream-desktop-ubuntu",
        access="stream",
        stream_port=5900,
        shm_mb=512,
        default_profile="large",
        recommended_profile="large",
        min_profile="medium",
        privacy="direct",
        default_idle_minutes=60,
        notes="Built locally from docker/stream-desktop-ubuntu (python -m app build-images).",
    ),
    _builtin(
        id="desktop-kali",
        name="Security Lab Desktop: Kali",
        description=(
            "Kali Linux desktop with the common security tools, streamed to your browser. "
            "For AUTHORIZED testing only; you must accept the lab rules."
        ),
        category="desktop",
        image="orbitnest/stream-kali:1",
        build_context="stream-kali",
        access="stream",
        stream_port=5900,
        shm_mb=512,
        default_profile="large",
        recommended_profile="large",
        min_profile="medium",
        privacy="direct",
        authorized_use_only=True,
        optional_capabilities=("NET_RAW", "NET_ADMIN", "SYS_PTRACE"),
        run_as_root=True,
        notes=(
            "Built locally (several GB). Rolling base image, so rebuilds differ. Runs unprivileged, on an "
            "isolated network, with only the capabilities you tick."
        ),
    ),
    _builtin(
        id="terminal-ubuntu",
        name="Terminal: Ubuntu",
        description="Ubuntu command-line environment.",
        category="terminal",
        image="ubuntu:24.04",
        command=_IDLE,
        default_idle_minutes=120,
    ),
    _builtin(
        id="dev-python",
        name="Developer: Python",
        description="Python 3.12 with a persistent project directory.",
        category="developer",
        image="python:3.12-slim",
        command=_IDLE,
        default_idle_minutes=120,
        local_supported=True,
        required_local_tools=("python3",),
    ),
    _builtin(
        id="dev-node",
        name="Developer: Node.js",
        description="Node.js 22 with a persistent project directory.",
        category="developer",
        image="node:22-slim",
        command=_IDLE,
        default_idle_minutes=120,
        local_supported=True,
        required_local_tools=("node",),
    ),
    _builtin(
        id="dev-web",
        name="Developer: Web",
        description="HTML/CSS/JavaScript development with Node.js tooling.",
        category="developer",
        image="node:22-slim",
        command=_IDLE,
        default_idle_minutes=120,
        local_supported=True,
        required_local_tools=("node",),
    ),
    _builtin(
        id="security-lab",
        name="Security Lab",
        description=(
            "Kali Linux command-line lab with the common security tools, for AUTHORIZED testing only. "
            "You must accept the lab rules, and may switch on extra Linux capabilities."
        ),
        category="security",
        image="orbitnest/security-lab:1",
        build_context="security-lab",
        command=_IDLE,
        default_profile="medium",
        recommended_profile="medium",
        privacy="direct",
        default_idle_minutes=60,
        authorized_use_only=True,
        optional_capabilities=("NET_RAW", "NET_ADMIN", "SYS_PTRACE"),
        run_as_root=True,
        notes=(
            "Built locally (several GB). Without extra capabilities, raw-socket tools (SYN scans, ping, "
            "packet capture) will not work. It never runs privileged or on the host network."
        ),
    ),
)

BUILTIN_BY_ID: dict[str, Template] = {t.id: t for t in BUILTIN_TEMPLATES}


def validate_custom_definition(payload: dict[str, Any]) -> Template:
    """Validate an administrator-supplied definition. Raises TemplateError with safe messages."""
    data = dict(payload)
    data.pop("builtin", None)
    template_id = str(data.get("id", ""))
    if not template_id.startswith("custom-"):
        raise TemplateError("custom template ids must start with 'custom-'")
    if template_id in BUILTIN_BY_ID:
        raise TemplateError("id collides with a built-in template")
    data["category"] = "custom"
    # Local-process execution runs on the host without isolation; it is never offered for custom
    # templates, and arbitrary host commands are never accepted from definitions.
    data.setdefault("local_supported", False)
    if data.get("local_supported") or data.get("local_command"):
        raise TemplateError("custom templates cannot run as local processes")
    if set(data.get("capabilities") or ()) - CUSTOM_ALLOWED_CAPABILITIES:
        raise TemplateError("custom templates may only use the NET_BIND_SERVICE capability")
    if data.get("run_as_root"):
        raise TemplateError("only built-in lab templates may run as root inside the container")
    if data.get("optional_capabilities"):
        raise TemplateError("optional capabilities are only available on built-in lab templates")
    if data.get("privacy") == "tor":
        raise TemplateError("only the built-in Tor template can claim Tor routing")
    try:
        return Template.model_validate(data)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'template'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        raise TemplateError(reasons) from None


@dataclass(frozen=True)
class Availability:
    """Whether (and how) a template can be created on this host right now."""

    creatable: bool
    state: str  # matrix state for the current mode, or "unsupported"
    runtime: str | None
    reason: str | None = None
    remediation: str | None = None
    isolation: str = "none"  # "container" | "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "creatable": self.creatable,
            "state": self.state,
            "runtime": self.runtime,
            "isolation": self.isolation,
            "reason": self.reason,
            "remediation": self.remediation,
        }


_MODE_TO_COLUMN = {
    "linux-docker": ("linux-docker", "docker"),
    "windows-docker-desktop": ("windows-docker-desktop", "docker"),
    "termux-local": ("termux-local", "local"),
}


def evaluate(
    template: Template,
    caps: Capabilities,
    *,
    which: Callable[[str], str | None] = shutil.which,
    image_present: Callable[[Template], bool | None] | None = None,
) -> Availability:
    """Decide availability from the matrix, the detected capabilities, and unbuilt-feature blockers."""
    if not template.enabled:
        return Availability(False, "unsupported", None, template.disabled_reason)
    mapped = _MODE_TO_COLUMN.get(caps.mode)
    if mapped is None:
        feature = caps.features.get("container_workspaces")
        return Availability(
            False,
            "unsupported",
            None,
            feature.reason if feature else "No workspace runtime is available.",
            feature.remediation if feature else None,
        )
    column, runtime = mapped
    state = matrix.matrix_state(template.matrix_key, column)
    isolation = "container" if runtime == "docker" else "none"
    blocker = matrix.BLOCKED.get((template.matrix_key, column))
    if blocker:
        return Availability(False, state, runtime, matrix.BLOCKERS[blocker], isolation=isolation)
    if state in {"remote", "unsupported"}:
        return Availability(
            False,
            state,
            runtime,
            "This profile needs a remote Docker-capable Linux host; this device can only be the client."
            if state == "remote"
            else "Not supported on this platform.",
            "Open a remote OrbitNest host in your browser, or run workspaces on a Linux host.",
            isolation,
        )
    if (
        runtime == "docker"
        and template.build_context
        and image_present is not None
        and image_present(template) is False
    ):
        return Availability(
            False,
            state,
            runtime,
            "The container image for this template has not been built on this host yet.",
            "Run: python -m app build-images " + template.build_context,
            isolation,
        )
    if runtime == "local":
        if not template.local_supported:
            return Availability(
                False, state, runtime, "This profile cannot run as a local process.", isolation=isolation
            )
        missing = [tool for tool in template.required_local_tools if which(tool) is None]
        if missing:
            return Availability(
                False,
                state,
                runtime,
                f"Required tool not installed: {missing[0]}",
                f"In Termux run: pkg install {'python' if missing[0] == 'python3' else 'nodejs'}",
                isolation,
            )
    return Availability(True, state, runtime, isolation=isolation)


class TemplateRegistry:
    """Built-in templates plus the administrator's custom templates (stored in the database)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _custom_rows(self, conn: sqlite3.Connection) -> list[tuple[Template, bool]]:
        out: list[tuple[Template, bool]] = []
        for row in conn.execute("SELECT definition, enabled FROM custom_templates ORDER BY created_at, id"):
            tpl = Template.model_validate(json.loads(row["definition"]))
            out.append(
                (
                    tpl.model_copy(
                        update={
                            "enabled": bool(row["enabled"]),
                            "disabled_reason": None if row["enabled"] else "Disabled by an administrator.",
                        }
                    ),
                    bool(row["enabled"]),
                )
            )
        return out

    def list(self) -> list[Template]:
        with self.db.connect() as conn:
            return [*BUILTIN_TEMPLATES, *(t for t, _ in self._custom_rows(conn))]

    def get(self, template_id: str) -> Template | None:
        if template_id in BUILTIN_BY_ID:
            return BUILTIN_BY_ID[template_id]
        if not template_id.startswith("custom-"):
            return None
        with self.db.connect() as conn:
            for tpl, _ in self._custom_rows(conn):
                if tpl.id == template_id:
                    return tpl
        return None

    def create_custom(self, actor_id: str, payload: dict[str, Any]) -> Template:
        tpl = validate_custom_definition(payload)
        now = to_iso(utcnow())
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO custom_templates "
                    "(id, definition, enabled, created_by, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?, ?)",
                    (tpl.id, tpl.model_dump_json(), actor_id, now, now),
                )
        except sqlite3.IntegrityError:
            raise TemplateError("a template with that id already exists") from None
        return tpl

    def set_enabled(self, template_id: str, enabled: bool) -> Template | None:
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE custom_templates SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), to_iso(utcnow()), template_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(template_id)

    def delete_custom(self, template_id: str) -> bool:
        """Delete a custom template. Refused while any non-deleted workspace uses it."""
        with self.db.transaction() as conn:
            in_use = conn.execute(
                "SELECT COUNT(*) FROM workspaces WHERE template_id = ? AND status != 'deleted'",
                (template_id,),
            ).fetchone()[0]
            if in_use:
                raise TemplateError("template is in use by existing workspaces")
            return conn.execute("DELETE FROM custom_templates WHERE id = ?", (template_id,)).rowcount > 0
