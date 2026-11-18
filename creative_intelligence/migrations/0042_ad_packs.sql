-- Part C, Ad Pack: say what creatives you need, approve one plan, download one pack ready for Meta.
-- A pack goes request → planned → producing → review → ready; saved request templates sit beside the built-in ones.
CREATE TABLE IF NOT EXISTS ad_packs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL DEFAULT '',
    request_json  TEXT NOT NULL DEFAULT '{}',
    plan_json     TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'request',
    total_usd     REAL NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    approved_at   TEXT,
    finished_at   TEXT
);
CREATE TABLE IF NOT EXISTS ad_pack_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    request_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
