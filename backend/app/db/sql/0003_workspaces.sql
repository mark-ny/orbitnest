-- 0003 workspaces: workspaces, runtime instances, storage volumes, lifecycle events, custom templates.
-- Workspace rows are never hard-deleted: 'deleted' is a status, so history and audit stay intact.

CREATE TABLE workspaces (
    id                   TEXT PRIMARY KEY,
    owner_id             TEXT NOT NULL REFERENCES users (id),
    name                 TEXT NOT NULL COLLATE NOCASE,
    template_id          TEXT NOT NULL,
    runtime              TEXT NOT NULL CHECK (runtime IN ('docker', 'local', 'fake')),  -- 'fake' is for tests
    status               TEXT NOT NULL CHECK (status IN (
                             'provisioning', 'starting', 'running', 'idle', 'stopping',
                             'stopped', 'failed', 'deleting', 'deleted')),
    status_reason        TEXT,
    persistence          TEXT NOT NULL CHECK (persistence IN ('persistent', 'disposable')),
    destroy_on_stop      INTEGER NOT NULL DEFAULT 0 CHECK (destroy_on_stop IN (0, 1)),
    resource_profile     TEXT NOT NULL,
    cpu_millis           INTEGER NOT NULL,
    memory_mb            INTEGER NOT NULL,
    pids_limit           INTEGER NOT NULL,
    storage_quota_mb     INTEGER NOT NULL,
    idle_timeout_minutes INTEGER,                      -- NULL = never stop for idleness
    network_policy       TEXT NOT NULL CHECK (network_policy IN ('none', 'isolated-bridge')),
    labels               TEXT NOT NULL DEFAULT '{}',   -- JSON object of short string pairs
    idempotency_key      TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    started_at           TEXT,
    last_activity_at     TEXT,
    last_health_at       TEXT,
    last_health_ok       INTEGER CHECK (last_health_ok IN (0, 1)),
    deleted_at           TEXT
) STRICT;

-- Names are unique per owner among live workspaces only, so a deleted name can be reused.
CREATE UNIQUE INDEX idx_workspaces_owner_name ON workspaces (owner_id, name) WHERE status != 'deleted';
CREATE UNIQUE INDEX idx_workspaces_idempotency ON workspaces (owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_workspaces_owner_status ON workspaces (owner_id, status);
CREATE INDEX idx_workspaces_status ON workspaces (status);

-- One row per runtime resource (container, local process record). A reset creates a new instance.
CREATE TABLE runtime_instances (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    runtime      TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    image        TEXT,
    created_at   TEXT NOT NULL,
    removed_at   TEXT
) STRICT;

CREATE INDEX idx_runtime_instances_workspace ON runtime_instances (workspace_id, created_at);
CREATE UNIQUE INDEX idx_runtime_instances_resource ON runtime_instances (runtime, resource_id);

CREATE TABLE storage_volumes (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('bind')),
    host_path    TEXT NOT NULL,                       -- relative to <data_dir>/workspaces
    mount_path   TEXT NOT NULL,                       -- path inside the workspace
    persistence  TEXT NOT NULL CHECK (persistence IN ('persistent', 'disposable')),
    quota_mb     INTEGER NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('active', 'retained', 'purged')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    purged_at    TEXT
) STRICT;

CREATE TABLE workspace_events (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    at            TEXT NOT NULL,
    event         TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT,
    reason        TEXT,
    actor_user_id TEXT
) STRICT;

CREATE INDEX idx_workspace_events_workspace ON workspace_events (workspace_id, at, id);

-- Administrator-defined templates (validated at write time; see app/services/templates.py).
CREATE TABLE custom_templates (
    id         TEXT PRIMARY KEY,
    definition TEXT NOT NULL,                         -- validated Template JSON
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_by TEXT REFERENCES users (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
