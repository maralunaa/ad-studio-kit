-- V7: each finished version keeps its own length (variants of one video can be 15 s or 30 s).
ALTER TABLE adstudio_video_renders ADD COLUMN duration_s REAL;
