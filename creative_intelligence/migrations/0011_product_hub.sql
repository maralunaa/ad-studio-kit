-- Product Hub: every Shopify product synced daily (archived, never deleted, when it leaves the store),
-- a profile per product (look, taste, customer language), tagged reference photos with heroes,
-- facts with a status, source and confidence, and research runs.
ALTER TABLE adstudio_products ADD COLUMN origin TEXT NOT NULL DEFAULT 'local';      -- shopify | local (not in store)
ALTER TABLE adstudio_products ADD COLUMN shopify_handle TEXT;
ALTER TABLE adstudio_products ADD COLUMN shop_json TEXT NOT NULL DEFAULT '{}';     -- type, tags, variants, image, price
ALTER TABLE adstudio_products ADD COLUMN in_stock INTEGER;
ALTER TABLE adstudio_products ADD COLUMN season TEXT NOT NULL DEFAULT '';          -- in | out | ''
ALTER TABLE adstudio_products ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adstudio_products ADD COLUMN synced_at TEXT;
ALTER TABLE adstudio_products ADD COLUMN taste TEXT NOT NULL DEFAULT '';
ALTER TABLE adstudio_products ADD COLUMN taste_status TEXT NOT NULL DEFAULT '';    -- '' | draft | approved
ALTER TABLE adstudio_products ADD COLUMN customer_language TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS adstudio_products_handle ON adstudio_products (shopify_handle);

ALTER TABLE adstudio_photos ADD COLUMN tag TEXT NOT NULL DEFAULT '';               -- whole | cut | in_hand | in_box | lifestyle
ALTER TABLE adstudio_photos ADD COLUMN is_hero INTEGER NOT NULL DEFAULT 0;

-- Facts written before the hub were approved by hand, so they stay approved.
ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT 'approved';             -- pending | approved | rejected | do_not_use
ALTER TABLE facts ADD COLUMN source_url TEXT NOT NULL DEFAULT '';
ALTER TABLE facts ADD COLUMN confidence TEXT NOT NULL DEFAULT '';                 -- high | medium | low
ALTER TABLE facts ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual';               -- manual | research
ALTER TABLE facts ADD COLUMN note TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS shopify_syncs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    counts_json TEXT NOT NULL DEFAULT '{}',
    error       TEXT
);

CREATE TABLE IF NOT EXISTS product_research (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',                                   -- queued | running | done | failed
    estimate_usd REAL NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    searches    INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS product_research_product ON product_research (product_id, id);
