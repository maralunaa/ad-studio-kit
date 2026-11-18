-- Brief S2 #1: fruit characters. Each character has a bible (name, personality, voice, do's and don'ts) and AI look
-- sheets; the owner approves one sheet before the character can be used. Episodes are QA'd against the approved sheet.
CREATE TABLE IF NOT EXISTS characters (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL,
    name              TEXT NOT NULL,
    personality       TEXT NOT NULL DEFAULT '',
    voice             TEXT NOT NULL DEFAULT '',
    dos               TEXT NOT NULL DEFAULT '',
    donts             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'draft',      -- draft | approved | retired
    approved_sheet_id INTEGER,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS character_sheets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id  INTEGER NOT NULL,
    prompt        TEXT NOT NULL,
    path          TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',         -- queued | done | failed | approved | rejected
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS characters_product ON characters (product_id);
