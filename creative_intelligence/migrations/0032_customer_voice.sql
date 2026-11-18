-- Phase 2a R3: customer voice. Reviews remember where they came from (external id, so an import can re-run) and
-- whether the source picked them (the site testimonials page is curated). Themes are what Claude reads in them,
-- per product (NULL = shop-wide): desires, objections, use moments and exact phrases, each counted in code from the
-- reviews it was found in (review_ids link back to the source). An objection counts as an angle only with an
-- approved fact behind it (fact_id).
ALTER TABLE reviews ADD COLUMN external_id TEXT;
ALTER TABLE reviews ADD COLUMN curated INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS reviews_external ON reviews (external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS voice_themes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    product_id  INTEGER,
    kind        TEXT NOT NULL,                       -- desire | objection | moment | phrase
    theme       TEXT NOT NULL,
    mentions    INTEGER NOT NULL DEFAULT 0,
    review_ids  TEXT NOT NULL DEFAULT '[]',
    fact_id     INTEGER,
    angle       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS voice_themes_product ON voice_themes (product_id, kind);
CREATE TABLE IF NOT EXISTS voice_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,                      -- import_fera | import_trustpilot | import_bbb | import_testimonials | import_csv | extract
    product_id   INTEGER,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    result_json  TEXT NOT NULL DEFAULT '{}',
    cost_usd     REAL NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT ''
);
