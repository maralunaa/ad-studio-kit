-- Feedback 2026-10-22 (F4): the hook critic's score and one-line reason for every hook it kept.
ALTER TABLE adstudio_hooks ADD COLUMN critic_score REAL;
ALTER TABLE adstudio_hooks ADD COLUMN critic_note TEXT NOT NULL DEFAULT '';
