-- Feedback 2026-10-22: BBB entries are complaints and are never used. Archive the ones already imported (they stay
-- stored; reviews.USABLE also keeps 1–3 star reviews out of ads, themes, phrases and ideas).
UPDATE reviews SET archived=1 WHERE source='bbb';
