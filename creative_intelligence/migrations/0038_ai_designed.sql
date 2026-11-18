-- Feedback 2026-10-22: Images is Bulk + One ad, and AI ads live inside them. An AI ad's words are either painted by
-- the image model (AI-designed, read back by the text check) or drawn by code on a text-free scene. '' = the old
-- default (TEXTFREE_AI_ADS). A painted ad that fails the read-back is made once more with the problems as its fix.
ALTER TABLE adstudio_textads ADD COLUMN text_mode TEXT NOT NULL DEFAULT '';
ALTER TABLE adstudio_textads ADD COLUMN text_retries INTEGER NOT NULL DEFAULT 0;
