-- Compose → Bulk: one row per bulk batch (its plan, progress and outcome); ads it made point back to it.
CREATE TABLE IF NOT EXISTS compose_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL,
    plan_json   TEXT NOT NULL DEFAULT '{}',
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    message     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS compose_batches_product ON compose_batches (product_id, id);
ALTER TABLE adstudio_composites ADD COLUMN batch_id INTEGER;
ALTER TABLE adstudio_textads ADD COLUMN batch_id INTEGER;
