-- Feedback 2026-10-22 (F6): several videos in one go. A batch writes every script first (a few cents each), shows
-- one plan with one estimate, and nothing paid starts until it is approved.
CREATE TABLE IF NOT EXISTS video_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_json TEXT NOT NULL DEFAULT '{}',
    items_json   TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'planning',     -- planning | planned | started | failed | cancelled
    total_usd    REAL NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    started_at   TEXT
);
