-- Feedback 2026-10-22: ideas and competitor ads as pictures to reuse. "Make ours like this" makes AI-designed ads
-- with a reference picture (composition, light, layout, mood only: never the brand, words, fonts or product).
ALTER TABLE adstudio_textads ADD COLUMN ref_path TEXT NOT NULL DEFAULT '';
ALTER TABLE adstudio_textads ADD COLUMN ref_label TEXT NOT NULL DEFAULT '';
