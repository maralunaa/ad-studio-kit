-- A presenter's persona (feedback 2026-11-04: the Farmer, the Fruit Nerd, the Chef, the Wellness Coach, the
-- Grandparent): who they are, how they talk and what they cover. Scripts with that presenter are written in it.
ALTER TABLE presenters ADD COLUMN brief TEXT NOT NULL DEFAULT '';
