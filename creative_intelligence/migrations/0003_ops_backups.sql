-- One row per backup attempt (nightly database backup to Drive; weekly image backup).
CREATE TABLE IF NOT EXISTS ops_backups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,              -- 'database' | 'images'
    trigger       TEXT NOT NULL DEFAULT 'scheduled',
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',   -- running | done | failed
    file_name     TEXT,
    size_bytes    INTEGER,
    drive_file_id TEXT,
    message       TEXT
);
