-- Knowledge base (K2d): true 15-second video views from the read-only Meta sync, so hold rate can follow the
-- playbook (15-second views ÷ 3-second views) instead of ThruPlays, which count the whole of a shorter video.
ALTER TABLE ad_daily_insights ADD COLUMN video_15s_views INTEGER;
