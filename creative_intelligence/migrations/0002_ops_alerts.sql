-- Alerts shown as a banner on the Admin page (failed post-deploy smoke, rollbacks, failed backups).
CREATE TABLE IF NOT EXISTS ops_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'error',
    source      TEXT NOT NULL,
    message     TEXT NOT NULL,
    resolved_at TEXT
);
