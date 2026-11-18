-- Spending caps, editable on Admin (seeded from config the first time they are read).
CREATE TABLE IF NOT EXISTS budget_caps (
    key        TEXT PRIMARY KEY,   -- generation_run | generation_day | text_day | tagging_alert_day
    usd        REAL NOT NULL,
    updated_at TEXT
);
