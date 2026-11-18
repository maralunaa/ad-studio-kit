-- Facts shared by a fruit family (every mango): product_id NULL and family set, researched once and
-- reused by every product of that family. Global facts keep family ''.
ALTER TABLE facts ADD COLUMN family TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS facts_family ON facts (family, status);
