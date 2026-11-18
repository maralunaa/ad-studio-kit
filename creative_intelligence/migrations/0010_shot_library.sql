-- Video Studio V2: the shot library. A shot is a video of style 'shot' with a shot type (a recipe
-- card in prompts/shots/), generated once and reused.
ALTER TABLE adstudio_videos ADD COLUMN shot_type TEXT;
ALTER TABLE adstudio_videos ADD COLUMN card_version INTEGER;
ALTER TABLE adstudio_videos ADD COLUMN route TEXT;
ALTER TABLE adstudio_videos ADD COLUMN has_audio INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adstudio_videos ADD COLUMN retired INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adstudio_videos ADD COLUMN reuse_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS adstudio_videos_shots ON adstudio_videos (product_id, shot_type, status);
