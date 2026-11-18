-- Background work, done by the worker process (python -m creative_intelligence.worker), never the web.
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key   TEXT,                       -- at most one queued/running job per key
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    run_after    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    worker       TEXT,
    error        TEXT,
    result_json  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status, run_after, id);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs (dedupe_key, status);

-- One row per worker process: when it started and when it last checked in.
CREATE TABLE IF NOT EXISTS ops_worker (
    id           TEXT PRIMARY KEY,           -- host:pid
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
