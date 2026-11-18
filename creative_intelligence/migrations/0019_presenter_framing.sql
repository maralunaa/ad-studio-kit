-- Presenters for every house voice: each keeps a framing (a still portrait or a handheld phone selfie).
ALTER TABLE presenters ADD COLUMN framing TEXT NOT NULL DEFAULT 'portrait';
-- The first Wade look (Latino, olive linen, sunlit kitchen) becomes Luis with his looks; Wade becomes the selfie presenter.
UPDATE presenters SET name='Luis', voice='Luis' WHERE name='Wade' AND look LIKE '%Latino%'
    AND NOT EXISTS (SELECT 1 FROM presenters WHERE name='Luis');
