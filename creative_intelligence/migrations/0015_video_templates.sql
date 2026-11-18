-- Video Studio V4: our winning video ads decoded into structure templates (presets for V6).
CREATE TABLE IF NOT EXISTS video_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id           TEXT NOT NULL,
    ad_name         TEXT NOT NULL DEFAULT '',
    video_id        TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',              -- video | thumbnail (partial)
    video_path      TEXT NOT NULL DEFAULT '',
    duration_s      REAL,
    metrics_json    TEXT NOT NULL DEFAULT '{}',
    scenes_json     TEXT NOT NULL DEFAULT '[]',
    transcript_json TEXT NOT NULL DEFAULT '[]',
    template_json   TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'queued',        -- queued | running | done | failed
    step            TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    cost_usd        REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS video_templates_ad ON video_templates (ad_id);
