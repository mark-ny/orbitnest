-- 0006 per-workspace egress: 'direct' (the host's own connection) or 'tor' (forced through a Tor gateway).
ALTER TABLE workspaces ADD COLUMN egress TEXT NOT NULL DEFAULT 'direct';
