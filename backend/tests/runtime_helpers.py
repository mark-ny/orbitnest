from __future__ import annotations

from app.runtimes.base import RuntimeSpec

WSID = "01JABCDEFGHJKMNPQRSTVWXYZ0"[:26]


def make_spec(data_dir: str | None, **kw) -> RuntimeSpec:
    base = dict(
        workspace_id=WSID,
        image="python:3.12-slim",
        command=("sleep", "infinity"),
        cpu_millis=500,
        memory_mb=512,
        pids=256,
        network="isolated-bridge",
        mount_path="/home/workspace",
        data_dir=data_dir,
        labels={"orbitnest.managed": "true", "orbitnest.workspace": WSID},
    )
    base.update(kw)
    return RuntimeSpec(**base)
