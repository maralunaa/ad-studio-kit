-- Brief S3: the real reviews and comments library. Lo-fi layouts (review mashup, comment screenshot) and the
-- review card draw only from here: every quote is real, attributed, with its source and date (decision i: no
-- invented or AI-written reviews).
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER,                             -- NULL = about the shop / every product
    kind        TEXT NOT NULL DEFAULT 'review',      -- review | comment
    text        TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT '',
    rating      INTEGER,                             -- 1-5 for reviews that have one
    source      TEXT NOT NULL,                       -- google | shopify | meta_comment | email | other
    source_url  TEXT NOT NULL DEFAULT '',
    posted_on   TEXT NOT NULL,                       -- YYYY-MM-DD, as shown where it was posted
    added_at    TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reviews_product ON reviews (product_id, kind);
