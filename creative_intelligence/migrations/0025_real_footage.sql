-- Brief S4: real footage uploaded to the shot library (customer reactions, interviews, testimonials, the founder or
-- farmer story). Tagged real, never AI-made, never mixed with AI people; who filmed it, when, and the consent kept.
ALTER TABLE adstudio_videos ADD COLUMN real INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adstudio_videos ADD COLUMN real_json TEXT NOT NULL DEFAULT '{}';
