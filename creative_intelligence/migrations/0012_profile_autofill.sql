-- Product Hub auto-fill: a product added to the list fills its own profile (photos, look, research,
-- customer language, 5 hooks). One status per product so the page can show progress.
ALTER TABLE adstudio_products ADD COLUMN fill_status TEXT NOT NULL DEFAULT '';   -- '' | queued | running | done | failed
ALTER TABLE adstudio_products ADD COLUMN fill_step TEXT NOT NULL DEFAULT '';
ALTER TABLE adstudio_products ADD COLUMN fill_error TEXT NOT NULL DEFAULT '';
ALTER TABLE adstudio_products ADD COLUMN fill_cost_usd REAL NOT NULL DEFAULT 0;
