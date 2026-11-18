-- Video Studio V3: storytelling videos keep their timeline (9:16) and their house voice.
ALTER TABLE adstudio_videos ADD COLUMN timeline_json TEXT;
ALTER TABLE adstudio_videos ADD COLUMN voice TEXT NOT NULL DEFAULT '';
