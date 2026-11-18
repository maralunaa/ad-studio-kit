-- Feedback 2026-10-22: "Make characters" writes four caricature characters for a product in one click. Each keeps its
-- caricature look (the expression and pose its look sheet asks for); chosen_at marks the approved character the
-- product's episodes use now (the last one the owner picked with "Use this one").
ALTER TABLE characters ADD COLUMN look TEXT NOT NULL DEFAULT '';
ALTER TABLE characters ADD COLUMN chosen_at TEXT;
