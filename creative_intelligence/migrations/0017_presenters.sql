-- Video Studio V5: AI presenters (brand narrators) and their looks. Every video with one is ai_person.
CREATE TABLE IF NOT EXISTS presenters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT '',
    voice           TEXT NOT NULL DEFAULT '',          -- a house voice (config.HOUSE_VOICES)
    look            TEXT NOT NULL DEFAULT '',          -- how they look: age, hair, clothes, setting
    status          TEXT NOT NULL DEFAULT 'draft',     -- draft | approved
    approved_look_id INTEGER,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS presenter_looks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    presenter_id  INTEGER NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'portrait',    -- portrait | angle
    label         TEXT NOT NULL DEFAULT '',
    path          TEXT,
    prompt        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'queued',      -- queued | done | failed | approved | rejected
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS presenter_looks_presenter ON presenter_looks (presenter_id, id);
