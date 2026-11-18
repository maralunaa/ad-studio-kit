-- Knowledge base (K3): a playbook export uploaded in Admin → Knowledge, its section diff against the playbook in
-- force, and Claude's proposed updates to the knowledge files, rubric and prompts, each approved or rejected.
CREATE TABLE IF NOT EXISTS knowledge_updates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    revision       INTEGER,
    exported       TEXT,
    playbook_md    TEXT NOT NULL,
    diff_json      TEXT NOT NULL DEFAULT '[]',
    proposals_json TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'queued',   -- queued | running | ready | failed
    error          TEXT NOT NULL DEFAULT '',
    cost_usd       REAL NOT NULL DEFAULT 0
);
