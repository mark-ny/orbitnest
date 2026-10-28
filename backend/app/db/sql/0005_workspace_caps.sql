-- 0005 per-workspace extra Linux capabilities (security lab). JSON array of capability names.
-- Chosen at creation, after the lab rules are accepted; validated against an allowlist before use.
ALTER TABLE workspaces ADD COLUMN extra_caps TEXT NOT NULL DEFAULT '[]';
