-- Phase 2a R2: competitor ads tagged with the full taxonomy our own ads use (angle, hook type, style, format, offer
-- type, product type, visual style) plus why it might work, and competitor videos decoded into structure templates
-- labelled competitor-derived (structure only: never their footage, words or marks).
ALTER TABLE competitor_ads ADD COLUMN format TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN offer_type TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN product_type TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN visual_style TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN why_it_works TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN video_path TEXT NOT NULL DEFAULT '';
ALTER TABLE competitor_ads ADD COLUMN template_id INTEGER;
ALTER TABLE video_templates ADD COLUMN origin TEXT NOT NULL DEFAULT 'own';
ALTER TABLE video_templates ADD COLUMN competitor_ad_id INTEGER;
