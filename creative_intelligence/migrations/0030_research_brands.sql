-- Phase 2a R1: the brand registry (tiers A/B/C, seeded from the brief), the US keyword searches, and what each competitor
-- ad needs to be read by longevity: when it started and stopped, whether it is still active, its call to action and
-- landing page, a screenshot when it was added from one, and the concept it belongs to (near-identical variants share
-- one, and the variant count shows how much a brand invests). Brand context = offer, prices, guarantee, positioning,
-- refreshed monthly from the brand's public site with Claude web search.
CREATE TABLE IF NOT EXISTS research_brands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    tier          TEXT NOT NULL DEFAULT 'C',          -- A direct | B premium fruit and gifting | C same audience, other food
    website       TEXT NOT NULL DEFAULT '',
    page_name     TEXT NOT NULL DEFAULT '',           -- the Facebook Page name as the Ad Library shows it
    page_id       TEXT NOT NULL DEFAULT '',
    why           TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    context_json  TEXT NOT NULL DEFAULT '{}',
    context_at    TEXT,
    added_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_keywords (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    term      TEXT NOT NULL UNIQUE,
    active    INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    trigger      TEXT NOT NULL DEFAULT 'manual',
    status       TEXT NOT NULL DEFAULT 'running',
    result_json  TEXT NOT NULL DEFAULT '{}',
    error        TEXT NOT NULL DEFAULT ''
);
ALTER TABLE competitor_ads ADD COLUMN brand_id INTEGER;
ALTER TABLE competitor_ads ADD COLUMN ended_on TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE competitor_ads ADD COLUMN last_seen_on TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN cta TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN landing_url TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN media_path TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN media_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN concept_key TEXT NOT NULL DEFAULT '';
-- The watched brands and search terms are seeded from the brand pack on first use (adstudio/market.py › _seed).
