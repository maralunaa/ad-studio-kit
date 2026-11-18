-- Knowledge base (K2b): what the measured QA checks saw (text boxes, word counts, CTA, contrast, how much of
-- the product the copy covers), and per video version its export spec and caption measurements, for the
-- pre-launch checklist.
ALTER TABLE qa_scores ADD COLUMN measured_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE adstudio_video_renders ADD COLUMN checks_json TEXT NOT NULL DEFAULT '{}';
