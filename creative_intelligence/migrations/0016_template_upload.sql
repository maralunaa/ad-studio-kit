-- V4: a winner's video uploaded by hand when the Meta token can't read it (Page-owned videos).
ALTER TABLE video_templates ADD COLUMN upload_path TEXT NOT NULL DEFAULT '';
