-- Our own Instagram posts (feedback 2026-10-27: "add a section for our own organic content, pulled from IG").
CREATE TABLE IF NOT EXISTS ig_posts (
    id          TEXT PRIMARY KEY,                  -- Instagram media id
    caption     TEXT NOT NULL DEFAULT '',
    media_type  TEXT NOT NULL DEFAULT '',          -- IMAGE | VIDEO | CAROUSEL_ALBUM
    permalink   TEXT NOT NULL DEFAULT '',
    posted_at   TEXT NOT NULL DEFAULT '',
    likes       INTEGER NOT NULL DEFAULT 0,
    comments    INTEGER NOT NULL DEFAULT 0,
    reach       INTEGER,
    saves       INTEGER,
    shares      INTEGER,
    media_path  TEXT NOT NULL DEFAULT '',          -- the picture (a video's cover) cached on the volume
    synced_at   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ig_sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',   -- running | done | failed
    message     TEXT NOT NULL DEFAULT '',
    added       INTEGER NOT NULL DEFAULT 0
);
