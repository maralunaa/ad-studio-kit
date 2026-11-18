-- Phase 2a R6: the swipe file (any competitor ad, our own ad, or a screenshot or link, with notes) and the monthly
-- taste rubric proposals Claude draws from it and from our winners. Each proposed criterion is approved or rejected
-- one at a time; approved ones join the QA rubric through an overlay on the volume (like approved knowledge).
CREATE TABLE IF NOT EXISTS swipe_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,                       -- competitor_ad | own_asset | screenshot | link
    ref_id       INTEGER,                             -- competitor_ads.id or adstudio_assets.id
    url          TEXT NOT NULL DEFAULT '',
    media_path   TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS taste_proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    trigger      TEXT NOT NULL DEFAULT 'manual',
    summary      TEXT NOT NULL DEFAULT '',
    items_json   TEXT NOT NULL DEFAULT '[]',          -- [{key, name, question, weight, applies_to, evidence, status}]
    cost_usd     REAL NOT NULL DEFAULT 0
);
