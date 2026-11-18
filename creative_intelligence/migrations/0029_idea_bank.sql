-- Phase 2a R4: the idea bank. An idea is an angle + a hook pattern (a pattern, never copy: decision f) + a style,
-- for some products, with its evidence kept in parts (our results, the market, customers), never one number.
-- Generation uses ideas for a set share of what it makes (knowledge rules.ideas_mix), and idea_id marks what an idea made.
CREATE TABLE IF NOT EXISTS ideas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    angle          TEXT NOT NULL DEFAULT '',          -- why someone buys
    hook           TEXT NOT NULL DEFAULT '',          -- the hook pattern, generation writes its own words
    hook_type      TEXT NOT NULL DEFAULT '',          -- hooks.yaml key
    style          TEXT NOT NULL DEFAULT '',          -- styles.yaml key ('' = any)
    medium         TEXT NOT NULL DEFAULT 'any',       -- static | video | any (from the style)
    product_ids    TEXT NOT NULL DEFAULT '[]',        -- JSON list, [] = every product
    why            TEXT NOT NULL DEFAULT '',
    evidence_json  TEXT NOT NULL DEFAULT '{}',        -- {customer_terms: [...], review_ids: [...], competitor_ad_ids: [...], notes}
    status         TEXT NOT NULL DEFAULT 'new',       -- new | approved | in_production | testing | winner | fatigued | retired
    source         TEXT NOT NULL DEFAULT 'manual',    -- manual | drafted | brief | swipe
    source_ref     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ideas_status ON ideas (status);
ALTER TABLE adstudio_images ADD COLUMN idea_id INTEGER;
ALTER TABLE adstudio_hooks ADD COLUMN idea_id INTEGER;
ALTER TABLE adstudio_briefs ADD COLUMN idea_id INTEGER;
ALTER TABLE adstudio_videos ADD COLUMN idea_id INTEGER;
ALTER TABLE adstudio_assets ADD COLUMN idea_id INTEGER;
