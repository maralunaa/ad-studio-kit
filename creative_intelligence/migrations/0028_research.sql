-- Phase 2a research (brief R; decision h): competitor ads from the Meta Ad Library API (read-only) or added by hand,
-- tagged with our own taxonomy (styles.yaml families and styles, hooks.yaml hook types), and the weekly research brief.
CREATE TABLE IF NOT EXISTS competitor_ads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,                  -- ad_library | manual
    ad_archive_id  TEXT,                           -- Meta Ad Library id (ad_library)
    page_name      TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    headline       TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',       -- Ad Library snapshot or the link the operator saw
    started_on     TEXT NOT NULL DEFAULT '',
    platforms      TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    style          TEXT NOT NULL DEFAULT '',       -- styles.yaml key (tagged)
    family         TEXT NOT NULL DEFAULT '',
    hook_type      TEXT NOT NULL DEFAULT '',       -- hooks.yaml key
    angle          TEXT NOT NULL DEFAULT '',
    tagged_at      TEXT,
    added_at       TEXT NOT NULL,
    UNIQUE (ad_archive_id)
);
CREATE TABLE IF NOT EXISTS research_briefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT 'manual',
    brief_json  TEXT NOT NULL DEFAULT '{}',
    cost_usd    REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'done',
    error       TEXT NOT NULL DEFAULT ''
);
