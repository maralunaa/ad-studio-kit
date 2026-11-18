-- Video Studio V1: a clip made from an approved still, and the finished versions of it (text, size).
CREATE TABLE IF NOT EXISTS adstudio_videos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     INTEGER NOT NULL,
    source_kind    TEXT NOT NULL,               -- 'image' | 'core'
    source_id      INTEGER NOT NULL,
    style          TEXT NOT NULL DEFAULT 'animate',
    motion_preset  TEXT NOT NULL DEFAULT '',
    motion_prompt  TEXT NOT NULL DEFAULT '',
    duration_s     INTEGER NOT NULL DEFAULT 5,
    still_strategy TEXT NOT NULL DEFAULT '',    -- 'as_is' | 'native' | 'outpaint'
    still_path     TEXT,                        -- the 9:16 still the clip starts from
    clip_path      TEXT,                        -- the generated clip, no text
    model          TEXT NOT NULL DEFAULT '',
    copy_json      TEXT NOT NULL DEFAULT '{}',  -- {template, headline, sub, cta, hook_id} for the first renders
    status         TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    step           TEXT NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0,
    message        TEXT NOT NULL DEFAULT '',
    prompt_version TEXT,
    ai_person      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS adstudio_videos_product ON adstudio_videos (product_id, id);

CREATE TABLE IF NOT EXISTS adstudio_video_renders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      INTEGER NOT NULL,
    product_id    INTEGER NOT NULL,
    aspect        TEXT NOT NULL,                -- '9:16' | '4:5'
    template      TEXT NOT NULL DEFAULT '',
    headline      TEXT NOT NULL DEFAULT '',
    subhead       TEXT NOT NULL DEFAULT '',
    cta           TEXT NOT NULL DEFAULT '',
    hook_id       INTEGER,
    output_path   TEXT,
    poster_path   TEXT,
    status        TEXT NOT NULL DEFAULT 'done',
    review_status TEXT NOT NULL DEFAULT '',
    is_ready      INTEGER NOT NULL DEFAULT 0,
    review_notes  TEXT NOT NULL DEFAULT '',
    reviewed_at   TEXT,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adstudio_video_renders_video ON adstudio_video_renders (video_id);
